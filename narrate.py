#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 一期 解说词生成层
=====================================================================
职责：把 data/snapshot.json（纯数据）转成各人设的解说词。
输出：data/commentary_<key>.json（人设 -> [{t, text, tag, refs}]），供 build_live.py 渲染
架构：
  * 本地引擎 = 一个 LLM 调用 + 一个 prompt。LLM 接口抽象为 `llm_call()`。
  * **一期默认 --engine local**：不调任何外部 API，用「模板+启发式」本地合成器
    先把页面跑通、把直播时序跑通。这是最小成本验证「像不像球赛」的方式。
  * --engine api：走外部 LLM（需要 key），生成质量更高。
纪律：
  * 生成层永不碰网络（除 --engine api），只读 snapshot（单向依赖）
  * 人设 = personas/*.json，加人设不改引擎
运行：
  python3 narrate.py --engine local              # 本地合成器，零依赖
  python3 narrate.py --engine api --key sk-xxx   # 接外部 LLM
"""

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(line_buffering=True)

# V1.9.1：源快讯全文缓存（后处理器 _fix_metric_name 要比对源里的英文口径）
F_NEWS_CACHE = []

# 行情报价时点距离快照采集时刻的阈值（秒）。超过即视为"陈旧报价"。
#⚠️ V1.9.2 实测踩到：盘前时段拿到的是**上一交易日收盘价**（道琼斯报价时刻
#   北京 10-06 04:49 = 美东 10-05 16:49 收盘），但prompt 只给了「+0.18%」没给
#   时点，模型就当成本场实时涨幅播出去（实测「道琼斯现在 51267.90 点，涨了 0.18%」）。
#   **数字本身没错，错的是时点语义** —— 用户看到"盘前还没开盘"却又一条"现在涨了
#   0.18%"，自然认为编造。**凡报数字必须带时点，否则"陈旧数据"会被播成"实时"。**
QUOTE_FRESH_SEC = 45 * 60# 45 分钟：盘中超过即视为可能陈旧


def log(m):
    print(m, flush=True)


def load(snap_path, personas_dir):
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)
    personas = []
    for fn in sorted(os.listdir(personas_dir)):
        if fn.endswith(".json"):
            with open(os.path.join(personas_dir, fn), encoding="utf-8") as f:
                personas.append(json.load(f))
    return snap, personas


# ====================================================================
# 一期 · 本地合成器
# ====================================================================
#思路：不调 LLM也能产出「有节奏、有落差」的解说词骨架。
# 做法 = 事实卡（从snapshot 抽的硬事实）+ 人设专属句式模板 + 随机气口。
# 目的：先把「直播时序 + 人设切换 + 视觉」跑通；等接 LLM 后只换事实卡内容。
#局限：句式终究是模板，AI 味去不掉—— 这正是一期要暴露的问题。
# ====================================================================

GAS = ["好", "嗯", "嚯", "来", "你看", "这", "得嘞", "嘿", "哎", "好家伙"]
RETAIL_GAS = ["我天", "不是", "诶", "你敢信", "我靠", "完了完了", "我的妈呀", "好家伙"]


def pct(x, digits=2):
    return ("%+.2f%%" % x) if isinstance(x, (int, float)) else "—"


def amount(x):
    if not isinstance(x, (int, float)) or x <= 0:
        return "—"
    if x >= 1e12:
        return "%.0f亿" % (x / 1e8)          # 成交额单位：元
    if x >= 1e8:
        return "%.0f亿" % (x / 1e8)
    return "%.0f万" % (x / 1e4)


# ⚠️ V1.9.36：快讯选取改为「按场次信源配比」。旧口径 = eulerpool 优先且不设上限
#   （`(_ep + _rest)[:8]`）——实测 2026-10-09 A股 交易中，24 条 eulerpool 把 8 个坑位
#   **全占掉**（东财快讯 7 条 + 东财财经 5 条一条都进不了 prompt）→ 解说在讲 A股，
#   手里却只有"Meta/AMD/CEG"这些美股个股消息，于是只能回头念指数与个股涨幅。
#   连带后果：link_news_sectors 拿不到中文快讯 →【快讯↔板块】关联恒为空。
#   修法：三桶（中媒 / Eulerpool / 其余外媒）按场次配额取，总量仍 8 条。
CN_NEWS_SRC = ("东财快讯", "东财财经", "财联社", "同花顺", "新浪财经", "证券时报")

# 场次 → (中媒, Eulerpool, 其余外媒) 配额，和为 8。A股 场以国内消息为主，
# 美股场以 eulerpool 个股消息为主，港股场两边兼顾。
NEWS_QUOTA = {"cn": (6, 2, 0), "hk": (4, 3, 1), "us": (2, 5, 1), None: (4, 3, 1)}

# ⚠️ 时效闸（V1.9.36）：Eulerpool 是"按 ticker 抓最近 N 条"，未必是当日的 —— 实测
#   2026-10-09 A股 盘中，它最新一条还停在 10-08 11:52（约 27 小时前）。A股/港股场次讲的是
#   "当下这一刻"，塞隔夜美股个股旧闻是噪声；美股场（尤其盘前）讲前夜消息正合适。
#   超时的**不进 prompt**，空出的坑位由其余快讯（东财快讯等）按时间补齐。
EP_MAX_AGE_H = {"cn": 24.0, "hk": 24.0, "us": 48.0, None: 36.0}


def _news_ts_key(x):
    """'MM-DD HH:MM' → 可比较的近似序（同年足够；跨年数据不参与选优）。"""
    t = (x.get("time") or "").strip()
    return t if len(t) >= 11 else ""


def _is_cn_news(x):
    """中媒快讯判定：源名命中，或非外媒 + 7×24/公司频道（东财系的两条通道）。"""
    s = x.get("src") or ""
    if any(k in s for k in CN_NEWS_SRC):
        return True
    return (x.get("foreign") is False and (x.get("chan") or "") in ("7×24", "公司"))


def _news_age_h(x):
    """快讯年龄（小时）。collect_live 已给 _age_h 的直接用，否则从 'MM-DD HH:MM' 算。"""
    try:
        if x.get("_age_h") is not None:
            return float(x["_age_h"])
    except Exception:                                   # noqa: BLE001
        pass
    m = re.match(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", (x.get("time") or "").strip())
    if not m:
        return None
    try:
        _now = dt.datetime.now()
        _t = dt.datetime(_now.year, int(m.group(1)), int(m.group(2)),
                         int(m.group(3)), int(m.group(4)))
    except Exception:                                   # noqa: BLE001
        return None
    h = (_now - _t).total_seconds() / 3600.0
    if h < -24 * 30:                                    # 跨年（年初看去年 12 月）
        h += 365 * 24
    return h


def _sector_hit_score(item, sec_names):
    """快讯与"今日真实在榜板块"的主题重合度（用于 A股 场选源时的关联优先排序）。

    只做 0/1 打分：① 板块名原样出现 +2；② 命中行业簇（泛称/实体词）且该簇对应的
    细分板块今天在榜 +1。**不在榜的板块不给分** —— 与 link_news_sectors 同一纪律：
    绝不为了"有故事讲"去挂一个今天榜上没有的板块。"""
    hay = ("%s %s" % (item.get("title") or "", item.get("text") or "")).lower()
    sc = 0
    for nm in sec_names:
        if nm and nm in hay:
            sc += 2
    for kws, frags in NEWS_SECTOR_CLUSTERS:
        if any(k.lower() in hay for k in kws) and any(
                any(f in nm for f in frags) for nm in sec_names):
            sc += 1
    return sc


def pick_news(all_news, active, sectors=None):
    """按场次配额混采快讯，不足用其余桶补齐，最后按时间倒序返回（最多 8 条）。

    ⚠️ V1.9.36：A股/港股场次对**中媒桶做"关联优先"排序** —— 与今日在榜板块主题
    重合的快讯排前面（各自内部仍按时间倒序）。理由：师傅反馈"解说流里 A股 很少引用
    资讯"，根因之一是被选中的快讯（隔夜美股个股消息、泛宏观）与当天盘面毫无关系，
    模型就算引用了也接不上板块主线。让"能接上主线"的快讯优先入场，资讯才有落点。"""
    _key = active if active in ("cn", "hk", "us") else None
    _lim = EP_MAX_AGE_H.get(_key, 36.0)

    def _ep_fresh(x):
        if (x.get("src") or "") != "Eulerpool":
            return False
        _a = _news_age_h(x)
        return _a is None or _a <= _lim

    _cn = [x for x in all_news if _is_cn_news(x)]
    _ep = [x for x in all_news if _ep_fresh(x)]
    _ot = [x for x in all_news if not _is_cn_news(x) and (x.get("src") or "") != "Eulerpool"]
    q = NEWS_QUOTA.get(_key) or NEWS_QUOTA[None]
    # 关联优先（仅中媒桶；_all 传进来时已是时间倒序，两段各自保持时序）
    _sec_names = []
    for _s in ((sectors or {}).get("top") or []):
        if _s.get("name"):
            _sec_names.append(_s["name"])
    for _s in ((sectors or {}).get("bottom") or []):
        if _s.get("name"):
            _sec_names.append(_s["name"])
    if _sec_names and _cn:
        _hits = [x for x in _cn if _sector_hit_score(x, _sec_names) > 0]
        if _hits:
            _cn = _hits + [x for x in _cn if _sector_hit_score(x, _sec_names) == 0]
    buckets = [_cn[:q[0]], _ep[:q[1]], _ot[:q[2]]]
    out = [x for b in buckets for x in b]
    if len(out) < 8:                      # 某桶不足 → 用没进桶的按时间补齐
        seen = set(id(x) for x in out)
        for x in all_news:
            if len(out) >= 8:
                break
            if id(x) in seen:
                continue
            out.append(x)
            seen.add(id(x))
    return sorted(out, key=_news_ts_key, reverse=True)[:8]


def facts(snap):
    """从 snapshot 抽硬事实卡—— 合成器和 LLM prompt 共用同一份，保证数据一致。
    三市场：cn/hk/us 各自一份index；A 股独有 breadth/榜/板块/涨跌停。

    ⚠️ V1.8.0 陈旧数据闸：A股微观数据（涨跌家数/榜/板块/涨停池/比赛态势）只在
       「snapshot 日期 == 今天」时才可用。否则那是上一个交易日的快照——
       拿它讲今天的盘、或拿它当休市日的实况，都是假直播。
       （实测触发：2026-10-06 A股国庆休市、港股开市，旧口径会把 9/30 的涨停家数
         当今日实况播出去。快照日期由 collect_live.py --date 写入。）"""
    cn_fresh = (str(snap.get("date") or "") == time.strftime("%Y%m%d"))
    idx = snap.get("index") or {}
    b = snap.get("breadth") if cn_fresh else None
    zt = ((snap.get("zt") or {}).get("tc")) if cn_fresh else None
    dt_ = ((snap.get("dt") or {}).get("tc")) if cn_fresh else None
    sec = (snap.get("sectors") or {}) if cn_fresh else {}
    mk = snap.get("markets") or {}
    sess = snap.get("session") or {}
    # ⚠️ V1.9.36：这里的选源规则**已上移到模块级 pick_news()**（按场次信源配比）。
    #   历史沿革留档：V1.9.0 起曾"优先 eulerpool 个股新闻、不足 8 条再补齐"——
    #   eulerpool 挂着具体 ticker + 具体数字，确实比泛泛宏观长文更适合解说；但
    #   **不设上限**的代价是 A股 场次被美股个股消息占满 8/8 坑位（见 pick_news 注释）。
    #   时间倒序的理由不变：eulerpool 抓取是按 ticker 逐个插入，不排序就会"配置表里
    #   最后一个 ticker 的旧闻霸占前排"（实测先出 GOOGL/META 旧闻）。
    _all = sorted(snap.get("news") or [], key=_news_ts_key, reverse=True)
    _picked = pick_news(_all, sess.get("active"), snap.get("sectors") if cn_fresh else None)
    # V1.9.1：把源文本缓存给后处理器（_fix_metric_name 要比对源里的英文口径）
    global F_NEWS_CACHE
    F_NEWS_CACHE = _all
    # ⚠️ V1.9.31：日内分时摘要（intraday_summary）—— 只喂摘要不喂原始序列，避免爆 token；
    #   给 LLM 走势形态/振幅/斜率，替代旧的"指数逐条报数"，回应师傅"解说偏指数、不解读走势"反馈。
    intraday = snap.get("intraday") or {}
    # ⚠️ V1.9.34：RRG 跨资产轮动状态（只读解析 yangxiaa.cc/rrg/，落盘 data/rrg_state.json）
    #   定性背景态：象限分布 + risk_read 一句话判读 + 与 A股联动资产。不喂原始百分比，
    #   避免被 _fix_market_pct 误删；RRG 跨资产、不含 A股行业，只能讲全球资金/风险偏好。
    _rrg_path = os.path.join(HERE, "data", "rrg_state.json")
    rrg = None
    try:
        if os.path.exists(_rrg_path):
            _rrg = json.load(open(_rrg_path, encoding="utf-8"))
            _rrg_sum = (_rrg.get("summary") or {})
            rrg = {
                "available": True,
                "data_date": _rrg.get("data_date"),
                "counts": _rrg_sum.get("quadrant_counts") or {},
                "risk_read": _rrg_sum.get("risk_read"),
                "overheated": _rrg_sum.get("overheated") or [],
                "a_share_related": _rrg_sum.get("a_share_related") or [],
                "assets": _rrg.get("assets") or [],
            }
    except Exception:
        rrg = None
    intraday_summary = {}
    for _k, _v in intraday.items():
        _cd = _v.get("candles") or []
        if len(_cd) < 2:
            continue
        _o = _cd[0][1]
        _h = max(c[2] for c in _cd); _l = min(c[3] for c in _cd); _c = _cd[-1][4]
        _amp = (_h - _l) / _o * 100 if _o else 0
        _mid = max(1, len(_cd) // 2)
        _first = sum(c[4] for c in _cd[:_mid]) / _mid
        _second = sum(c[4] for c in _cd[_mid:]) / max(1, len(_cd) - _mid)
        _slope = (_second - _first) / _first * 100 if _first else 0
        if _c >= _o:
            _shape = "震荡上行" if _slope > 0 else "冲高回落"
        else:
            _shape = "震荡下行" if _slope < 0 else "探底回升"
        intraday_summary[_k] = {"name": _v.get("name", _k), "open": round(_o, 2),
                                "high": round(_h, 2), "low": round(_l, 2), "close": round(_c, 2),
                                "amp_pct": round(_amp, 2), "slope_pct": round(_slope, 2),
                                "shape": _shape, "bars": len(_cd)}
    # ⚠️ V1.9.31：美股广度（DeanFi S&P500）—— 现成格局维度，旧口径零引用，现接入解说。
    _db = snap.get("us_breadth_deanfi")
    us_breadth = None
    if isinstance(_db, dict) and _db.get("advances") is not None:
        us_breadth = {"advances": _db.get("advances"), "declines": _db.get("declines"),
                      "ad_ratio": _db.get("ad_ratio"), "above_200ma_pct": _db.get("above_200ma_pct"),
                      "adv_pct": _db.get("adv_pct"), "date": _db.get("date"),
                      "universe": _db.get("universe", "S&P 500")}
    # ⚠️ V1.9.32 C 方向：快讯 ↔ 板块 轻量关键词关联钩子（仅 A股 当日数据 + 真实出现板块）
    news_sector_links = link_news_sectors(_picked, sec) if (cn_fresh and sec) else []
    return {
        "date": snap.get("date"),
        "cn_fresh": cn_fresh,
        "index": idx,
        "markets": mk,
        # ⚠️ V1.9.6：港股个股（龙头）。此前 facts 里只有 markets.hk 的 3 个指数，
        #   所以港股场次 AI 只能讲指数 —— 页面补了面板还不够，**提示词也得喂**，
        #   否则解说词与页面对不上（页面有 15 只个股、解说里一个不报）。
        "hk_stocks": snap.get("hk_stocks") or [],
        "market_keys": [k for k in ("cn", "hk", "us") if mk.get(k)],
        "session": sess,
        "news": _picked,
        "news_total": len(_all),
        # ⚠️ 经济数据官方发布日历（eulerpool）。**只有"何时发什么"，没有任何数值**
        #   —— 连history 端点也拿不到 actual/prev。prompt 里只能当"议程"用，
        #   铁律：涉及经济数据不许报数字（否则模型必编"市场预期 3.8%"）。
        "econ_cal": (snap.get("econ_cal") or [])[:14],
        "global": snap.get("global_ticker") or [],
        "breadth": b,
        "zt": zt, "dt": dt_,
        "sec_top": (sec.get("top") or [])[:5],
        "sec_bottom": (sec.get("bottom") or [])[:5],
        "up": (((snap.get("gainers") or {}).get("rows") or [])[:6]) if cn_fresh else [],
        "down": (((snap.get("losers") or {}).get("rows") or [])[:6]) if cn_fresh else [],
        "active": (((snap.get("active") or {}).get("rows") or [])[:6]) if cn_fresh else [],
        "zt_pool": (((snap.get("zt") or {}).get("pool") or [])[:6]) if cn_fresh else [],
        "dt_pool": (((snap.get("dt") or {}).get("pool") or [])[:6]) if cn_fresh else [],
        "game": (snap.get("game") or {}) if cn_fresh else {},   # 比赛态势（MVP/乌龙/最佳），与页面同源
        # ⚠️ V1.9.31：日内分时摘要 + 美股广度（已采未用，现接入解说）
        "intraday_summary": intraday_summary,
        "us_breadth": us_breadth,
        # ⚠️ V1.9.32 C 方向：快讯 ↔ 板块 轻量关键词关联
        "news_sector_links": news_sector_links,
        # ⚠️ V1.9.34：RRG 跨资产轮动状态（只读解析，定性背景态）
        "rrg": rrg,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="local", choices=["local", "api"])
    ap.add_argument("--preset", default="", choices=["", "deepseek", "agnes"],
                    help="LLM 预设：从 config.json llm.presets 读 base/model/key；与 --base/--model/--key 互斥补充")
    ap.add_argument("--key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--base", default=os.environ.get("LLM_BASE", ""))
    ap.add_argument("--model", default="")
    ap.add_argument("--rpm", type=int, default=10,
                    help="LLM 免费档速率上限（次/分）。调用间按 60/rpm 留间隔防突发超频")
    ap.add_argument("--snapshot", default=os.path.join(HERE, "data", "snapshot.json"))
    ap.add_argument("--personas", default=os.path.join(HERE, "personas"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "data"))
    ap.add_argument("--market", default="auto",
                    help="cn|hk|us|auto（auto = 按当前时段选正在开市的市场，休市回退A股）")
    a = ap.parse_args()

    # ---- LLM 预设（--preset）：从 config.json llm.presets 读 base/model/key ----
    # 规则：--base/--model/--key 显式传入优先；否则用预设值；key 再回落到预设的 key_env 环境变量。
    if a.preset:
        cfg_path = os.path.join(HERE, "config.json")
        try:
            presets = json.load(open(cfg_path, encoding="utf-8")).get("llm", {}).get("presets", {})
        except Exception as e:                                    # noqa: BLE001
            log("[x] 读 config.json 失败：%s" % e)
            presets = {}
        pc = presets.get(a.preset)
        if not pc:
            log("[x] 预设 %s 不存在（config.json llm.presets）" % a.preset)
            sys.exit(1)
        a.base = a.base or pc.get("base", "")
        a.model = a.model or pc.get("model", "")
        a.rpm = a.rpm or pc.get("rpm", 10)
        if not a.key:
            a.key = os.environ.get(pc.get("key_env", ""), "") or pc.get("key", "")

    if a.engine == "api" and not a.base:
        log("[x] api 引擎需要 --base（或用 --preset 提供）")
        sys.exit(1)
    if a.engine == "api" and not a.model:
        log("[x] api 引擎需要 --model（或用 --preset 提供）")
        sys.exit(1)

    snap, personas = load(a.snapshot, a.personas)
    F = facts(snap)
    sess = F.get("session") or {}
    avail = F.get("market_keys") or []
    MKT_CN = {"cn": "A股", "hk": "港股", "us": "美股"}
    # ⚠️ V1.8.4 目标市场选择（师傅明确要求）：
    #   「A股没开市不需要做什么复盘」+「只针对开市中的市场解锁」+「午间休市可以解说你拿到的全球资讯」。
    #   早先逻辑：active 不可用就**回退 A股**（`else ("cn" if ...)`）→ 全场休市时
    #   会生成一整套"A股收盘复盘"，而那是上一个交易日的数据，纯误导 + 易编造。
    #   现在：active 必须是**真正开市中**的那个市场（state=="open"）；
    #   全场休市 → mk=None，产「全球资讯解读」场次（用走马灯 + 外媒快讯这些真数据），
    #   不碰任何单一市场的行情。
    # ⚠️⚠️ V1.9.1 师傅指出真bug：「你当前是美股盘前就只讲美股的东西，为何要提AH 无关的」。
    #   根因：这里只认 state=="open"，**盘前(pre)被判成"没开市"** → mk=None →
    #   走全球速览场 → prompt 里把"A股休市 / 港股收盘 / 美股盘前"三个市场的状态
    #   全列出来（那是"各市场当前状态"这行数据的用途），模型照读就成了
    #   「A股呢今天假期休市…港股刚才收了…美股那边还在盘前候着」—— 与本场无关的两家
    #   占了开头 3 句。**盘前时段本就该聚焦那个即将开盘的市场**（它的盘前定价、
    #   它的个股新闻、它相关的经济数据），不该退化成全球泛览。
    #   修法：pre（盘前）**也算"本场聚焦市场"**，只有真正全场无事件时才退全球速览。
    #   ⚠️ 但state=="post"（盘后）不纳入 —— 盘后属于"今天已收工"，讲它等于复盘，
    #   与师傅「A股没开市不做复盘」那条冲突。
    if a.market == "auto":
        mk = sess.get("active")
        if mk not in avail:
            mk = None
        else:
            st_mk = (sess.get("all", {}).get(mk) or {})
            # 盘前 = 即将开盘，是"本场主角"；休市/盘后 = 无事件可讲 → 退回全球速览
            if st_mk.get("holiday") or st_mk.get("state") not in ("open", "pre"):
                mk = None
    else:
        mk = a.market
    F["mk"] = mk
    F["is_open"] = bool(mk) and bool((sess.get("all", {}).get(mk) or {}).get("open"))
    F["mkt_name"] = MKT_CN.get(mk, "全球")
    # 本场聚焦市场的状态（open=盘中 / pre=盘前），供 prompt 明确"这是本场主角"
    if mk:
        _st = (sess.get("all", {}).get(mk) or {})
        F["mk_state"] = _st.get("state")
        F["mk_pre"] = (_st.get("state") == "pre")
    # ⚠️ V1.9.13 AH 并行交易：A股（9:30–15:00）与港股交易时段高度重叠，旧口径每轮
    #   只挑一个 active 市场（cn 优先）→ 重叠时段 H股被完全忽略，解说流看不到港股。
    #   修复：主角是 A股 且 H股同时 open/pre 时，标 parallel_hk，prompt 额外注入港股
    #   数据并允许模型带一嘴港股（指数+龙头）。H股单独开市（如 A股休市港股开）时
    #   active 本就是 hk，自然整场讲港股，无需此标记。
    _hk_st = (sess.get("all", {}).get("hk") or {})
    F["parallel_hk"] = bool(mk == "cn" and _hk_st.get("state") in ("open", "pre"))

    # V1.9.8：载入上一轮（及更早）已写好的滚动历史，作为本轮反重复上下文。
    #   注意：本轮此刻尚未写入历史，故这里拿到的是真正的「上一轮」。
    F["prev_rounds"] = load_history()

    log("=== 解说生成 · engine=%s · %d 个人设 ===" % (a.engine, len(personas)))
    if mk is None:
        log("目标市场：无开市市场 → 本场做**全球资讯解读**（不碰任何单一市场行情）"
            "· 可用数据：走马灯 %d 条 + 外媒快讯 %d 条" % (
                len(F.get("global") or []), len(F.get("news") or [])))
    else:
        log("目标市场 %s（开市中）· 可用市场 %s" % (F["mkt_name"], "、".join(avail) or "-"))
        if mk == "cn":
            log("事实卡：涨停%s 跌停%s 涨%s跌%s" % (
                F["zt"], F["dt"], (F["breadth"] or {}).get("up"), (F["breadth"] or {}).get("down")))
        else:
            log("事实卡：%s指数 %d 条 + 外媒快讯 %d 条%s" % (
                F["mkt_name"], len((F.get("markets") or {}).get(mk) or []), len(F.get("news") or []),
                (" + 港股个股 %d 只" % len(F.get("hk_stocks") or []))
                if (mk == "hk" and F.get("hk_stocks")) else
                "（该市场无个股数据，个股/板块/涨跌停整块跳过）"))

    os.makedirs(a.outdir, exist_ok=True)
    for i, p in enumerate(personas):
        if a.engine == "local":
            segs = synth(F, p)
            src = "local-synth"
        else:
            if not a.key:
                log("[skip] %s 无 key" % p["name"])
                continue
            # 免费档频率保护：两次 LLM 调用之间按 60/rpm 留间隔，防突发超频被封
            if i > 0:
                gap = 60.0 / max(1, getattr(a, "rpm", 10))
                log("[rate] 频率保护：等 %.1f s（rpm=%d）" % (gap, getattr(a, "rpm", 10)))
                time.sleep(gap)
            segs = llm_segments(F, p, a)
            src = a.model
        if a.engine == "api":
            # ⚠️ V1.9.11：中文读数 → 阿拉伯数字 兜底（确定性，先于一切校验）。
            #   模型先验太强、单条 prompt 拦不住，必须在代码层强制；
            #   跑在 _fix_market_pct 之前，让它那个 `\d+%` 真值校验能真正生效。
            segs = cn_num_to_arabic(segs)
            segs = _fix_relative_dates(segs, F)
            # ⚠️ V1.9.0：行情百分比**代码校验**（见 _fix_market_pct 注释与实现）。
            #   实测抓到过prompt 里明明写「恒生科技 +0.94%」、模型转写成「1.94%」
            #   ——纯转写变形，prompt 禁令拦不住（它并不"编造一个新数字"，只是写错）。
            segs = _fix_market_pct(segs, F)
            # ⚠️ V1.9.2：盘前"上一交易日收盘价"被播成"现在在涨"的词面兜底。
            segs = _fix_stale_quote(segs, F)
            # ⚠️ V1.9.1：财务指标口径兜底（源"net margin"被讲成"毛利率"——数字对、
            #   事实错，且对账查不出来）。
            segs = _fix_metric_name(segs)
            # ⚠️ V1.9.1：剔除垃圾段（markdown 残留 / 纯免责元叙述 / 预测句式）。
            #   盘前场次数据少，模型"没料"就转向预测+免责声明（实测第 9/10 段）。
            segs = _strip_junk_segs(segs)
            # refs 从 AI 正文里反向抽取（AI 引擎不经过 local 合成器那条挂 refs 的路，
            # 以前恒为空 → 页面标签完全不出现。V1.8.6）
            segs = attach_refs(segs, F)
        # ⚠️ V1.8.3：api 失败时 segs=[]，**绝不写文件、也绝不退回本地模板**。
        #   若照写，build_live.py 会把上一轮的旧 JSON 当成本轮新产物端上页面 ——
        #   看起来在正常更新，实际是陈旧内容（师傅要求「不要任何合成的内容」，
        #   也不接受"看起来是新的旧内容"）。宁可不写，让页面显式报缺。
        if not segs:
            log("[x] %s 本轮无有效产出，**不写文件**（旧文件若存在属上一轮，"
                "build_live.py 会因引擎/新鲜度闸拒它上页）" % p["name"])
            continue
        out = {
            "persona": p["key"], "name": p["name"], "short": p["short"],
            "desc": p.get("desc", ""), "engine": src, "date": F["date"],
            "market": mk, "market_name": F["mkt_name"], "is_open": F["is_open"],
            "segments": segs,          # 最新一轮（渲染层读这个，契约不变）
        }
        fp = os.path.join(a.outdir, "commentary_%s.json" % p["key"])
        out["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # V1.9.7：解说词就是「当下这一轮」。循环每轮整体覆盖 commentary_*.json，
        #   页面读顶层 segments 实时播出 —— 直播即当下，不累积历史回放。
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        # V1.9.8：本轮写入滚动历史（供页面接续流 + 下一轮反重复）。
        #   写入在 llm_segments 之后 —— 下一轮 prompt 才会把它当「上一轮」看到。
        append_history(dict(out))
        log("[ok] %-8s %d 段 → %s" % (p["name"], len(segs), os.path.basename(fp)))


# ====================================================================
# 本地合成器（零依赖）
# ====================================================================
def _seg(t, text, tag, ev=None, refs=None):
    """一段解说。refs = 正文提到的标的标签 [{code,name,pct,price,kind}]，
    渲染在正文末尾（时间 + 正文 + 标签）。tag/ev 保留作诊断，页面不再显示。"""
    return {"t": t, "text": text, "tag": tag, "ev": ev or {},
            "refs": [r for r in (refs or []) if r]}


# ====================================================================
# 标的标签（正文末尾的「名称 + ticker + 行情」）
# ====================================================================
# 指数 ticker 为公开通用代码（Bloomberg 口径）；表里没有的一律留空，
# 绝不猜代码——猜错代码比没有代码更糟（标签是给人核对行情的）。
IDX_TICKER = {
    # A 股
    "上证指数": "000001.SH", "深证成指": "399001.SZ", "创业板指": "399006.SZ",
    "科创50": "000688.SH", "沪深300": "000300.SH", "上证50": "000016.SH",
    "中证500": "000905.SH", "中证1000": "000852.SH", "国证2000": "399303.SZ",
    # 港股
    "恒生指数": "HSI", "恒生科技": "HSTECH", "恒生国企": "HSCEI",
    # 美股
    "道琼斯": "DJI", "纳斯达克": "IXIC", "标普500": "SPX", "纳斯达克100": "NDX",
    # 其他
    "日经225": "N225", "英国富时100": "FTSE", "德国DAX": "DAX",
    "法国CAC40": "CAC", "韩国KOSPI": "KS11", "澳洲标普200": "AXJO",
    "印度SENSEX": "SENSEX", "巴西Bovespa": "BVSP", "俄罗斯RTS": "RTS",
    # V1.8.6 补：行情源实际返回的指数名（恒生国企指数的准确名是"国企指数"，
    # 不是"恒生国企"；缺这一项会让 AI 正文里的"国企指数"挂不上标签）
    "国企指数": "HSCEI", "恒生国企指数": "HSCEI",
}

# V1.8.6 口语别名 → 标准名。AI 解说用简称（"恒指""英国富时""巴西"），
# 正文里出现的是简称，但行情源/IDX_TICKER 里是全称 —— 不做映射就挂不上标签。
IDX_ALIAS = {
    "恒指": "恒生指数", "恒生": "恒生指数",
    "恒科": "恒生科技", "恒生科技指数": "恒生科技",
    "国企": "国企指数", "国指": "国企指数",
    "上证": "上证指数", "沪指": "上证指数", "深成": "深证成指", "深指": "深证成指",
    "创业板": "创业板指", "科创": "科创50", "科创板": "科创50",
    "英国富时": "英国富时100", "富时": "英国富时100", "富时100": "英国富时100",
    "巴西": "巴西Bovespa", "日经": "日经225",
    "纳指": "纳斯达克", "纳斯达克综合": "纳斯达克", "道指": "道琼斯",
    "标普": "标普500", "德国DAX": "德国DAX", "法国CAC": "法国CAC40",
}

_ETF_PREFIX = ("51", "56", "58", "15", "16", "17")   # 沪/深 ETF / LOF 号段


def _a_suffix(code):
    """6 位 A 股代码 -> 交易所后缀。认不出就留空，不猜。"""
    c = str(code or "")
    if len(c) != 6 or not c.isdigit():
        return ""
    if c[0] == "6":
        return ".SH"
    if c[0] in ("0", "3"):
        return ".SZ"
    if c[0] in ("4", "8", "9"):        # 北交所（含 920 号段）
        return ".BJ"
    return ""


def _kind_of(code):
    return "etf" if str(code or "")[:2] in _ETF_PREFIX else "stock"


def _ref_stock(row, kind=None):
    """个股/ETF -> 标签。没有 code 就不给标签（宁缺勿造）。"""
    if not row:
        return None
    code = str(row.get("code") or "")
    if not code:
        return None
    return {"code": code + _a_suffix(code), "name": row.get("name") or "",
            "pct": row.get("pct"), "price": row.get("price"),
            "kind": kind or _kind_of(code)}


# V1.9.6 港股个股（龙头）→ 代码 + 口语别名。
# 用于：① 解说正文提到个股时挂可核对标签；② 校验正文里的个股涨跌幅（防转写变形）。
# 代码 = 新浪 5 位（rt_hk 用的就是它）；别名是 AI 口语（正文写「腾讯」要能命中「腾讯控股」）。
HK_STOCK_TICKER = {
    "腾讯控股": "00700", "阿里巴巴-W": "09988", "美团-W": "03690",
    "京东集团-SW": "09618", "小米集团-W": "01810", "比亚迪股份": "01211",
    "中国移动": "00941", "友邦保险": "01299", "网易-S": "09999",
    "香港交易所": "00388", "中国平安": "02318", "快手-W": "01024",
    "百度集团-SW": "09888", "中芯国际": "00981", "吉利汽车": "00175",
}
HK_STOCK_ALIAS = {
    "腾讯控股": ["腾讯控股", "腾讯"], "阿里巴巴-W": ["阿里巴巴", "阿里"],
    "美团-W": ["美团"], "京东集团-SW": ["京东集团", "京东"],
    "小米集团-W": ["小米集团", "小米"], "比亚迪股份": ["比亚迪股份", "比亚迪"],
    "中国移动": ["中国移动", "中移动"], "友邦保险": ["友邦保险", "友邦"],
    "网易-S": ["网易"], "香港交易所": ["香港交易所", "港交所"],
    "中国平安": ["中国平安", "平安"], "快手-W": ["快手"],
    "百度集团-SW": ["百度集团", "百度"], "中芯国际": ["中芯国际", "中芯"],
    "吉利汽车": ["吉利汽车", "吉利"],
}

# ⚠️ V1.9.6 港股个股标签（独立于 _ref_index：个股有 5 位代码，但**不是指数**，
#   不能塞进 IDX_TICKER —— 那个表的语义是"公开通用指数代码"，混进去会污染指数识别正则）。
def _ref_hk_stock(name, code, pct, price=None):
    if not name:
        return None
    return {"code": code or "", "name": name, "pct": pct, "price": price, "kind": "stock"}


def _ref_index(name, pct, price=None):
    tk = IDX_TICKER.get(name or "")
    if not tk:
        return None
    return {"code": tk, "name": name, "pct": pct, "price": price, "kind": "index"}


def _ref_sector(name, pct):
    """板块无独立 ticker（东财 gn_xxx 不是行情代码）→ 只给名称+涨幅。"""
    if not name:
        return None
    return {"code": "", "name": name, "pct": pct, "kind": "sector"}


def _clean_refs(*xs, cap=3):
    """去重 + 截断（一段最多 3 个标签，多了会糊）"""
    out, seen = [], set()
    for x in xs:
        if not x:
            continue
        key = (x.get("code") or "") + "|" + (x.get("name") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
        if len(out) >= cap:
            break
    return out


def _pct_voice(x):
    """把 +0.31% 说成嘴说的话。这是去 AI 味最关键的一步。"""
    if not isinstance(x, (int, float)):
        return "持平"
    a = abs(x)
    if a < 0.001:
        return "基本没动"
    if a < 0.3:
        return "微涨" if x > 0 else "微跌"
    if a < 1:
        return "涨了不到一个点" if x > 0 else "跌了不到一个点"
    if a < 3:
        return "涨%s个点" % round(a, 1) if x > 0 else "跌%s个点" % round(a, 1)
    return "涨%s%%" % round(a, 1) if x > 0 else "跌%s%%" % round(a, 1)


def synth(F, p):
    """本地合成器：事实卡 + 人设句式 + 气口，拼出有起伏的解说骨架。
    三市场通用：A 股有全市场维度（涨跌家数/榜/板块/涨跌停），港美股只有指数+快讯。"""
    rnd = random.Random(hash(p["key"]) & 0xffff)
    g = lambda: rnd.choice(GAS)
    rg = lambda: rnd.choice(RETAIL_GAS)
    b = F["breadth"] or {}
    up, dn = b.get("up"), b.get("down")
    zt, dt = F["zt"], F["dt"]
    k = p["key"]
    segs = []
    mk = F.get("mk", "cn")
    mkt = F.get("mkt_name", "A股")
    is_open = F.get("is_open", False)
    midx = (F.get("markets") or {}).get(mk) or []
    has_cn_deep = bool(up is not None)and mk == "cn"

    def date_cn(d):
        """20260930 -> 9月30日（不补零，念出来才像人话）"""
        if not d or len(str(d)) != 8:
            return "今天"
        s = str(d)
        return "%d月%d日" % (int(s[4:6]), int(s[6:8]))

    date_txt = date_cn(F["date"])
    lead = midx[0] if midx else {}
    lead_name = lead.get("name") or mkt
    lead_pct = lead.get("pct")
    lead_px = lead.get("price")
    lead_txt = ("%d点" % lead_px) if isinstance(lead_px, (int, float)) and lead_px > 1000 else mkt
    second = midx[1] if len(midx) > 1 else None
    ref_idx2 = _clean_refs(_ref_index(lead_name, lead_pct, lead_px),
                           _ref_index(second["name"], second.get("pct"), second.get("price")) if second else None)

    # ---------- ① 开场（叫出解说员本人 + 区分开市/休市） ----------
    nm = p.get("short") or p.get("name") or p["key"]
    if k == "pro":
        segs.append(_seg(0, "好，各位观众，我是%s。这里是%s的%s直播间。" % (
            nm, mkt, "现场" if is_open else "收盘复盘"), "开场", {"ev": "open"}))
    elif k == "talk":
        segs.append(_seg(0, "各位好，我是%s。今天咱们聊%s%s。" % (
            nm, mkt, "，不过现在全场休息，只能复盘" if not is_open else ""), "开场", {"ev": "open"}))
        segs.append(_seg(4, "别的市场休着，咱们开着。开盘之前，总得有人先说两句。" if not is_open
                         else "别的市场还睡着呢，咱们先开练。", "开场", {"ev": "open"}))
    else:
        segs.append(_seg(0, "%s兄弟们！我是%s！我刚扒完%s的盘，跟你们说个事儿……" % (rg(), nm, mkt),
                         "开场", {"ev": "open"}))

    # ---------- ② 比分（本市场主指数 + 次指数）----------
    if k == "pro":
        body = "先看比分。%s%s，%s。" % (lead_name, ("收在" + lead_txt) if lead_txt != mkt else "",
                                       _pct_voice(lead_pct))
        if second:
            body += "%s%s。" % (second["name"], _pct_voice(second.get("pct")))
            if (isinstance(lead_pct, (int, float)) and isinstance(second.get("pct"), (int, float))
                    and abs(lead_pct - second["pct"]) > 0.5):
                body += "谁强谁弱，这一下就分出来了。"
        segs.append(_seg(10, body, "比分", {"ev": "index", "mkt": mk}, refs=ref_idx2))
    elif k == "talk":
        segs.append(_seg(10, "%s%s，%s。" % (lead_name, lead_txt, _pct_voice(lead_pct)),
                         "比分", {"ev": "index", "mkt": mk}, refs=ref_idx2))
        segs.append(_seg(15, "我这只基金的净值跟它一比，显得我很努力。", "比分", {"ev": "index", "mkt": mk}, refs=ref_idx2))
    else:
        segs.append(_seg(10, "%s%s%s啊，%s。就…就还行吧。" % (rg(), lead_name, lead_txt, _pct_voice(lead_pct)),
                         "比分", {"ev": "index", "mkt": mk}, refs=ref_idx2))
        if second:
            segs.append(_seg(14, "%s%s。我没细看，不敢看。" % (second["name"], _pct_voice(second.get("pct"))),
                             "比分", {"ev": "index", "mkt": mk}, refs=ref_idx2))

    # ---------- ③ 全球盘面（走马灯的解说版：外盘怎么演） ----------
    gl = F.get("global") or []
    if gl:
        up_g = [x for x in gl if (x.get("pct") or 0) > 0]
        if k == "pro":
            segs.append(_seg(18, "先看外盘。%d 个市场里%d 个红盘，%s。%s%s。" % (
                len(gl), len(up_g), "外围不拖后腿" if len(up_g) * 2 >= len(gl) else "外围偏冷",
                (gl[0]["name"] if gl else ""), _pct_voice(gl[0].get("pct")) if gl else ""),
                "外盘", {"ev": "global"}, refs=_clean_refs(_ref_index(gl[0]["name"], gl[0].get("pct"), gl[0].get("price"))) if gl else None))
        elif k == "talk":
            segs.append(_seg(18, "外盘%d 红 %d 绿。%s%s，走势挺配合。" % (
                len(up_g), len(gl) - len(up_g), gl[0]["name"], _pct_voice(gl[0].get("pct"))),
                "外盘", {"ev": "global"}, refs=_clean_refs(_ref_index(gl[0]["name"], gl[0].get("pct"), gl[0].get("price"))) if gl else None))
        else:
            segs.append(_seg(18, "外盘%s！%d 个红盘！%s%s。咱们这边有戏唱吗？" % (
                rg(), len(up_g), gl[0]["name"], _pct_voice(gl[0].get("pct"))), "外盘", {"ev": "global"}, refs=_clean_refs(_ref_index(gl[0]["name"], gl[0].get("pct"), gl[0].get("price"))) if gl else None))

    # ---------- ④ 全场统计（仅 A 股有） ----------
    if has_cn_deep and isinstance(up, int) and isinstance(dn, int) and up + dn > 0:
        side = "红方" if up > dn else "绿方" if dn > up else "两边"
        if k == "pro":
            segs.append(_seg(24, "全场数据：%d家上涨，%d家下跌，%s占了上风。涨停%s家，跌停%s家。" % (
                up, dn, side, zt or 0, dt or 0), "全场统计", {"ev": "breadth"}))
        elif k == "talk":
            segs.append(_seg(24, "全场面：涨%d跌%d，%s人多。涨停%s，跌停%s。" % (up, dn, side, zt or 0, dt or 0),
                             "全场统计", {"ev": "breadth"}))
            segs.append(_seg(29, "你说这是A股，还是什么大型真人秀。我倾向于后者。", "全场统计", {"ev": "breadth"}))
        else:
            segs.append(_seg(24, "%s家涨，%d家跌啊！涨停%s家。" % (up, dn, zt or 0), "全场统计", {"ev": "breadth"}))
            segs.append(_seg(29, "%s我账户里涨停的有%s个。一比就想哭，不比就当没看见。" % (
                rg(), str(zt) if zt else "零个"), "全场统计", {"ev": "breadth"}))
    else:
        # 港美股无全市场数据 → 用指数内部涨跌替代「全场统计」的叙事位
        if len(midx) >= 2:
            wins = sum(1 for x in midx if (x.get("pct") or 0) > 0)
            if k == "pro":
                segs.append(_seg(24, "%s这几大指数，%d涨%d跌。%s" % (
                    mkt, wins, len(midx) - wins, "整体偏暖" if wins > len(midx) / 2 else "分歧不大"),
                    "全场统计", {"ev": "breadth", "mkt": mk}))
            elif k == "talk":
                segs.append(_seg(24, "%s三大指数，%d涨%d跌，平均成绩。%s" % (mkt, wins, len(midx) - wins,
                         "不出彩，也不出错。" if wins == len(midx) - wins else "有强有弱。"),
                         "全场统计", {"ev": "breadth", "mkt": mk}))
            else:
                segs.append(_seg(24, "%s%d个涨%d个跌啊！%s" % (rg(), wins, len(midx) - wins,
                         ("行吧，A股那边应该有肉吃。" if mk == "hk" else "港A那边现在应该在睡觉。")),
                         "全场统计", {"ev": "breadth", "mkt": mk}))

    # ---------- ⑤ 板块（仅 A 股；港美股无全市场板块数据 → 直接跳过） ----------
    if mk == "cn" and F["sec_top"]:
        t0 = F["sec_top"][0]
        t1 = F["sec_top"][1] if len(F["sec_top"]) > 1 else None
        line = "%s领跑，%s" % (t0["name"], _pct_voice(t0["pct"]))
        if t1:
            line += "；%s跟上，%s" % (t1["name"], _pct_voice(t1["pct"]))
        if k == "pro":
            segs.append(_seg(34, "%s。这两块，是今天的主战场。" % line, "板块", {"ev": "sector"}, refs=_clean_refs(_ref_sector(t0["name"], t0["pct"]), _ref_sector(t1["name"], t1["pct"]) if t1 else None)))
        elif k == "talk":
            segs.append(_seg(34, "概念板块方面，%s。" % line, "板块", {"ev": "sector"}, refs=_clean_refs(_ref_sector(t0["name"], t0["pct"]), _ref_sector(t1["name"], t1["pct"]) if t1 else None)))
            segs.append(_seg(39, "翻译一下：钱去%s了，走得很果断，跟我家外卖小哥一样。" % t0["name"], "板块", {"ev": "sector"}, refs=_clean_refs(_ref_sector(t0["name"], t0["pct"]), _ref_sector(t1["name"], t1["pct"]) if t1 else None)))
        else:
            segs.append(_seg(34, "%s%s！" % (rg(), line), "板块", {"ev": "sector"}, refs=_clean_refs(_ref_sector(t0["name"], t0["pct"]), _ref_sector(t1["name"], t1["pct"]) if t1 else None)))
            segs.append(_seg(38, "我重仓%s你敢信吗。不敢信吧，我也不敢。" % t0["name"], "板块", {"ev": "sector"}, refs=_clean_refs(_ref_sector(t0["name"], t0["pct"]), _ref_sector(t1["name"], t1["pct"]) if t1 else None)))

    # ---------- ⑥ 高光（A股用涨幅榜 / 港美股用领涨指数） ----------
    # ---------- ⑥ 高光 / 本场MVP（与页面「本场状况」同源） ----------
    # 港美股无个股榜 → 不走「涨幅榜/MVP」，改用本市场指数（见下方 else 分支）
    mvp = ((F.get("game") or {}).get("mvp") if mk == "cn" else None)
    t1 = (mvp or (F["up"][0] if F["up"] else None)) if mk == "cn" else None
    if t1:
        # t2 必须是「另一只」票：MVP 通常就是涨幅榜第一，取 up[0] 会指向自己 → 说"他排第二"
        t2 = None
        for x in F["up"]:
            if not t1.get("code") or x.get("code") != t1.get("code"):
                t2 = x
                break
        turn = ("换手%s%%" % round(t1["turn"], 1)) if t1.get("turn") is not None else None
        if k == "pro":
            body = "本场最佳球员：%s，%s" % (t1["name"], _pct_voice(t1["pct"]))
            if turn:
                body += "，%s" % turn
            if mvp and mvp.get("amount"):
                body += "，成交%.0f亿" % (mvp["amount"] / 1e8)
            body += "。这一场，全场最佳。"
            if t2:
                body += "%s紧随其后。" % t2["name"]
            segs.append(_seg(44, body, "高光", {"ev": "gainer", "code": t1.get("code")}, refs=_clean_refs(_ref_stock(t1), _ref_stock(t2))))
        elif k == "talk":
            segs.append(_seg(44, "本场最佳球员：%s%s。这数据我看着都替它紧张。" % (
                t1["name"], _pct_voice(t1["pct"])), "高光", {"ev": "gainer", "code": t1.get("code")}, refs=_clean_refs(_ref_stock(t1), _ref_stock(t2))))
            if t2:
                segs.append(_seg(49, "%s也想join MVP，但只能排第二。" % t2["name"], "高光",
                                 {"ev": "gainer", "code": t2.get("code")}, refs=_clean_refs(_ref_stock(t2))))
        else:
            segs.append(_seg(44, "%s本场最佳球员%s%s啊！！！" % (rg(), t1["name"], _pct_voice(t1["pct"])),
                             "高光", {"ev": "gainer", "code": t1.get("code")}, refs=_clean_refs(_ref_stock(t1), _ref_stock(t2))))
            if t2:
                segs.append(_seg(49, "%s也是MVP候选！%s！我天我天我天。" % (t2["name"], _pct_voice(t2["pct"])),
                                 "高光", {"ev": "gainer", "code": t2.get("code")}, refs=_clean_refs(_ref_stock(t2))))
            segs.append(_seg(53, "我的股票什么时候能这样。我不酸，我是真酸。", "高光",
                             {"ev": "gainer", "code": t1.get("code")}, refs=_clean_refs(_ref_stock(t1), _ref_stock(t2))))
    else:
        # 港美股：用本市场最强指数当高光
        best = max(midx, key=lambda x: (x.get("pct") or -99), default=None)
        worst = min(midx, key=lambda x: (x.get("pct") or 99), default=None)
        if best and best is not worst:
            if k == "pro":
                segs.append(_seg(44, "本场最佳：%s，%s。%s垫底，%s。" % (
                    best["name"], _pct_voice(best.get("pct")), worst["name"], _pct_voice(worst.get("pct"))),
                    "高光", {"ev": "gainer"}, refs=_clean_refs(_ref_index(best["name"], best.get("pct"), best.get("price")), _ref_index(worst["name"], worst.get("pct"), worst.get("price")))))
            elif k == "talk":
                segs.append(_seg(44, "%s%s，是今天唯一值得鼓掌的。%s就不提了。" % (
                    best["name"], _pct_voice(best.get("pct")), worst["name"]), "高光", {"ev": "gainer"}, refs=_clean_refs(_ref_index(best["name"], best.get("pct"), best.get("price")), _ref_index(worst["name"], worst.get("pct"), worst.get("price")))))
            else:
                segs.append(_seg(44, "%s%s%s！全场最佳！%s哭晕在厕所。" % (
                    rg(), best["name"], _pct_voice(best.get("pct")), worst["name"]), "高光", {"ev": "gainer"}, refs=_clean_refs(_ref_index(best["name"], best.get("pct"), best.get("price")), _ref_index(worst["name"], worst.get("pct"), worst.get("price")))))

    # ---------- ⑦ 低潮（A股用跌幅榜；港美股用最弱指数） ----------
    if mk == "cn" and F["down"]:
        d1 = F["down"][0]
        if k == "pro":
            segs.append(_seg(58, "另一边，%s%s，换手%s。%s这一场，输得没脾气。" % (
                d1["name"], _pct_voice(d1["pct"]),
                ("%s%%" % round(d1["turn"], 1)) if d1.get("turn") is not None else "—", d1["name"]),
                "低潮", {"ev": "loser", "code": d1["code"]}, refs=_clean_refs(_ref_stock(d1))))
        elif k == "talk":
            segs.append(_seg(58, "%s%s。跌成这样也是一种本事，我敬你。" % (d1["name"], _pct_voice(d1["pct"])),
                             "低潮", {"ev": "loser", "code": d1["code"]}, refs=_clean_refs(_ref_stock(d1))))
        else:
            segs.append(_seg(58, "%s%s…%s。" % (rg(), d1["name"], _pct_voice(d1["pct"])), "低潮",
                             {"ev": "loser", "code": d1["code"]}))
            segs.append(_seg(62, "我不看了，我关掉软件了，是真的。（两分钟后又打开）", "低潮", {"ev": "loser", "code": d1["code"]}, refs=_clean_refs(_ref_stock(d1))))
    elif midx:
        worst = min(midx, key=lambda x: (x.get("pct") or 99), default=None)
        if worst and (worst.get("pct") or 0) < 0:
            if k == "pro":
                segs.append(_seg(58, "再补一句，%s这边%s，是今天最安静的一块。问题不大，但也没人去。" % (
                    worst["name"], _pct_voice(worst.get("pct"))), "低潮", {"ev": "sector"}, refs=_clean_refs(_ref_index(worst["name"], worst.get("pct"), worst.get("price")))))
            elif k == "talk":
                segs.append(_seg(58, "%s%s。收盘了我看它一眼，为它鼓掌。它听不见，但它应该听见了。" % (
                    worst["name"], _pct_voice(worst.get("pct"))), "低潮", {"ev": "sector"}, refs=_clean_refs(_ref_index(worst["name"], worst.get("pct"), worst.get("price")))))
            else:
                segs.append(_seg(58, "%s%s…行吧。%s我不看它了，看了难受。" % (
                    worst["name"], _pct_voice(worst.get("pct")), rg()), "低潮", {"ev": "sector"}, refs=_clean_refs(_ref_index(worst["name"], worst.get("pct"), worst.get("price")))))

    # ---------- ⑧ 涨停 / 连板（仅 A 股） ----------
    if mk == "cn" and F["zt_pool"]:
        multi = [x for x in F["zt_pool"] if (x.get("boards") or 0) >= 2]
        if multi:
            m = multi[0]
            if k == "pro":
                segs.append(_seg(67, "%s%s连板，%s方向。这是今天唯一的连板高度。" % (
                    m["name"], m["boards"], m.get("sector") or "未知"), "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
                segs.append(_seg(71, "全场焦点就在这。别的都是浮云。", "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
            elif k == "talk":
                segs.append(_seg(67, "%s，%s连板，全场最高分。" % (m["name"], m["boards"]), "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
                segs.append(_seg(71, "我们管这叫唯一还有救的。", "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
            else:
                segs.append(_seg(67, "%s%s，%s连板！" % (rg(), m["name"], m["boards"]), "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
                segs.append(_seg(71, "我承认我酸了，但我主要是不服。", "连板", {"ev": "zt"}, refs=_clean_refs(_ref_stock(m))))
        else:
            z1 = F["zt_pool"][0]
            if k == "pro":
                segs.append(_seg(67, "涨停这边，%s开了个好头，%s。" % (z1["name"], z1.get("sector") or "题材"), "涨停", {"ev": "zt"}, refs=_clean_refs(_ref_stock(z1))))
            elif k == "talk":
                segs.append(_seg(67, "涨停有%s家，%s打头阵。这数字，说多不多说少不少，尴尬。" % (zt or 0, z1["name"]), "涨停", {"ev": "zt"}, refs=_clean_refs(_ref_stock(z1))))
            else:
                segs.append(_seg(67, "涨停%s家，%s领着。%s啊这日子有盼头了。" % (zt or 0, z1["name"], rg()), "涨停", {"ev": "zt"}, refs=_clean_refs(_ref_stock(z1))))

    # ---------- ⑨ 资金面（仅 A 股） ----------
    if mk == "cn" and F["active"]:
        a1 = F["active"][0]
        turn = ("换手%s%%" % round(a1["turn"], 1)) if a1.get("turn") is not None else "换手—"
        if k == "pro":
            segs.append(_seg(76, "资金面：%s成交%s，%s。这钱的味儿，跟它股价一个方向。" % (
                a1["name"], amount(a1["amount"]), turn), "资金", {"ev": "active", "code": a1["code"]}, refs=_clean_refs(_ref_stock(a1))))
        elif k == "talk":
            segs.append(_seg(76, "%s砸了%s，%s。" % (a1["name"], amount(a1["amount"]), turn), "资金",
                             {"ev": "active", "code": a1["code"]}, refs=_clean_refs(_ref_stock(a1))))
            segs.append(_seg(80, "这钱冲进去得有多想不开我不知道，但成交量它是真的。", "资金", {"ev": "active", "code": a1["code"]}, refs=_clean_refs(_ref_stock(a1))))
        else:
            segs.append(_seg(76, "%s成交额%s，%s。%s这就是我说的，有人在接。" % (
                a1["name"], amount(a1["amount"]), turn, rg()), "资金", {"ev": "active", "code": a1["code"]}, refs=_clean_refs(_ref_stock(a1))))

    # ---------- ⑩ 资讯（真实外媒快讯，呼应右侧「全球资讯」栏） ----------
    news = F.get("news") or []
    if news:
        n0 = news[0]
        src0 = n0.get("src", "")
        txt0 = n0["text"][:44]
        if k == "pro":
            segs.append(_seg(85, "插一条外媒快讯（%s）：%s" % (src0, txt0), "快讯", {"ev": "news"}))
            if len(news) > 1:
                segs.append(_seg(89, "再一条：%s这条，跟%s有关。" % (
                    news[1].get("src", ""), mkt), "快讯", {"ev": "news"}))
        elif k == "talk":
            segs.append(_seg(85, "插播一条外媒的（%s）：%s" % (src0, txt0), "快讯", {"ev": "news"}))
            segs.append(_seg(89, "这条要是真的，明天开盘热闹了。", "快讯", {"ev": "news"}))
        else:
            segs.append(_seg(85, "%s外媒快讯来了！%s：%s" % (rg(), src0, txt0), "快讯", {"ev": "news"}))
    # ---------- ⑪ 收尾钩子 ----------
    if k == "pro":
        if mk == "cn":
            tail = ("涨停家数比跌停多，故事就还有得讲"
                    if isinstance(zt, int) and isinstance(dt, int) and zt > dt else "这场的下半场，下个时段见")
        else:
            tail = "%s的盘，看的就这几杆大旗往哪边倒" % mkt
        segs.append(_seg(95, "好，%s这场的战报就先播到这。记住一句话：%s。" % (mkt, tail), "收尾", {"ev": "close"}))
    elif k == "talk":
        segs.append(_seg(95, "好，今天就到这。%s%s。" % (lead_name, lead_txt), "收尾", {"ev": "close"}))
        segs.append(_seg(99, "下期见——前提是你还活着。", "收尾", {"ev": "close"}))
    else:
        segs.append(_seg(95, "%s，今天就先到这，我%s了。明天开盘我再来。" % ("收工", "睡"), "收尾", {"ev": "close"}))
        segs.append(_seg(99, "%s你敢信我今天说了这么多。困了。晚安。" % rg(), "收尾", {"ev": "close"}))

    return segs
# ====================================================================
# 外部 LLM（--engine api）
# ====================================================================
# （V1.8.8 删除 _next_open_fact）
# 职责划分铁律：本直播里「确定说什么」是代码的活（确定性），「拿数据做解读」是 AI 的活（扩散性）。
# 「下一个开盘的市场/时间」属确定性信息——若要展示，由页面/代码层呈现，绝不进入 AI 的 prompt，
# 也不许 AI 自行判断、展望。AI 只在「系统已选定的本场市场 + 已给出的数据」范围内做口语化扩散。


# ====================================================================
# 滚动解说流历史（V1.9.8）
# 两个用途：① 页面据此前置「接续流」（一轮接一轮、覆盖旧的不整页重载）；
#          ② 喂给下一轮 LLM 当「上一轮已讲过」上下文，避免车轱辘话。
# 注意：这是数据层滚动窗口，**不是回放面板**（回放 UI 师傅已否决）。
# ====================================================================
HISTORY_PATH = os.path.join(HERE, "data", "commentary_history.json")
HISTORY_MAX_AGE = 3600      # 1 小时超龄脱落（与师傅「1 小时前不留」一致）
HISTORY_CAP = 16


def load_history():
    try:
        d = json.load(open(HISTORY_PATH, encoding="utf-8"))
        if isinstance(d, list):
            return d
    except Exception:                                            # noqa: BLE001
        pass
    return []


def append_history(round_obj):
    """把本轮追加进滚动历史，裁到最近 1 小时 / CAP 条。返回裁后列表。"""
    hist = load_history()
    hist.append(round_obj)
    now = time.time()
    kept = []
    for r in hist:
        ga = str(r.get("generated_at") or "")
        try:
            ts = time.mktime(time.strptime(ga, "%Y-%m-%d %H:%M:%S"))
        except Exception:                                        # noqa: BLE001
            ts = now
        if now - ts <= HISTORY_MAX_AGE:
            kept.append(r)
    if len(kept) > HISTORY_CAP:
        kept = kept[-HISTORY_CAP:]
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=1)
    except Exception:                                            # noqa: BLE001
        pass
    return kept


def build_prompt(F, p):
    """把事实卡渲染成 prompt。同一份事实卡，本地合成器与 LLM 共用 → 数据永远一致。

    ⚠️ V1.8.2 市场门控（实测踩到 · 严重）：早先无条件把 `F["index"]` 全量塞进 prompt，
      而它是 cn/hk/us 三市场混在一起的 dict → 港股场次的 prompt 里赫然写着
      「上证指数3842点 跌0.11%」，LLM 读到这些**真实数字**就当成今天 A 股实况开讲
      （实测段云飞那场直接播了 9/30 的科创50 跌 2.51%）。`cn_fresh` 闸只清了 A 股
      微观数据，**没闸指数** —— 于是「数据是真的，只是不是今天的、也不是这个市场的」。
      修法三条：① 指数只给目标市场那一份；② 显式点名其他市场今天休市/未开；
      ③ 节奏模板按市场给（港美股没有涨跌家数/涨停/板块，硬给 A 股模板等于逼它编）。"""
    lines = []
    # ⚠️ V1.8.4：mk 允许为 None ——「全场休市」时做**全球资讯解读**场次。
    #   师傅：「A股没开市不需要做什么复盘」「午间休市可以解说你拿到的全球资讯」。
    #   故不再回退 A股，也不再有"A股休市复盘"模板。
    mk = F.get("mk")
    mkt_name = F.get("mkt_name") or "全球"
    cn_stale = False          # 不再有"A股休市复盘"场次，此闸已随之作废
    # ⚠️ V1.8.8 职责划分（师傅明确要求）：代码负责一切确定性（选哪个市场、给什么数据、
    #   各市场状态），AI 只负责扩散性（拿确定的数据做口语化解读）。prompt 必须把这个边界
    #   讲死，否则 AI 会越界去"判断下一场 / 排时间表"。
    if mk is None:
        subj = ("全球市场速览（当前 A股/港股/美股均不在交易时段，故本场只解读全球行情"
                "快照与外媒快讯）")
    else:
        # ⚠️ V1.9.2 修：盘前标"（开市中）"是错的（state=pre，尚未开盘），
        #   而这是给模型看的**第一句**定性描述 —— 标错等于开场就给了错误前提。
        if F.get("parallel_hk"):
            subj = ("A股（开市中）+ 港股同步交易 —— AH 并行时段，本场在讲完 A股后"
                    "一并带过港股（指数 + 龙头）")
        else:
            subj = ("%s（盘前，尚未开盘）" % mkt_name if F.get("mk_pre")
                    else "%s（开市中）" % mkt_name)
    lines.append("【系统职责声明】本场解说的市场/主题已由系统**确定性选定**：%s。" % subj)
    lines.append("下面【真实数据】里的每一条，都是系统从行情源/资讯源**实际获取并核验**的。"
                 "你**不需要、也不允许**自行判断该说哪个市场、该不该提别的时段、下一个谁开盘。"
                 "你的职责是**只依据这些数据做口语化解读（扩散）**：把上面给的阿拉伯数字**原样**写进正文"
                 "（不要读成中文、不要自己换算、不要约算）、说感受、聊消息面。"
                 "系统没给你的信息，对你而言就是不存在。")
    lines.append("【真实数据（只能用这些，不许编造）】")
    # ⚠️ V1.8.7 补：全球速览场原先**一句日期都没有** —— 实测模型把当天 15:59 的港股收盘
    #   报价说成「最后报价停在**昨天**的下午三点五十九分」、把当天涨跌说成「昨天」。
    #   日期是事实的锚：不给它，口语人设就会顺手把"刚发生的事情"讲成昨日。
    _bj = str(((F.get("session") or {}).get("beijing_now") or ""))[:10]
    _ck = (F.get("session") or {}).get("checked_at") or ""
    try:
        _d0 = dt.date(*time.strptime(_bj, "%Y-%m-%d")[:3])
        if F.get("mk_pre"):
            # ⚠️ V1.9.2 修矛盾：盘前拿到的指数是**上一交易日收盘价**（实测报价距今14h），
            #   若还写「禁止说成昨天」，等于逼模型把收盘价说成"今天的"—— 那是把
            #   陈旧数据讲成实时，比"说昨天"更坏。**盘前要反过来禁**：不许说"今天涨了"。
            lines.append("【今天是】%d年%d月%d日（周%s）；本轮快照采集于 %s。"
                         "⚠️ 注意：下面【指数】是**上一交易日收盘价**（不是今天的实况，"
                         "今天还没开盘），**要讲就讲「上一交易日收在…」，"
                         "不许说成「今天涨了/跌了」「现在在涨」**。"
                         % (_d0.year, _d0.month, _d0.day, "一二三四五六日"[_d0.weekday()],
                            _ck or "—"))
        else:
            lines.append("【今天是】%d年%d月%d日（周%s）；本轮快照采集于 %s。"
                         "上面所有行情/快讯都是**这一轮**的，**禁止说成「昨天」「昨天下午」**"
                         "（实测口语人设会把当天 15:59 的收盘价讲成「昨天」）。"
                         % (_d0.year, _d0.month, _d0.day, "一二三四五六日"[_d0.weekday()],
                            _ck or "—"))
    except Exception:                                                # noqa: BLE001
        pass
    if mk is None:
        lines.append("⚠️ 当前 A股/港股/美股**都没有在交易**（休市或非交易时段）。")
        lines.append("   所以本场**不解读任何单一市场的行情**，只解读下面这些**全球市场实时涨跌**"
                     "与**外媒快讯**，当作一份全球市场速览。")
    # ① 指数只给「目标市场」那一份。
    #    ⚠️ 数据结构坑：F["index"] 的键是 sh/sz/cyb/kc50 —— **全是A股**，不含港美股；
    #    港美股指数在 F["markets"]["hk"|"us"] 里（各是 [{name,price,pct},…] 列表）。
    #    早先误以为 index 是 cn_*/hk_*/us_* 三市场合集而全量塞进 prompt，港股场次
    #    于是拿着「上证指数3842点」当今天实况播（实测播了 9/30 科创50 跌2.51%）。
    if mk == "cn":
        for kk, v in (F["index"] or {}).items():
            if isinstance(v, dict) and v.get("name"):
                lines.append("指数 %s：%s点%s" % (v["name"], v.get("price"), pct(v.get("pct"))))
    elif mk:
        # ⚠️⚠️ V1.9.2 实测 bug：盘前时段拿到的是**上一交易日收盘价**（实测道琼斯
        #   报价时刻= 北京 10-06 04:49 = 美东 10-05 16:49 收盘），而 prompt 只写了
        #   「+0.18%」没写时点 → 模型播成「道琼斯**现在** 51267.90 点，**涨了** 0.18%」。
        #   师傅批「还没开盘，这个涨幅哪条资讯来的，乱说了啊」—— 数字不是编的（确实来自
        #   行情源），但**时点语义是错的**：盘前没有"今早的涨幅"，那个涨幅属于上一交易日。
        #   修法两条：① 判定报价是否陈旧（对照快照采集时刻），陈旧就**显式标注时点**；
        #   ② 盘前**一律不给涨跌幅**（避免被当成本场实时涨跌），只给点位 + 收盘定语。
        # ⚠️ checked_at 只有**分钟**精度（实测 '2026-10-06 18:59'，16 字符），
        #   所以解析要兼容两种格式（带秒/不带秒）—— 否则时点判定永远失败、
        #   "陈旧报价"提示永不触发（第一次写这里就踩了）。
        _ck_dt = None
        for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                _ck_dt = dt.datetime.strptime(
                    str(((F.get("session") or {}).get("checked_at") or ""))[:19], _fmt)
                break
            except Exception:                                # noqa: BLE001
                continue
        if _ck_dt is None:
            # checked_at 不可解析时退回 beijing_now（它带秒）
            try:
                _ck_dt = dt.datetime.strptime(
                    str(((F.get("session") or {}).get("beijing_now") or ""))[:19],
                    "%Y-%m-%d %H:%M:%S")
            except Exception:                                # noqa: BLE001
                pass
        _stale = []
        for v in ((F.get("markets") or {}).get(mk) or []):
            if not (isinstance(v, dict) and v.get("name")):
                continue
            # ⚠️ V1.9.1：点位必须**代码格式化**。原先直接 `%s` 打原始 price，
            #   行情源给的是 `51267.8984`（4 位小数）→ 模型照搬念出
            #   「五万一千二百六十七点八九」（实测，荒谬精度）。行情源精度 ≠ 解说精度。
            _px = v.get("price")
            try:
                _px = "%.2f" % float(_px) if _px is not None else "—"
            except (TypeError, ValueError):
                _px = str(_px)
            # 陈旧判定：该指数报价时刻与快照采集时刻的间隔
            _age_min = None
            try:
                _q = dt.datetime.strptime(str(v.get("asof") or "")[:19],
                                          "%Y-%m-%d %H:%M:%S")
                if _ck_dt:
                    _age_min = int((_ck_dt - _q).total_seconds() // 60)
            except Exception:                                    # noqa: BLE001
                pass
            if F.get("mk_pre"):
                # 盘前：只给点位 + 明确"这是上一交易日收盘"，**不给涨跌幅**
                _tail = "（**上一交易日收盘价**，本场还没开盘，**没有今早的涨跌**）"
                if _age_min is not None:
                    _tail += "该报价距今约%d小时。" % max(0, _age_min // 60)
                lines.append("指数 %s：%s点%s" % (v["name"], _px, _tail))
                if _age_min is not None and _age_min > 60:
                    _stale.append(v["name"])
            else:
                _fresh = (_age_min is not None and _age_min <= QUOTE_FRESH_SEC // 60)
                _tag = "" if _fresh else ("（报价时间 %s，**已陈旧**，别当成刚发生的）"
                                          % str(v.get("asof"))[11:16])
                lines.append("指数 %s：%s点%s%s"
                             % (v["name"], _px, pct(v.get("pct")), _tag))
        if F.get("mk_pre") and _stale:
            lines.append("⚠️ %s 的点位是**上一交易日收盘价**（报价已过去数小时），"
                         "**绝对不许**说成「现在」「今天」在涨在跌 —— "
                         "要讲就讲「上一交易日收在…」。" % "、".join(_stale))
        # ⚠️ V1.9.6 港股个股（龙头，实时）。只在**港股本场**注入 ——
        #   指数是"大盘温度"，个股才是"今天谁在动"，港股场不给个股等于只有骨架。
        #   数据已按涨跌幅倒序（markets.py 排的），所以前几条就是当日最强/最弱。
        if mk == "hk":
            _hks = F.get("hk_stocks") or []
            if _hks:
                lines.append("")
                lines.append("【港股个股（龙头 · 实时，按涨跌排序）】")
                for _s in _hks:
                    _spx = _s.get("price")
                    try:
                        _spx = "%.2f" % float(_spx) if _spx is not None else "—"
                    except (TypeError, ValueError):
                        _spx = str(_spx)
                    lines.append("· %s（%s）：%s%s"
                                 % (_s.get("name") or "?", _s.get("code") or "—",
                                    _spx, pct(_s.get("pct"))))
                # ⚠️ V1.9.10 师傅反馈：「解说词里只是重复一下他们的涨跌幅数字，
                #   而且还用中文描述数字，显得毫无意义」。
                #   根因：这段旧指令让模型"逐个报价格+涨跌幅"，而**页面标签本来就会
                #   把正文提到的个股自动挂出来（名称 + 精确涨跌幅）** —— 正文再念一遍
                #   数字是纯冗余（读者眼睛已经在标签上看到了）。
                #   修法：正文不念数字，改为**讲结构**。标签负责"是多少"，解说负责"意味着什么"。
                #   ⚠️ 但仍要求点出个股名字 —— 标签是靠正文点名才挂得上的（attach_refs 按名匹配）。
                lines.append("   ⚠️ 只解说**这里列出的**个股，**不要提名单外的港股公司**"
                             "（快照里没有它们的行情，报了就是编造）。注意是港元计价。")
                lines.append("   ⚠️⚠️ **（作废级）不要把上面这些价格和涨跌幅逐个念出来**。"
                             "只要你提到某只票的名字，页面就会自动挂出"
                             "「名字 + 精确涨跌幅」的标签 —— 正文再念一遍数字，"
                             "观众等于被念了第二遍，纯冗余。**反面教材**："
                             "「网易涨了一点六二个点、京东涨了一个点、阿里跌了两个半点」"
                             "—— 这种逐条报数**一律作废**，标签上都有。"
                             "所以正文要做的是**归类与解读**，不是复述："
                             "哪一类在跌（比如互联网平台齐跌）、哪一类在扛、"
                             "分化还是普跌、和几分钟前比强弱有没有换手。"
                             "**名字要点**（点了标签才挂得上），但**数字交给标签**。"
                             "只有在数字本身构成强烈反差、值得单独强调时才点名一次具体幅度。")
    # ⚠️ V1.9.13 AH 并行：主角是 A股、H股同步开市 → 额外注入港股数据，
    #   允许模型播完 A股后单独用一段带一嘴港股（指数 + 龙头）。这段在 if/elif mk 链之后，
    #   故 mk=="cn" 也能拿到（上面 cn 分支只给 A股指数）。
    if F.get("parallel_hk"):
        _hki = (F.get("markets") or {}).get("hk") or []
        if _hki:
            lines.append("")
            lines.append("【港股（AH 并行 · 本场一并带过）指数】")
            for _v in _hki:
                if isinstance(_v, dict) and _v.get("name"):
                    _px = _v.get("price")
                    try:
                        _px = "%.2f" % float(_px) if _px is not None else "—"
                    except (TypeError, ValueError):
                        _px = str(_px)
                    lines.append("指数 %s：%s点%s" % (_v["name"], _px, pct(_v.get("pct"))))
        _hks = F.get("hk_stocks") or []
        if _hks:
            lines.append("【港股龙头（实时，按涨跌排序）】")
            for _s in _hks:
                _spx = _s.get("price")
                try:
                    _spx = "%.2f" % float(_spx) if _spx is not None else "—"
                except (TypeError, ValueError):
                    _spx = str(_spx)
                lines.append("· %s（%s）：%s%s" % (_s.get("name") or "?", _s.get("code") or "—",
                                                 _spx, pct(_s.get("pct"))))
            lines.append("   ⚠️ 港股用**港元**计价。点名个股即可（标签会自动挂名字+精确涨跌幅），"
                         "数字交给标签，不要逐条念涨跌幅。只讲这里列出的港股龙头，不提名单外的公司。")
        lines.append("⚠️ 本场 AH 并行：播完 A股后，请用**单独一段**带一嘴港股 —— "
                     "恒指/恒科方向 + 港股龙头里谁强谁弱（归类解读，不逐条报数）。")
    # ② 其他市场状态显式点名，防止模型"顺手"拿别国指数当本场实况
    # ⚠️ V1.9.1：**盘前场次连"它们休市/未开盘"都不许出现**。
    #   师傅原话：「你当前是美股盘前就只讲美股的东西，为何要提 AH 无关的」。
    #   根因：这段原本写成"以下市场当前没有实况，不要报它们的指数点位" ——
    #   **名义上禁了数字，实际上把"A股休市 / 港股未开盘"这两个状态送进了上下文**，
    #   模型照读就成了「A股呢今天假期休市…港股刚才收了…」（实测开头 3 句全是它）。
    #   → 盘前场**只留本场市场**，别家状态整条不出现（而不是"出现但禁提"）。
    sess_all = ((F.get("session") or {}).get("all") or {})
    off = []
    _pre = bool(F.get("mk_pre"))
    for k in ("cn", "hk", "us"):
        if k == mk:
            continue
        if k == "hk" and F.get("parallel_hk"):
            continue          # AH 并行：港股是与本场同台的，不是"别的市场"
        st = sess_all.get(k) or {}
        nm = {"cn": "A股", "hk": "港股", "us": "美股"}.get(k, k)
        if st.get("holiday"):
            off.append("%s（今日休市）" % nm)
        elif not st.get("open"):
            off.append("%s（未在交易时段）" % nm)
    if mk is None:
        pass          # 已在开头说明了；这里不重复"某个市场不能提"
    elif _pre:
        # 盘前：本场主角就是这个即将开盘的市场，**别家状态连提都不许提**
        lines.append("【本场只讲%s】现在是%s**盘前**阶段。" % (mkt_name, mkt_name))
        lines.append("   ⚠️ 上面【真实数据】里只有%s 相关的内容（指数/个股/资讯）。" % mkt_name)
        lines.append("   **绝对不要提其他市场** —— 不要说A股、不要说港股、不要说它们休市或"
                     "已收盘，一个字都不要提。开场直接讲%s 盘前在发生什么。" % mkt_name)
    elif off:
        lines.append("【本场只讲%s】以下市场当前没有实况，**不要报它们的指数点位**，"
                     "更不许把它们的数字当成本场数据：%s" % (mkt_name, "、".join(off)))
    b = F["breadth"]
    if b:
        lines.append("全市场：涨%d家 跌%d家 平%d家 涨停约%d家 跌停约%d家" % (
            b.get("up", 0), b.get("down", 0), b.get("flat", 0), b.get("limit_up", 0), b.get("limit_down", 0)))
    if F["zt"] is not None:
        lines.append("涨停池共%s只，跌停池共%s只" % (F["zt"], F["dt"]))
    if F["sec_top"]:
        lines.append("领涨板块：" + "、".join("%s%s" % (x["name"], pct(x["pct"])) for x in F["sec_top"]))
    if F["sec_bottom"]:
        lines.append("领跌板块：" + "、".join("%s%s" % (x["name"], pct(x["pct"])) for x in F["sec_bottom"]))
    for grp, lab in (("up", "涨幅榜"), ("down", "跌幅榜"), ("active", "成交额榜")):
        if F[grp]:
            lines.append(lab + "：" + "、".join(
                "%s%s%s" % (x["name"], pct(x["pct"]), ("（成交" + amount(x["amount"]) + "）" if x.get("amount") else ""))
                for x in F[grp]))
    if F["zt_pool"]:
        lines.append("涨停池（连板梯队）：" + "、".join(
            "%s%s连板（%s）" % (x["name"], x.get("boards") or 1, x.get("sector") or "?") for x in F["zt_pool"]))
    # ⚠️ V1.9.31 日内走势形态 + 美股广度（已采未用，现接入解说，回应"偏指数不解读走势"反馈）
    if not F.get("mk_pre"):
        _intra = (F.get("intraday_summary") or {}).get(mk)
        if _intra:
            lines.append("")
            lines.append("【%s 日内走势形态（来自分时 K 线，分钟级聚合，非逐笔）】" % _intra.get("name", mk))
            lines.append("· 开盘 %s / 最高 %s / 最低 %s / 当前 %s（日内振幅 %.2f%%）"
                         % (_intra["open"], _intra["high"], _intra["low"], _intra["close"], _intra["amp_pct"]))
            lines.append("· 走势形态：**%s**；前半段→后半段斜率 %.2f%%（正=后程走强，负=后程走弱）"
                         % (_intra["shape"], _intra["slope_pct"]))
            lines.append("   ⚠️ 据此讲**盘面结构**（如「早盘冲高后回落、目前在低位窄幅」），"
                         "开高低收数字交给页面标签/走势图，你讲形态意味着什么、多空谁占上风。")
    # ⚠️ V1.9.31：美股格局（DeanFi S&P500 广度）—— 美股无全市场 breadth 源，用现成数据补。
    if mk == "us":
        _ub = F.get("us_breadth")
        if _ub and _ub.get("advances") is not None:
            lines.append("")
            lines.append("【美股格局（S&P 500 广度 · DeanFi，数据日 %s）】" % (_ub.get("date") or "—"))
            lines.append("· 上涨 %d 家 / 下跌 %d 家；A-D 比 %.2f"
                         % (_ub["advances"], _ub["declines"], _ub.get("ad_ratio") or 0))
            lines.append("· 站上 200 日均线个股占比：%s%%"
                         % (_ub.get("above_200ma_pct") if _ub.get("above_200ma_pct") is not None else "—"))
            lines.append("   ⚠️ 据此讲美股内部**广度强弱**（普涨 / 分化 / 少数权重撑盘），"
                         "不要编涨跌家数以外的细节。")

    # 港美股场次的"外围环境"要有真数据可依：全球走马灯就是美股/欧股/商品的真实涨跌。
    # 不给它，节奏③「外围环境」就变成模型自由发挥的编造入口。
    # ⚠️ 但走马灯里**混着 A 股指数**（上证/深证/创业板…），而 A股 今天可能休市 ——
    #   不过滤就会又出现"禁止提A股"与"上证+0.31%"并列的自相矛盾（实测）。
    #   走马灯无市场字段，只能按名称白名单识别（与 markets.py 的指数名一致）。
    # 全球市场实时涨跌（走马灯）。V1.8.4：全球速览场（mk=None）下**全给**，
    # 因为「本场不解读单一市场」正是要靠这些数字说话；港美股场次仍过滤 A 股
    # （那场不允许提 A 股点位，否则与"只讲港股"自相矛盾）。
    if F.get("global"):
        cn_names = {"上证指数", "深证成指", "创业板指", "科创50", "沪深300", "中证500",
                    "中证1000", "国证2000", "上证50"}
        # ⚠️ V1.8.7：mk=None（全球速览）时**不能无条件全给** —— 走马灯里的 A 股指数在
        #   A股 休市日是**上一个交易日**的旧数（实测 9/30 的上证 +0.31% 混在"全球实时涨跌"
        #   里，等于拿旧数当今天播）。只有 A股 今天真交易过（traded_today）才保留它的指数。
        keep_cn = bool(mk is None and ((sess_all.get("cn") or {}).get("traded_today")))
        # ⚠️ V1.9.1 师傅指出：「你当前是美股盘前就只讲美股的东西，为何要提 AH 无关的」。
        #   ⚠️ 我第一版理解错了方向：以为"外围"就该把别国指数全留，于是港股恒生
        #   仍被当作"外围"念出来（第 4 段「外围市场上恒生涨了1个点」）→ 师傅的诉求
        #   仍未满足。**他要的是"盘前这场的正文里不许出现 A股/港股"**，而"外围市场"
        #   这个标签本身就在邀请模型聊别国（讲着讲着就变成报 A股/港股的涨跌了）。
        #   正确做法：**盘前场不给"外围市场"这个数据块**（本场市场自己的数据已足够，
        #   盘前指数报价本身就更该讲透）；"全球市场实时涨跌"仅留给 mk=None 的全球速览场。
        #   → mk 非None 时，恒生/日经/富时这类**一律不进 prompt**（不是"禁提"，是"不给"，
        #      与纪律 70 同理：名义上禁了、实际上数据还在上下文里，模型就会用）。
        #   mk=None（全球速览）时行为完全不变。
        _home = set()
        if mk:
            _home = {"cn": cn_names,
                     "hk": {"恒生指数", "恒生科技", "国企指数"},
                     "us": {"道琼斯", "纳斯达克", "标普500"}}[mk]
        gl, had_cn = [], False
        for g in (F.get("global") or []):
            if not isinstance(g, dict) or not g.get("name"):
                continue
            if g["name"] in cn_names:
                if not keep_cn:
                    had_cn = True          # 只是"有但不给"，与"本来就没有"要分开说
                    continue
            gl.append(g)
            if len(gl) >= 8:
                break
        if mk:
            lines.append("⚠️ 本场**只给上面【指数 %s】那几条**用，外围别国指数**本场不提供**"
                         "（不是禁提而是没有给）—— 你看不到就不会提，也不许自己补。"
                         % mkt_name)
        elif gl:
            lines.append("全球市场实时涨跌（真实，可直接引用）：" + "、".join(
                "%s %s" % (g["name"], pct(g.get("pct"))) for g in gl))
        # ⚠️ V1.9.2：单市场场次（mk≠None）**不出现这条A股 提示** ——
        #   本场只讲美股，提示里却有"A股今日没有交易"，等于又把A股 送进上下文
        #   （与纪律 70「禁提要靠不给」同理）。只有全球速览场（mk=None）才需要它。
        if had_cn and mk is None:
            lines.append("⚠️ A股今日没有交易，**它的指数不在这里**（上面看不到 A股 指数 = "
                         "不要点评 A股 任何点位，更不许拿假期前的旧数字当今天）。")
    if F.get("news"):
        # 来源名（[SeekingAlpha]/[CNBC]…）**不要**写进 prompt —— 模型会连着念出来，
        # 变成「[SeekingAlpha] Trump says...」这种机器感极强的一句（实测）。
        # 想要"有据可查"就在前面统一说明来源域即可。
        # ⚠️ V1.9.36：① 由 [:5] 放宽为**全量 8 条** —— 按场次配额选出的中媒快讯若排在第
        #   6~8 位，只喂 5 条等于配额白做；② 措辞由"外媒快讯"改"本轮快讯"（现在混着
        #   东财快讯等中文源，"外媒"是错的标签，且会诱导模型只挑英文的说）。
        lines.append("本轮快讯（共 %d 条，媒体名已省略，内容可直接使用）：" % len(F["news"])
                     + "｜".join((n.get("text") or "")[:70] for n in F["news"]))
        lines.append("⚠️ 引用快讯时**不许照抄英文原文**，要用中文说清楚它在讲什么"
                     "（可以保留专有名词，如 Nasdaq、OPEC、World Bank）；"
                     "**中文快讯直接用其要点**即可。**不许出现方括号媒体名**"
                     "（[SeekingAlpha]、[CNBC] 这种）。")
    # ⚠️ V1.9.32 C 方向：快讯 ↔ 板块 轻量关键词关联钩子（只在有关联时给，避免噪声）
    if F.get("news_sector_links"):
        lines.append("")
        lines.append("【快讯 ↔ 板块 关联提示（关键词线索，仅供参考因果，勿过度引申）】")
        for _lk in F["news_sector_links"]:
            _secs = "、".join("%s%s(%s)" % (s["name"], pct(s["pct"]), s["kind"]) for s in _lk["sectors"])
            lines.append("· 快讯「%s…」→ 关联板块：%s" % (_lk["snippet"], _secs))
        lines.append("   ⚠️ 讲领涨/领跌板块时，若点出了上面的关联，请说明**快讯与板块的因果**"
                     "（如「半导体领涨，对应英伟达业绩这条快讯」）；"
                     "但关联是关键词线索、非确定性，扯不上就别硬凑。")
    # ⚠️ V1.9.34：RRG 跨资产轮动状态（只读 yangxiaa.cc/rrg/，data/rrg_state.json）
    #   仅作「全球资金/风险偏好」背景，不解释 A股板块涨跌（RRG 跨资产、不含 A股行业）。
    if F.get("rrg") and F["rrg"].get("available"):
        _r = F["rrg"]
        _qc = _r.get("counts") or {}
        _qc_s = "、".join("%s%d" % (k, v) for k, v in _qc.items()) if _qc else "—"
        lines.append("")
        lines.append("【跨资产轮动状态（RRG 看板，数据截止 %s）】" % (_r.get("data_date") or "—"))
        lines.append("· 象限分布：%s" % _qc_s)
        if _r.get("overheated"):
            lines.append("· 过热/回避：%s" % "、".join(_r["overheated"]))
        if _r.get("risk_read"):
            lines.append("· 风险偏好判读：%s" % _r["risk_read"])
        lines.append("   ⚠️ 这是**跨资产（美股/黄金/比特币/美债…）**轮动状态，不含 A 股行业；"
                     "讲到「全球资金流向 / 风险偏好 / 外围与 A股背离」时**可主动引用**（例：「跨资产看，标普500、纳指已过热回避，"
                     "A股相关的沪深300、创业板仍处弱势，资金在往避险方向轮动」）；但**不要拿它解释某个 A 股板块的涨跌原因**；"
                     "且是日级数据，不要当成实时信号。")
    # ---- 经济数据官方发布日历（V1.9.0 · eulerpool）----
    if F.get("econ_cal"):
        cal = F["econ_cal"]
        lines.append("经济数据官方发布日程（**只有事件名和日期，任何数值都没有**）："
                     + "；".join("%s %s" % (c.get("date", "")[5:] or "—", c.get("name") or "")
                                 for c in cal[:8]))
        lines.append("⚠️ 关于经济数据：**只说「什么时候发什么」，绝对不许报任何数字** —— "
                     "不许说预期多少、不许说前值多少、不许说市场怎么反应。"
                     "你没有这些数据（上面给的是日程表，不含数值）。"
                     "这是硬禁令，违反就算整段作废。")
    lines.append("")
    lines.append("【任务】")
    lines.append("按下面的顺序输出 **6~10 段**连续的话，每段 25~70 字，段间用空行分隔。")
    # ⚠️ V1.8.5 师傅指出：「人设与解说词、市场信息三者不匹配」——
    #   原话「现在这球赛，恒指涨了0.73%」—— **体育比喻泄漏进正文**。
    #   根因：① prompt 里写「像真球赛一样有起伏」把它往下带；② 人设给了
    #   「上半场结束」「这一球漂亮」这类现成锚点，模型直接搬。
    #   修法：**借现场感、不借体育**。user 与 system 双侧明令禁体育词。
    lines.append("⚠️ 这是**市场直播**，不是体育赛事直播。说话像真人对着屏幕另一头聊天。")
    lines.append("**绝对不许出现体育比喻**：这球 / 上半场 / 下半场 / 比分 / 球员 / 进球 / 开赛 / "
                 "这一球漂亮 / 战报 / MVP。你借的是现场感与节奏，不是体育本身。")
    # ⚠️ V1.9.0 实测抓到两个新泄漏（都是 eulerpool 个股新闻带进来的）：
    #   ① **markdown 残留**：prompt 里我方通篇用 `**加粗**` 做强调，模型学到后
    #      连正文也吐星号 —— 实测输出「…835万美元，*（这句我不懂矿，别较真）*」。
    #   ② **元叙述/自我辩解**：碰到不熟悉的标的，模型会跳出来跟观众解释自己
    #      「这句我不懂矿，别较真」—— 直播口吻里这是致命的（解说不懂自己刚报的数）。
    #   修法：一条式禁 markdown 符号 + 一条式禁元叙述（都点死具体形态）。
    lines.append("**正文里绝对不许出现任何 markdown 符号**：不要 * ** # ` - 开头，"
                 "不要 ~~删除线~~，不许用星号强调。你说话就是说话，不是写文档。")
    lines.append("**绝对不许元叙述/自我辩解**：不许出现「这句我不确定」「我不了解这个行业」"
                 "「别较真」「我记不清了」这类向观众解释你自己状态的话。"
                 "遇到不熟悉的标的，就只说它客观上发生了什么（做了什么、涨跌如何），"
                 "不许承认自己不懂、也不许请观众原谅——你是解说，不是记者会后感。")
    # ③ 内容顺序按市场给（三态）。**A 股休市复盘模板已在 V1.8.4 删除** ——
    #    师傅：「A股没开市不需要做什么复盘」——复盘上一交易日既误导又易编造。
    if mk is None:
        # 职责：状态事实可以照读（谁休市/已收盘/盘前），但「下一场开谁、几点」一律不进
        # prompt —— 那是确定性信息，若要展示由页面/代码层负责，AI 禁止展望、禁止排时间表。
        st_line = []
        for k, nm in (("cn", "A股"), ("hk", "港股"), ("us", "美股")):
            st = sess_all.get(k) or {}
            state = st.get("state")
            if st.get("holiday"):
                st_line.append("%s 今日假期休市（无交易）" % nm)
            elif state == "open":
                st_line.append("%s 正在交易中" % nm)
            elif state in ("pre", "post"):
                # 盘前/盘后 ≠ 已收盘：美股 traded_today=True 但常规时段还没开，状态逐档分开给。
                st_line.append("%s %s（常规时段未开始，不是「正在交易」）"
                               % (nm, st.get("status") or "非交易时段"))
            elif st.get("traded_today"):
                st_line.append("%s 今日已收盘（最后报价 %s）" % (nm, st.get("asof") or "—"))
            else:
                st_line.append("%s %s（尚未开盘）" % (nm, st.get("status") or "未在交易"))
        lines.append("各市场当前状态（事实，照读即可）：" + "；".join(st_line) + "。")
        lines.append("⚠️ 任何市场的开收盘时间你都不知道（上面没给过）—— 不许写「09:30 开盘」"
                     "「下午三点收盘」这类安排，不许给任何市场排时间表，"
                     "更不许告诉观众「下一个开盘的是 X」或「X 马上要开」。")
        lines.append("内容顺序：① 若上面有「上一轮口播」，**先用一句接住上一轮收尾的话头**"
                     "（自然接续上一轮最后说的那点事，例如「接着刚才说的 XX…」），"
                     "再开口说清现在没有市场在盘中（按上面状态说谁休市、谁已收盘、谁盘前）"
                     " ② 挑几个跌得最狠/涨得最猛的念 ③ 挑 2~3 条快讯说说在讲什么"
                     "（优先国内 / 产业类的，再补外媒的）"
                     " ④ 你的感受（不预测、不展望下一个开盘） ⑤ 收一句留钩子"
                     "（钩子可以是「今天就先翻到这儿」之类，**不许用「等下看XX开盘」当钩子**）。")
    # ⚠️ V1.9.0 新增铁律 8：经济数据**只报事件，不报数字**。
    #   起因：接了eulerpool 的经济发布日历，它**只有"何时发什么"**（实测连
    #   /history 端点的 actual/prev 也拿不到）。若不在prompt 里点死，模型面对
    #   「FOMC Press Release」这类条目极易顺口编出「市场预期降息 25 个基点」
    #   ——这是最典型、最难自察的编造（因为它"听起来太合理"）。**禁令要按
    #   "算子/数据类别"枚举**，光说"不许编造"盖不住"日程表→预期值"这条推导。
    lines.append("8. **经济数据只说事件，不报任何数字。** 上面「经济数据官方发布日程」"
                 "只告诉你什么时候发什么，**不含数值** —— 不许说预期、前值、"
                 "实际值、市场反应，更不许推断「降息/加息多少」。"
                 "只能说「今晚有 FOMC 纪要要发」这类**事件性**表述。")
    if mk is None:
        lines.append("⚠️ 当前没有单一市场处于交易时段，本场是**全球市场速览**：上面「全球市场"
                     "实时涨跌」里的数字是行情源的当前报价，可以直接引用，但要说明是「全球速览」、"
                     "不要假装成某个市场在盘中。绝对不许编造上面没有的数字、涨跌家数、涨停数、"
                     "成交额，不许点评任何个股，不许展望下一个开盘（尤其不许写「美股今晚21:30开」"
                     "「等X开盘再看」—— 你只讲当下的全球速览，讲完即止）。")
    else:
        if F.get("mk_pre"):
            # ⚠️ V1.9.1：盘前场次的内容顺序单开。实测发现收尾模型会写
            #   「嗯，盘前就聊到这儿，**咱们等开盘见**」—— 这是**变相预告开盘**
            #   （等价于"下一个开盘的是美股21:30"），踩了铁律 7 的精神。
            lines.append("内容顺序：① 若上面有「上一轮口播」，**先用一句接住上一轮收尾的话头**"
                         "（自然接续上一轮最后说的，例如「接着刚才的盘前情绪…」），"
                 "再说清现在讲的是**%s盘前**（还没开盘，"
                 "你在讲开盘前的定价和消息，不是盘中实况）"
                 " ② **一句话概括%s整体强弱量级**（如「主要指数小涨、幅度都在零点几个百分点」），"
                 "**不要逐个念每条指数数字**（页面标签有精确值，念一遍是冗余）；"
                 "若上面给了【走势形态】，用一两句讲盘面结构（早盘怎么走、现在多空谁占上风）；"
                 "若给了【格局/主线】，点出广度情绪或领涨主线 ③ **快讯挑 2~3 条说**"
                 "（优先挑与本市场直接相关的国内消息，再补一条外媒的）"
                 " ④ 你的感受（不预测）⑤ 收一句留钩子。"
                         % (mkt_name, mkt_name))
            lines.append("⚠️ 收尾**不许用「等开盘见」「等开盘再看」「盘前就聊到这儿」**这种"
                         "预告开场的钩子 —— 你只讲当下盘前，讲完即止。"
                         "钩子要是你对盘前情绪的判断或一句留问，不是对未来的安排。")
        else:
            lines.append("内容顺序：① 若上面有「上一轮口播」，**先用一句接住上一轮收尾的话头**"
                         "（自然接续上一轮最后说的，例如「说到%s的 XX 走势…」），"
                 "再开口说清今天在看%s ② **一句话概括整体强弱量级**（如「指数小涨、但个股分化明显」），"
                 "**不要逐个念每条指数数字**（页面标签有精确值）；"
                 "重点讲上面给的【走势形态】（盘面结构/多空）/【格局】（涨跌家数或美股广度）/【主线】（领涨板块），"
                 "这些比指数幅度更能说明今天盘面 ③ 外围市场（**只提上面明确写了名字的**，其余一律不提）"
                 " ④ **快讯挑 2~3 条说**——本场是 %s 场，**优先挑与本市场直接相关的**"
                 "（国内板块 / 公司 / 政策 / 产业事件那一类），再补一条外媒的；"
                 "若上面给了【快讯 ↔ 板块 关联提示】，讲那条快讯时就把**因果**说出来"
                 "（如「XX 板块今天领涨，对应的就是上面那条快讯」）"
                 " ⑤ 你的看法（不预测） ⑥ 收一句留钩子。" % (mkt_name, mkt_name, mkt_name))
    # ⚠️ V1.9.36 修一处自相矛盾的死禁令（师傅反馈「还在瞄着指数和个股报」的真凶之一）：
    #   原文**无条件**往每个场次灌「%s没有全市场个股数据源，绝对不许编造涨跌家数、
    #   涨停家数、板块涨幅」—— 而 A股 场次上面明明给了涨跌家数/涨跌停/板块榜/涨停池。
    #   结果是模型一边拿到 A股 微观数据、一边被告知"你没有这些数据、不许提"，
    #   于是**绕开板块与广度，退回去念指数和个股涨幅**（正是师傅观察到的现象）。
    #   修法：A股 盘中场次改**正向要求**（有数据、必须用，只禁上面没给的）；其余场次
    #   维持原禁令（港美股确实只有指数+资讯）。
    if mk == "cn" and not F.get("mk_pre"):
        lines.append("⚠️ 本场是 A股 场次，**你手上就有完整的全市场微观数据**（涨跌家数、涨跌停家数、"
                     "涨跌幅榜、成交额榜、行业板块领涨/领跌、涨停池）—— 这些**必须用**，"
                     "它们才是一天盘面的主体，指数只作背景。"
                     "只禁**编造**：上面没给的数字（如北向资金、融资余额、两市总成交额）一律不许出现。")
    else:
        lines.append("⚠️ %s没有全市场个股数据源，**绝对不许**编造涨跌家数、涨停家数、板块涨幅、"
                     "个股涨跌幅、成交额。这些一律不提，只聊上面列出的指数和资讯里真实出现的内容。"
                     "数据不够就聊消息面和你的感受，这是真实市场直播的常态。" % mkt_name)
    lines.append("⚠️ **指数日内振幅通常远小于个股和板块**—— 单讲指数涨跌毫无信息量。"
                 "本场解说重心必须是**盘面结构（走势形态）、广度格局、领涨主线、以及和快讯的因果关联**，"
                 "而不是把指数数字念一遍。上面【走势形态/格局/主线】已给数据，把它们讲透，"
                 "指数只用一句话带过强弱量级即可。")
    lines.append("严格按你收到的人设指令写。记住铁律：只输出解说词，无标题无序号无解释。")
    # ⚠️ V1.8.4 师傅原话：「提示词里严格按照快照信息来解读转化，不要添加」。
    #   风格可以自由（那是人设的事），**事实不行** —— 下列每一条都算"添加"：
    #   快照里没有的数字/个股/板块/家数/成交额；快照没提的因果与预测；
    #   快照没给的"明日/后市怎么看"。要一句话说完一条信息就说完，说完接下一条。
    lines.append("")
    lines.append("【最高铁律 · 只解读，不添加】")
    lines.append("1. **只能使用上面【真实数据】里出现的信息**。上面没写的数字、个股、板块、"
                 "家数、成交额、涨跌幅，一律不许出现。"
                 "**没有基准的对比同样是编造** —— 实测写出「融资扩到 835 万美元，扩了一倍多」"
                 "（快照里只有「最高 835 万美元」这一个数，没有原额度，倍数无从谈起）；"
                 "「比上个月」「较昨日」这类比较，只要基准不在上面，就不许说。")
    lines.append("2. **可以轻度解释与推断，但必须带不确定口吻**。数据只给了涨了跌了，"
                 "你可以讲「这说明了什么／可能反映了什么」，但要用「可能／看起来／疑似」这类措辞；"
                 "**不许下笃定的因果结论**（凭空说「因为…」「资金在回流」「情绪驱动」"
                 "——快照里没有，属编造）。")
    lines.append("3. **可以轻度展望，但禁止笃定预期**。允许用「可能／或／有概率」做温和前瞻"
                 "（如「若…或延续」「短期可能…」），**不许写确定结论**（「明天会…」「后市必…」"
                 "「预计将…」这类不行）。各场次引导语里的「不预测」一律按本条放宽：可带不确定口吻轻度展望，但不笃定。"
                 "总基调：重解释、轻判断。")
    # ⚠️ V1.9.1：「外围挑涨跌大的」这句在盘前场次已经失效（外围数据块整条删了），
    #   留着会让模型去找一份不存在的数据 → 要么跳过、要么自己补（=编造）。
    #   修法：mk 非None（本场聚焦单一市场）时改口径为"指数逐个念+ 快讯挑有意思的说"。
    if F.get("mk"):
        lines.append("4. **段数不许缩水**。数据少也要把上面每一条都说到（走势形态 / 格局 / 主线 / 快讯），"
                     "指数只用一句话带过强弱量级即可；"
                     "其中**快讯那一段是硬性要求**（上面给了快讯就必须引用，不许跳过）；"
                     "其余长度**靠感受、看法、翻大白话来凑，不许重复同一句话**。"
                     "「信息不够」不是不说的理由 —— 换个角度说同一件事是可以的，"
                     "拿同一组数字翻来覆去讲三遍不行。")
    else:
        lines.append("4. **段数不许缩水**。数据少也要把上面每一条都说到（指数逐个**原样报、用阿拉伯数字**、外围挑涨跌大的、"
                     "快讯挑有意思的说、优先国内/产业类），但**靠感受、看法、翻大白话来凑长度，不许重复同一句话**。"
                     "「信息不够」不是不说的理由 —— 换个角度说同一件事是可以的，"
                     "拿同一组数字翻来覆去讲三遍不行。")
    lines.append("5. 表达可以花哨（那是你的风格），**事实必须和上面逐字对得上**。")
    # ⚠️ V1.9.10 师傅反馈：「用中文描述数字，显得毫无意义」。根因就在这条旧铁律里——
    #   它原先明写「口语化只允许改**单位读法**（24216.42 → 两万四千二百一十六点四二）」，
    #   等于**命令模型把数字转成中文读法**（那是为二期 TTS 准备的，但现在页面是纯文字，
    #   读者用眼睛扫，中文读法又长又难找数）。
    #   ⚠️⚠️ 更要命的副作用：中文读法会**绕过** _fix_market_pct 的真值校验 ——
    #   该校验的正则是 `\d+(?:\.\d+)?\s*%`，只认「数字+%」；模型写成「涨了百分之一点六七」
    #   时正则匹配不到，于是**这一段根本不进校验**，转写变形可以一路放行（已实测确认）。
    #   → 一律要求阿拉伯数字：既好读，也让代码兜底真正生效。
    #   （二期接 TTS 时，数字→读音的转换应放在 TTS 前置处理层，不该污染正文。）
    lines.append("6. **（作废级）数字一律用阿拉伯数字，不许写成中文读法**。"
                 "写「188.60」「+1.67%」「24148.88」，"
                 "**不许**写成「一百八十八块六毛」「百分之一点六七」「两万四千一百四十八点八八」"
                 "「两万四千一百三十一块四二」「两个点」。中文读数会被系统**强制改回阿拉伯数字**，"
                 "你写中文等于白写还容易出错。书面数字一眼就能扫到、能和下方标签对上，中文读法又长又难核对。"
                 "**数值本身不许换算、约算、挪位**：上面给了多少就写多少，"
                 "**不许**自己截断成「24216」、不许换算成「八毛左右」「涨了两三个点」。"
                 "**也不许做币种换算** —— 实测把新闻标题里的「$8.35M」自己折成"
                 "「大概六千万人民币上下」，汇率是你不知道的；美元金额只说美元。")
    # ⚠️ 时刻/时段 = 确定性信息，系统没给就是 AI 不知道的。V1.8.8 起彻底不展望下一个开盘，
    #   不再有任何"例外"（旧版曾把"下一场开市"算好塞进 prompt，本质仍是让 AI 主动提起，已删）。
    lines.append("7. **不许报时段/时刻，也不许展望下一个开盘**。上面没给的市场开收盘时间就是你"
                 "不知道的 —— 不要写「09:30 开盘」「下午三点收盘」这类安排，不要给任何市场排时间表，"
                 "更不要告诉观众「下一个开盘的是 X」或「X 马上要开」。"
                 "尤其不许写「美股今晚21:30开」「等港股开盘再看」这种 —— 哪怕你记得美股常规"
                 "21:30 开，那也是系统管的确定性信息，你无权替它预告。本场讲什么由系统已经定好，"
                 "你只解读已给的数据，讲完当下即止，不必安排、不必预告。")
    # ---- 滚动接续·承接（V1.9.8 起为「反重复」；V1.9.17 改为「承接优先」）：
    #   把上一轮已讲过的内容喂给模型，明确要求「开头先接住上一轮收尾的话头、
    #   再展开本轮，不冷重启；不许逐字照搬，但鼓励接着往前推、补新视角」。
    #   这是让"一轮接一轮"连续而非各自为政的关键；不是回放面板。
    prev = F.get("prev_rounds") or []
    if prev:
        _pr = prev[-2:]                      # 最近 1~2 轮足够抑制车轱辘话
        _blk = []
        for r in _pr:
            _segs = (r.get("segments") or [])
            _txt = " / ".join((s.get("text") or "") for s in _segs)
            _blk.append("  · %s（%s）：%s" % (
                str(r.get("generated_at", "")), str(r.get("market_name", "")),
                _txt[:500]))
        lines.append("")
        lines.append("【上一轮你已讲过的内容 · 本轮请接着讲、别冷重启】")
        lines.append("下面是你前 1~2 轮的口播原文（最后一段通常就是上一轮收尾留的钩子）。"
                     "本轮**开头应先接住上一轮的话头**（用一句话自然接续上面最后说的那点事，"
                     "再展开本轮），而不是从零重新自我介绍。"
                     "**不许逐字照搬上一轮的原句**，但**鼓励接着上一轮往前推、深化、补新视角**，"
                     "只要不把原句原样再念一遍即可；若市场这几分钟变化很小，"
                     "可明确点出「相比几分钟前…」的差异，而不是假装上一轮没发生过。")
        lines.append("\n".join(_blk))
    return "\n".join(lines)


# 人设 system 里写死了"A股"（"正在播报一场A股比赛"/"今晚的题目是A股盘面"）。
# 港股/美股场次直接拿去用，人设就会与市场错位 —— 与我 V1.8.2 修的"指数跨市场"是
# 同一类错误的 system 层版本。做法：**不改人设文件**（人设是资产、要保持稳定），
# 而是在注入时把市场名替换成目标市场。三人设措辞不同但都含"A股"，逐个替换。
_MKT_RE = re.compile(r"A股")


def persona_system(p, F):
    """人设 system + 目标市场名注入 + 本场状态注入。"""
    s = p.get("system") or ""
    mk = F.get("mk")
    name = F.get("mkt_name") or "全球"
    if mk is None:
        # 全球速览场：人设原文写死"A股"，要换成"全球市场"而不是某个单一市场
        s = _MKT_RE.sub("全球市场", s)
    elif mk != "cn" and "A股" in s:
        s = _MKT_RE.sub(name, s)
    # 状态行对**所有市场**都注入（含 A股 休市）—— 早先只在 `mk != "cn"` 分支里追加，
    # 结果 A股 休市日反而没有状态行，而那正是最需要说清"这是复盘不是直播"的场景。
    if mk is None:
        s += ("\n\n【本场状态】当前 A股/港股/美股**都没有在交易**。你是在做"
              "**全球市场资讯速览** —— 只解读全球涨跌与外媒快讯，"
              "不报任何单一市场的点位、不点评个股、不做涨跌预测、不预告下一个开盘。")
    elif mk == "cn":
        s += ("\n\n【本场状态】%s正在交易中，你是在做**实时播报**。" % name)
    elif F.get("mk_pre"):
        # ⚠️ V1.9.1：盘前是"即将开盘"，既不是实时播报、也不是复盘 —— 走独立分支。
        #   原来落进下面的 else（"不在交易时段，你在复盘"）会把盘前讲成事后点评，
        #   而且那句 system 里点名了 A股/港股/美股三市场，模型顺势就全念了一遍。
        s += ("\n\n【本场状态】现在是**%s盘前**（还没开盘，即将开始交易）。" % name)
        s += ("你只讲 %s 盘前在发生什么：盘前定价、%s 的个股与消息面、"
              "与 %s 相关的经济数据。" % (name, name, name))
        s += ("**一个字都不许提别的市场** —— 不许提A股、不许提港股、不许说它们休市或已收盘，"
              "也不要预告别的市场什么时候开。你就是 %s 盘前这一场的解说。" % name)
    elif F.get("is_open", True):
        s += "\n\n【本场状态】%s正在交易中，你是在做**实时播报**。" % name
    else:
        s += ("\n\n【本场状态】%s当前不在交易时段，你是在做**复盘/点评**，"
              "不许用「实时」「正在」「盘中」等把过去式说成进行式。" % name)
    # 日期也必须进 system：user prompt 里已写了「不许自己换算星期」，但实测
    # 散户人设仍说「上礼拜五」（9/30 实际是周三）—— 口语化人设更容易顺嘴带出
    # 相对日期，而 system 的约束力才压得住人设。system 与 user 双写才稳。
    if mk == "cn" and not F.get("cn_fresh"):
        snap_d = str(F.get("date") or "")
        try:
            _d = dt.date(int(snap_d[:4]), int(snap_d[4:6]), int(snap_d[6:8]))
            s += ("\n【数据日期】%d年%d月%d日（周%s）。要用日期就原样念这个，"
                  "**禁止自己换算星期或说成「上周X」**（实测会算错）。"
                  % (_d.year, _d.month, _d.day, "一二三四五六日"[_d.weekday()]))
        except Exception:                                            # noqa: BLE001
            pass
    return s


def _dedup_date(t, y, m, d):
    """压掉 LLM 顺嘴写出的叠字日期。

    口语化人设会把两种日期写法连着说，实测四种形态都出现过：
      「2026年9月30号2026年9月30日」「2026年9月30日9月30号」
      「9月30日9月30日」「9月30日9月30日9月30日」
    ⚠️ **不要用正则套娃**（实测连改三轮仍错）：形态 = 3 种写法 × 2 个顺序 × 可叠 N 次，
      正则写不全 —— 三连叠会产出「2026年2026年9月30日」这种**更糟**的结果。
    改用**扫 token 法**：正则只负责「找出全部日期 token」（这一步是可靠的），
    再按出现顺序保留第一个、逐个从原文切掉其余（两次之间的正文原样保留）。
    """
    # ⚠️ 年份必须整组可选：写成 `%d年?%d月%d` 时 "2026" 仍是必需的，
    # 匹配不到「9月30日」这种简写（同一个坑在 guard 处已踩过一次，这里用 (?:%d年)?）。
    tok = re.compile(r"(?:%d年)?%d月%d[日号]" % (y, m, d))
    hits = list(tok.finditer(t))
    if len(hits) < 2:
        return t
    out, last = [t[:hits[0].end()]], hits[0].end()
    for h in hits[1:]:
        out.append(t[last:h.start()])      # 两次之间的正文照留
        last = h.end()                     # 跳过这个重复 token
    out.append(t[last:])
    return "".join(out)


# V1.8.6：AI 引擎的 refs 抽取
# ================================================================
# 背景：refs（正文末尾的「名称+涨跌幅」标签）一直是 local 引擎的产物 ——
#   合成器自己知道每段引用了哪个标的，于是直接挂上。AI 引擎不经过那条路，
#   `_parse_llm` 出来的段 refs 恒为 []（实测），页面直接跳过 → 标签完全不出现。
#   师傅反馈：「提到指数或新闻资讯的，标的本来就在我们的后缀标签里有约定，没有出现」。
# 修法：**从 AI 已生成的正文里反向识别实体**，回查快照拿真实涨跌幅与链接。
#   为什么放后处理而不是让 LLM 自己填 refs：让 LLM 输出结构化 refs 会引入新的
#   编造面（它可能给不存在的标的编涨跌幅），而正文里的实体是它已经写出来的，
#   **回查校验更安全** —— 抽不到就不挂，绝不臆造。
IDX_NAME_SORTED = None      # 懒加载：按名称长度降序（长名优先，避免"恒生"抢在"恒生科技"前匹配）


def _idx_pattern():
    """构造指数识别正则：全称 + 口语别名，长名优先。"""
    global IDX_NAME_SORTED
    if IDX_NAME_SORTED is None:
        names = set(IDX_TICKER) | set(IDX_ALIAS)
        # 长的排前面：「恒生科技」要先于「恒生」、「英国富时100」要先于「英国富时」
        IDX_NAME_SORTED = sorted(names, key=len, reverse=True)
    return re.compile("|".join(re.escape(n) for n in IDX_NAME_SORTED))


# 资讯来源关键词 → 媒体名。AI 正文里会提「世行」「美联储」「OPEC」这类机构/说法，
# 属于资讯线索。但**必须能在快照的 news 里找到对应条目**才挂标签（防臆造）。
# ⚠️ 中文正文 vs 英文新闻标题 —— 必须有对照表，否则永远匹配不上（实测踩到）。
#   正文写「世行提醒人工智能资源集中」，快照标题是 "World Bank warns of AI
#   concentration risks" —— 一个中文字符都不同，`k in hay` 恒 False，标签挂不上。
#   规则：**键是中文口语（从正文抽），值是英文关键词列表（去标题里找）**。
#   同理「特朗普」→Trump、「美联储」→Fed、「欧佩克」→OPEC。
NEWS_HINT_MAP = {
    "世行": ["world bank"], "世界银行": ["world bank"],
    "美联储": ["fed", "federal reserve"], "美国联储": ["fed"],
    "特朗普": ["trump"], "白宫": ["trump", "white house"],
    "欧佩克": ["opec"], "opec": ["opec"], "石油输出国组织": ["opec"],
    "英伟达": ["nvidia"], "谷歌": ["google"], "苹果": ["apple"],
    "微软": ["microsoft"], "亚马逊": ["amazon"], "特斯拉": ["tesla"],
    "脸书": ["meta", "facebook"],
    "油价": ["oil", "crude"], "原油": ["oil", "crude"],
    "美债": ["treasury", "yield"], "收益率": ["yield", "treasury"],
    "通胀": ["inflation", "cpi"], "非农": ["jobs", "payroll", "employment"],
    "就业": ["jobs", "payroll", "employment"],
    "降息": ["rate cut", "fed cut", "lower rate"], "加息": ["rate hike", "raise rate"],
    "关税": ["tariff"], "制裁": ["sanction"],
    "俄乌": ["russia", "ukraine"], "俄罗斯": ["russia"], "乌克兰": ["ukraine"],
}
# ⚠️ **特异性纪律**：线索词越泛越容易误挂。「人工智能」「芯片」「俄」「乌」
#   在财经新闻里几乎篇篇出现（AI concentration / chip stocks / Russia 都常客），
#   挂了会指到不相干的条。已从线索表移除 —— 宁可少挂一个标签，
#   也不能让读者点开发现"这跟我看的那条没关系"。
# 组合线索（正则）：抽出的词去 NEWS_HINT_MAP 查英文关键词
NEWS_HINT_PAT = re.compile("|".join(sorted(map(re.escape, NEWS_HINT_MAP), key=len, reverse=True)))

# ⚠️ V1.9.32 C 方向：快讯主题词 → A股 行业板块 的轻量关联映射。
#   规则：**只匹配"今日真实出现"的板块名**（sec_top/sec_bottom），保证解说能引用真实涨幅；
#   若某主题板块今天不在榜上，宁可不挂（绝不编造板块）。关键词用英文（快讯多为外媒英文），
#   子串匹配板块中文名。特异性纪律同 NEWS_HINT_MAP：不用"人工智能/芯片"这类篇篇出现的泛词。
NEWS_SECTOR_LINKS = [
    ("半导体", ["nvidia", "semiconductor", "amd", "tsmc", "台积电", "nvda"]),
    ("石油", ["oil", "crude", "opec", "原油"]),
    ("油气", ["oil", "crude", "opec", "原油"]),
    ("黄金", ["gold", "黄金"]),
    ("贵金属", ["gold", "贵金属"]),
    ("汽车", ["tesla", "electric vehicle", " ev "]),
    ("新能源车", ["tesla", "electric vehicle"]),
    ("银行", ["fed ", "rate cut", "rate hike", "yield", "降息", "加息"]),
    ("白酒", ["liquor", "baijiu", "白酒"]),
    ("医药", ["fda", "pharma", "医药"]),
    ("房地产", ["real estate", "房产", "楼市", "地产"]),
    ("煤炭", ["coal", "煤炭"]),
    ("锂", ["lithium", "锂"]),
    ("消费电子", ["apple", "iphone", "消费电子"]),
    ("券商", ["降息", "加息", "rate"]),
]

# ⚠️ V1.9.36：上面那张表是「板块名 → 关键词」，只对得上**板块名本身出现在快讯里**的情况；
#   而中文快讯习惯用泛称（「传媒板块涨势扩大」「AI金属共振有色板块」），今日榜上却是
#   申万细分名（"视频媒体 / 文字媒体 / 影视院线 / 印制电路板"）→ 直接匹配必然落空。
#   故新增「行业簇」：快讯泛称/实体词 → 今日真实出现板块名需包含的片段。
#   纪律同前：**只与今日真实在榜的板块相连**（不在榜就不挂，绝不编造）；
#   泛词（"ai"/"人工智能"/"芯片"这类篇篇出现的）已剔除，只留实体词与行业专名；
#   每条快讯最多挂 2 个板块、整体最多 4 条关联，防"一挂一大片"的噪声。
NEWS_SECTOR_CLUSTERS = [
    (["传媒", "院线", "影视", "短剧", "media"],
     ["媒体", "影视", "院线", "广播", "出版", "动漫"]),
    (["有色", "小金属", "铜价", "铝价"],
     ["金属", "有色"]),
    (["半导体", "芯片", "nvidia", "英伟达", "amd", "台积电", "tsmc", "存储", "hbm"],
     ["半导体", "元件", "电路板", "电子化学", "集成电路", "光学光电子"]),
    (["openai", "算力", "数据中心", "服务器"],
     ["计算机设备", "软件", "通信设备", "it服务", "云服务"]),
    (["光伏", "硅料", "有机硅", "组件"],
     ["光伏", "硅"]),
    (["创新药", "fda", "pharma", "医药"],
     ["医药", "生物制品", "化学制药", "医疗器械", "中药", "医疗"]),
    (["tesla", "特斯拉", "新能源车", "整车", "锂电"],
     ["汽车整车", "汽车零部件", "乘用车", "商用车", "汽车服务", "整车"]),
    (["降息", "加息", "rate cut", "rate hike", "国债收益率"],
     ["银行", "证券", "保险", "多元金融"]),
    (["黄金", "gold"], ["黄金", "贵金属"]),
    (["楼市", "地产", "real estate", "房价"],
     ["房地产", "地产", "装修", "建材"]),
    (["风电", "海上风电"], ["风电"]),
]


def link_news_sectors(news_items, sectors):
    """轻量关键词关联：每条快讯 → 今日真实出现的关联板块（含方向 领涨/承压）。无匹配返回 []。"""
    present = []
    for _s in (sectors.get("top") or []):
        if _s.get("name"):
            present.append((_s["name"], _s.get("pct"), "领涨"))
    for _s in (sectors.get("bottom") or []):
        if _s.get("name"):
            present.append((_s["name"], _s.get("pct"), "承压"))
    if not present:
        return []
    out = []
    for _n in news_items:
        _hay = ("%s %s" % (_n.get("title") or "", _n.get("text") or "")).lower()
        _hit = []
        # ① 板块名**直接命中**（中文快讯常直接写出来，如「传媒板块涨势扩大」）
        for _pn, _pp, _pk in present:
            if _pn and _pn in _hay:
                _hit.append({"name": _pn, "pct": _pp, "kind": _pk})
        # ② 行业簇命中（泛称/实体词 → 今日真实在榜的细分板块）—— V1.9.36 新增
        for _kws, _frags in NEWS_SECTOR_CLUSTERS:
            if not any(_k.lower() in _hay for _k in _kws):
                continue
            for _pn, _pp, _pk in present:
                if any(_f in _pn for _f in _frags):
                    _hit.append({"name": _pn, "pct": _pp, "kind": _pk})
        # ③ 老表（板块名 → 英文专题词），保留兜底
        for _sub, _kws in NEWS_SECTOR_LINKS:
            if not any(_sub in p[0] for p in present):
                continue
            if any(_k in _hay for _k in _kws):
                for _pn, _pp, _pk in present:
                    if _sub in _pn:
                        _hit.append({"name": _pn, "pct": _pp, "kind": _pk})
                        break
        # 去重 + 每条快讯最多 2 个板块（防"一挂一大片"）
        _uniq, _names = [], set()
        for _h in _hit:
            if _h["name"] in _names:
                continue
            _names.add(_h["name"])
            _uniq.append(_h)
        if _uniq:
            _snip = (_n.get("text") or _n.get("title") or "")[:40]
            out.append({"snippet": _snip, "sectors": _uniq[:2]})
    # V1.9.36：同一板块最多出现在一条关联里（否则两条 OpenAI 快讯把名额占满，
    # 会把"黄金/传媒"这类真正接得上主线的关联挤出去）；整体最多 4 条。
    _seen_sec, _kept = set(), []
    for _item in out:
        _secs = [s for s in _item["sectors"] if s["name"] not in _seen_sec]
        if not _secs:
            continue
        for s in _secs:
            _seen_sec.add(s["name"])
        _kept.append({"snippet": _item["snippet"], "sectors": _secs})
        if len(_kept) >= 4:
            break
    return _kept



# ⚠️ V1.9.36：正文 ↔ 快讯 的"词重合"辅助（用于给段落挂资讯标签）。
#   起因：旧线索表（NEWS_HINT_MAP）只收**中文名**，而模型经常直接用英文原名
#   （Apple / OpenAI / iPhone）→ 表里查不到 → 明明引用了快讯却挂不上标签
#   （实测 V1.9.36 首轮：正文写了 Apple 砍 iPhone 订单、OpenAI 合作，资讯段仍为 0）。
#   这里改用**正文与快讯原文的直接重合**：英文词（≥3 字符，权重 2）+ 中文二字组（权重 1），
#   阈值保守（≥2，即 1 个英文实体词或 2 个中文二字组）—— 宁可少挂一个标签，也不误挂。
_WORDS_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-\.]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = {"the", "and", "for", "with", "that", "this", "from", "has", "have",
                "are", "was", "will", "its", "not", "you", "our", "than", "now", "out",
                "今天", "我们", "可能", "这个", "那个", "已经", "还是", "就是", "可以"}


def _overlap_tokens(s):
    """正文/快讯 → 可比较的 token 集合（英文词 + 中文二字组）。"""
    s = (s or "").lower()
    toks = set(w for w in _WORDS_RE.findall(s) if w not in _STOP_TOKENS)
    for run in _CJK_RUN_RE.findall(s):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks


def _match_news(text, news):
    """正文里提到的资讯线索 → 匹配快照 news 里最相关的一条（按关键词重合度）。"""
    if not news:
        return None
    # 媒体名也作为线索（正文若写「CNBC 报道…」也能匹配上）
    kw = NEWS_HINT_PAT.findall(text or "")
    # 中文线索 → 英文关键词集合（走对照表，见 NEWS_HINT_MAP 注释）
    en = set()
    for k in kw:
        for e in NEWS_HINT_MAP.get(k.lower(), [k.lower()]):
            en.add(e)
    best, best_score = None, 0
    if en:
        for idx, n in enumerate(news):
            hay = ("%s %s" % (n.get("title") or "", n.get("text") or "")).lower()
            score = 0
            for e in en:
                if e in hay:
                    score += 1
            # 同分时取列表里靠前那条（news 越靠前越新）。⚠️ 用 `>` 不用 `>=`，
            # 否则**最后**一条同分的会覆盖掉更靠前的那条 —— 实测「特朗普帮俄罗斯」
            # 与另一条含 Trump 的新闻同分，挂了不相干的 MAGA 那条。
            if score > best_score:
                best, best_score = n, score
    # 线索表命中（≥1 词）就用它 —— 那张表是人工筛过的，优先信它
    if best_score >= 1:
        return best
    # V1.9.36 路径②：正文 ↔ 快讯原文的词重合（老路径无命中时才用，阈值保守）
    tt = _overlap_tokens(text)
    if not tt:
        return None
    b2, s2 = None, 0
    for n in news:
        nt = _overlap_tokens("%s %s" % (n.get("title") or "", n.get("text") or ""))
        if not nt:
            continue
        inter = tt & nt
        if not inter:
            continue
        _en = len([x for x in inter if x.isascii()])
        _zh = len([x for x in inter if not x.isascii()])
        _sc = _en * 2 + _zh
        if _sc > s2:
            b2, s2 = n, _sc
    return b2 if s2 >= 2 else None


def attach_refs(segs, F, cap=4):
    """给 AI 生成的段落回填 refs（指数 + 资讯）。

    ⚠️ 三条纪律：
     1. **只挂快照里真实存在的**。指数涨跌幅一律回查 markets/global_ticker，不信正文里的数字。
     2. **资讯必须能在 news 里匹配上**（关键词重合 ≥1），否则不挂 —— 防"听起来像"的臆造。
     3. **抽不到就不挂**，绝不用占位符充数。标签空着比挂错强。
    """
    mk = F.get("mk")
    # 指数池：目标市场 + 全球走马灯（港美股场次池里已按 prompt 过滤掉 A 股）
    pool = {}
    for x in ((F.get("markets") or {}).get(mk) or []) if mk else []:
        if isinstance(x, dict) and x.get("name"):
            pool[x["name"]] = x
    for x in (F.get("index") or {}).values():
        if isinstance(x, dict) and x.get("name"):
            pool.setdefault(x["name"], x)
    for x in (F.get("global") or []):
        if isinstance(x, dict) and x.get("name"):
            pool.setdefault(x["name"], x)
    if F.get("parallel_hk"):
        # AH 并行：把港股指数也并入指数池，正文提到恒指/恒科才能挂指数标签
        for x in ((F.get("markets") or {}).get("hk") or []):
            if isinstance(x, dict) and x.get("name"):
                pool.setdefault(x["name"], x)
    if not pool:
        return segs
    pat = _idx_pattern()
    # ⚠️ V1.9.6 港股个股：只挂**本轮快照里真有行情**的（hk_stocks），且带别名匹配
    #   （正文写「腾讯」→ 命中快照里的「腾讯控股」）。名单外的公司一律不挂（防编造）。
    hk_pool = {}
    for x in (F.get("hk_stocks") or []):
        if isinstance(x, dict) and x.get("name"):
            hk_pool[x["name"]] = x
    hk_pat = None
    if hk_pool:
        _al = set()
        for nm in hk_pool:
            _al.add(nm)
            _al.update(HK_STOCK_ALIAS.get(nm) or [])
        hk_pat = re.compile("|".join(re.escape(a) for a in sorted(_al, key=len, reverse=True)))
    # ⚠️ V1.9.12 A股个股：与港股同理 —— 只挂本轮快照里**真有行情**的（涨幅/跌幅/成交额榜），
    #   名单外一律不挂（防编造）。A股名直接用全称匹配（模型照 prompt 榜单全称写，不写简称）。
    #   仅 A股场次（mk=="cn"）启用，避免港/美股场次误挂 A股个股。
    cn_pool = {}
    if mk == "cn":
        for r in ((F.get("up") or []) + (F.get("down") or []) + (F.get("active") or [])):
            if isinstance(r, dict) and r.get("name") and r.get("code"):
                cn_pool[str(r["name"])] = r
        # 涨停/跌停池（连板梯队）也纳入：模型常直接点连板股名（「紫竹高科3连板」），
        # 同为个股、同样该给「名字+涨跌幅」标签。pct 用池里的涨停/跌停幅度，可核对。
        for r in ((F.get("zt_pool") or []) + (F.get("dt_pool") or [])):
            if isinstance(r, dict) and r.get("name") and r.get("code"):
                cn_pool.setdefault(str(r["name"]), r)
    cn_pat = None
    if cn_pool:
        cn_pat = re.compile("|".join(re.escape(nm) for nm in sorted(cn_pool, key=len, reverse=True)))
    # ⚠️ V1.9.13 板块标签：A股领涨/领跌板块（名字+涨幅）挂标签。prompt 已给板块数据，
    #   模型会点名；正文提板块即挂，与个股同源。无 ticker（东财 gn_xxx 非行情代码），只给名称+涨幅。
    sec_pool = {}
    if mk == "cn":
        for r in ((F.get("sec_top") or []) + (F.get("sec_bottom") or [])):
            if isinstance(r, dict) and r.get("name"):
                sec_pool[str(r["name"])] = r
    sec_pat = None
    if sec_pool:
        sec_pat = re.compile("|".join(re.escape(nm) for nm in sorted(sec_pool, key=len, reverse=True)))
    n_idx = n_news = n_hk = n_cn = n_sec = 0
    for seg in segs:
        t = seg.get("text") or ""
        refs, seen = [], set()
        for m in pat.finditer(t):
            raw = m.group(0)
            std = IDX_ALIAS.get(raw, raw)
            if std in seen:
                continue
            q = pool.get(std)
            if not q or not IDX_TICKER.get(std):
                continue          # 快照里没有 / 无公开 ticker → 不挂
            seen.add(std)
            refs.append(_ref_index(std, q.get("pct"), q.get("price")))
            if len(refs) >= cap:
                break
        if refs:
            n_idx += 1
        # 港股个股：正文提到就挂一个可核对标签（涨幅/价格一律回查快照，不信正文）
        if hk_pat:
            _added_hk = False
            for m in hk_pat.finditer(t):
                raw = m.group(0)
                std = raw
                for nm, al in HK_STOCK_ALIAS.items():
                    if raw in al:
                        std = nm
                        break
                if std in seen:
                    continue
                q = hk_pool.get(std)
                if not q:
                    continue
                seen.add(std)
                refs.append(_ref_hk_stock(std, q.get("code"), q.get("pct"), q.get("price")))
                _added_hk = True
                if len(refs) >= cap:
                    break
            if _added_hk:
                n_hk += 1
        # A股个股：正文提到就挂一个可核对标签（涨跌幅/价格一律回查快照，不信正文）
        if cn_pat:
            _added_cn = False
            for m in cn_pat.finditer(t):
                raw = m.group(0)
                if raw in seen:
                    continue
                q = cn_pool.get(raw)
                if not q:
                    continue
                seen.add(raw)
                refs.append(_ref_stock(q))
                _added_cn = True
                if len(refs) >= cap:
                    break
            if _added_cn:
                n_cn += 1
        # ⚠️ V1.9.13 板块：正文提到领涨/领跌板块名即挂标签（仅 A股场次）
        if sec_pat:
            _added_sec = False
            for m in sec_pat.finditer(t):
                raw = m.group(0)
                if raw in seen:
                    continue
                q = sec_pool.get(raw)
                if not q:
                    continue
                seen.add(raw)
                refs.append(_ref_sector(raw, q.get("pct")))
                _added_sec = True
                if len(refs) >= cap:
                    break
            if _added_sec:
                n_sec += 1
        # 资讯：整段里找线索，匹配到就在末尾加一个可点击的来源标签
        n = _match_news(t, F.get("news"))
        if n and n.get("link"):
            refs.append({"code": "", "name": n.get("src") or "外媒快讯",
                         "pct": None, "link": n.get("link"),
                         "title": (n.get("title") or n.get("text") or "")[:80],
                         "kind": "news"})
            n_news += 1
        seg["refs"] = [r for r in refs if r]
    if n_idx or n_news or n_hk or n_cn or n_sec:
        log("[refs] 回填标签：指数段 %d、港股个股段 %d、A股个股段 %d、板块段 %d、资讯段 %d" % (n_idx, n_hk, n_cn, n_sec, n_news))
    return segs


def _looks_truncated(txt):
    """判断 LLM 返回是否被 max_tokens 截断（末段断在半句）。

    实测踩到（V1.8.3）：max_tokens=1600 时，talk 人设只出 3 段就被切断，
    末段「我账户比科创50还惨，人家才跌两个多点，我跌」—— 句子没写完就停了。
    段数少 + 末段无句末标点 = 截断。**必须检测并重试**，否则页面上半句话很难看。
    """
    t = (txt or "").rstrip()
    if not t:
        return True
    return t[-1] not in "。！？!?…\"'』」）)】"


def llm_segments(F, p, a, max_tries=3):
    """调 LLM 并解析。带重试：Agnes 免费网关首调偶发「HTTP 200 + 空内容」软失败、
    或 429 限流，必须退避重试，否则首个人设会整段丢失。

    ⚠️ 连续失败返回 []（**不退回本地合成**）—— 师傅要求「不要任何合成的内容」。
       空段时页面显示为无解说，胜过拿模板假装是 AI 产物。
    ⚠️ 截断（末段断在半句）也走重试，并把 max_tokens 逐步放大到 2 倍。"""
    import urllib.request
    import ssl
    url = a.base.rstrip("/") + "/chat/completions"
    # V1.9.18：兼容出口代理偶发 TLS 提前关闭（SSL UNEXPECTED_EOF_WHILE_READING）
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.options |= 0x100  # OP_IGNORE_UNEXPECTED_EOF
    payload = {
        # max_tokens 直给 2600：10 段 × 90 字中文 ≈ 1400 token 足够，但口语化人设
        # （尤其 talk「必须有包袱、比喻要具体」）容易写得偏长，1600 实测被截断
        # （talk 只出 3 段、末句断在「我跌」）。宁多给也别截断 —— 截断检测+重试
        # 会白烧一次额度（10 RPM 很小气）。
        "model": a.model, "temperature": 0.9, "max_tokens": 2600,
        "messages": [
            {"role": "system", "content": persona_system(p, F)},
            {"role": "user", "content": build_prompt(F, p)},
        ],
    }
    last_err = None
    # 截断重试时逐步放大 token：×1.0 → ×1.6 → ×2.5（封顶 5200）。
    # ⚠️ 基准必须与 payload 初始值一致 —— 早先这里硬编码 1600 而 payload 是 2600，
    #   结果"放大"第一次反而把 token 降回 1600（实测 retail 连试两次仍截断）。
    base_tokens = int(payload.get("max_tokens") or 2600)
    for attempt in range(1, max_tries + 1):
        try:
            payload["max_tokens"] = min(5200, int(base_tokens * (1.0, 1.6, 2.0)[attempt - 1]))
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + a.key})
            with urllib.request.urlopen(req, timeout=90, context=_ssl_ctx) as r:
                d = json.loads(r.read().decode())
            txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not txt or not txt.strip():
                raise RuntimeError("空响应（疑似网关冷启动/限流软失败）")
            # 截断检测：末段断在半句就重试（放大 token），宁可慢也不能上半句话上页
            if _looks_truncated(txt) and attempt < max_tries:
                raise RuntimeError("输出疑似被 max_tokens=%d 截断（末段未完句）"
                                   % payload["max_tokens"])
            if _looks_truncated(txt):
                log("[warn] %s 末段仍不完整，按现有内容采用（%d 段）"
                    % (p.get("name", "?"), len(_parse_llm(txt))))
            segs = _parse_llm(txt)
            # ⚠️ V1.8.7 新增：**段数也是个质量指标**。同一 prompt 在 temp=0.9 下实测出过
            #   5 段（要求 6~10 段）—— 内容没错但场次太薄，页面上一屏就播完了。
            #   段数不足就重试一次（截断重试已把 token 放大，这条只管"写得少"）。
            if len(segs) < 6 and attempt < max_tries:
                raise RuntimeError("只出 %d 段（要求 6~10 段）" % len(segs))
            return segs
        except urllib.error.HTTPError as e:
            last_err = e
            backoff = 8 if e.code == 429 else 3
            log("[retry %d/%d] HTTP %s：%s，%.0fs 后重试" % (attempt, max_tries, e.code, str(e)[:80], backoff))
            if attempt < max_tries:
                time.sleep(backoff)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log("[retry %d/%d] %s：%s，3s 后重试" % (attempt, max_tries, type(e).__name__, str(e)[:80]))
            if attempt < max_tries:
                time.sleep(3)
    log("[x] %s LLM 连续 %d 次失败：%s" % (p.get("name", "?"), max_tries, str(last_err)[:100]))
    return []


def _fix_market_pct(segs, F):
    """校验正文里的「行情百分比」是否与快照一致，不一致就地纠正（V1.9.0 新增）。

    ⚠️ 为什么 prompt 禁令不够（实测抓到的真实案例）：
      prompt 里明明白白写着「恒生指数 +1.00%、恒生科技 +0.94%」，模型输出却是
      「恒生科技也涨了不到一个点，1.94%」——**0.94 被转写成 1.94**。
      这不是"凭空编造一个新数字"（那类 prompt 禁令能拦），而是**转写变形**：
      数字看起来合理、禁令里也没写"不许算术错误"，模型自己滑过去了。
      → 凡是"确定性数字"（纪律 57），就不能只靠 prompt 声明，必须代码兜一遍。

    修法（保守，宁可漏改不可错改）：
      1. 只认行情源给出的百分比集合（走马灯 + 各市场指数 + 异动榜）；
      2. **必须同段内出现指数名**才认这是"行情百分比"——否则快讯里公司自己的
         数字（如 NVIDIA 净利率 55.6%）会被误当成行情而遭改（那才是真事故）；
      3. 文本里对不上任何真值的百分比**直接删掉该数字**（不做"猜哪个才对"），
         宁可留「涨了不少」也不留错数字。
    """
    # 收集所有行情真值
    truth = {}

    def _reg(name, pct):
        if pct is None:
            return
        try:
            v = abs(round(float(pct), 2))
        except (TypeError, ValueError):
            return
        if v<= 60:                       # 合理涨跌幅上限，超过多半不是行情
            truth.setdefault(v, name)
    for g in (F.get("global") or []):
        if isinstance(g, dict):
            _reg(g.get("name"), g.get("pct"))
    for k in (F.get("market_keys") or []):
        for r in ((F.get("markets") or {}).get(k) or []):
            if isinstance(r, dict):
                _reg(r.get("name"), r.get("pct"))
    # ⚠️ V1.9.6 港股个股也进真值池 —— 否则 AI 报「腾讯涨 3.21%」这种**转写变形**
    #   （真值 2.31%）抓不到：个股涨幅和指数涨幅一样是"确定性数字"，同样要代码兜底。
    for r in (F.get("hk_stocks") or []):
        if isinstance(r, dict):
            _reg(r.get("name"), r.get("pct"))
    # ⚠️ V1.9.12 A股个股同样进真值池 —— 个股涨幅和指数一样是"确定性数字"，
    #   模型报「世名科技涨 20.00%」若被转写变形（如 2.00%）要能被代码抓到。
    for _grp in ("up", "down", "active", "zt_pool", "dt_pool"):
        for r in (F.get(_grp) or []):
            if isinstance(r, dict):
                _reg(r.get("name"), r.get("pct"))
    # ⚠️⚠️ V1.9.33 修 bug（师傅实测：「创业板指-，科创50-，印制电路板-、半导体材料-」数字整批丢失）：
    #   ① **漏注册 A股指数**：上面 market_keys 注册的是 markets.cn（实时接口口径），但 prompt 给
    #      LLM 的 A股指数来自 F["index"]（东财口径）——两套口径数值不同（实测创业板指 index=-1.42
    #      vs markets.cn=-1.58）。于是模型**照着 prompt 写出的正确数字**被判"不在真值里"→ 删除。
    #   ② **漏注册板块**：sec_top/sec_bottom 的 pct 从未进池 → 讲板块涨幅时同样被删。
    #   港美股正常正是因为 markets.hk/us 早就注册了（1.40 命中）。补这两处即根治。
    for _iv in (F.get("index") or {}).values():
        if isinstance(_iv, dict):
            _reg(_iv.get("name"), _iv.get("pct"))
    for _sr in ((F.get("sec_top") or []) + (F.get("sec_bottom") or [])):
        if isinstance(_sr, dict):
            _reg(_sr.get("name"), _sr.get("pct"))
    # ⚠️ V1.9.33 续（A/B 方向的连带缺口）：V1.9.31 起解说要讲【日内走势形态】，而「日内振幅 /
    #   前后段斜率」是 prompt 直接给出的确定性数字。若不注册，模型**正确引用**「日内振幅 1.35%」
    #   也会被当转写变形删掉（实测本轮 1.35% 即被删）。美股广度里以 % 表达的两个字段同理。
    for _is in (F.get("intraday_summary") or {}).values():
        if isinstance(_is, dict):
            _reg("日内振幅", _is.get("amp_pct"))
            _reg("前后段斜率", _is.get("slope_pct"))
    _ubt = F.get("us_breadth") or {}
    if isinstance(_ubt, dict):
        _reg("站上200日均线占比", _ubt.get("above_200ma_pct"))
        _reg("上涨占比", _ubt.get("adv_pct"))
    if not truth:
        return segs
    # 指数名关键词 → 用于判断段内是否有"锚点"
    keys = sorted(truth.values(), key=len, reverse=True)
    alias = {"恒生科技": ["恒生科技", "恒生科技指数"], "恒生指数": ["恒生指数", "恒指"],
             "日经225": ["日经"], "英国富时100": ["富时"], "巴西Bovespa": ["巴西", "Bovespa"],
             "上证指数": ["上证"], "深证成指": ["深证"], "创业板指": ["创业板"],
             "纳斯达克": ["纳指", "纳斯达克"], "道琼斯": ["道指", "道琼斯"],
             "标普500": ["标普"], "恒生国企": ["国企"]}
    # ⚠️ 个股锚点用口语别名（正文写「腾讯」就算提到这只票）。
    #   不能只用全名 —— 否则「腾讯涨 3.21%」段内找不到「腾讯控股」这个锚点，
    #   整段被当成"公司自己的财务数字"跳过校验，变形照样放过去。
    for _nm, _al in HK_STOCK_ALIAS.items():
        if _nm in truth.values():
            alias.setdefault(_nm, _al)
    # ⚠️ V1.9.33：正则必须**把正负号一起吃掉**。原先是 `(\d+(?:\.\d+)?)\s*%`，
    #   删除「1.83%」时负号留在原位 → 正文出现裸「-」（「创业板指-，科创50-」的真凶之一）。
    pct_re = re.compile(r"([+-]?\s*\d+(?:\.\d+)?)\s*%")
    for s in segs or []:
        txt = s.get("text") or ""
        if not pct_re.search(txt):
            continue
        # 段内是否有指数锚点（无锚点 = 大概率是公司/快讯数字，不动）
        has_anchor = False
        for name in keys:
            for a2 in alias.get(name, [name]):
                if a2 in txt:
                    has_anchor = True
                    break
            if has_anchor:
                break
        if not has_anchor:
            continue

        def _sub(m):
            # V1.9.33：group(1) 现在可能形如 "-1.83" / "+ 1.83"，空格要先去掉才 float 得动。
            raw = m.group(1).replace(" ", "")
            try:
                v = abs(round(float(raw), 2))
            except ValueError:
                return m.group(0)
            if v in truth:
                return m.group(0)                # 对得上，保留原文
            log("[fix] 行情百分比 %s%% 不在快照真值里 → 删除（可能转写变形）" % raw)
            return "\x00"                # 先占位，最后统一清标点（避免留下「，。」）
        s["text"] = pct_re.sub(_sub, txt)
        if "\x00" in s["text"]:
            # 删掉数字后收尾清理：占位符连着的顿号/逗号/多余空白一并收拾
            t2 = s["text"].replace("\x00", "")
            # ⚠️ V1.9.33 兜底：数字被删后若仍留下裸「-」「+」（「创业板指-，科创50-」），
            #   在这里清掉。函数与 build_live 渲染层共用（单一实现，见 _strip_dangling_sign）。
            t2 = _strip_dangling_sign(t2)
            t2 = re.sub(r"[，,、；;]{2,}", "", t2)
            t2 = re.sub(r"[（(]\s*[，,、；;]?\s*[）)]", "", t2)
            t2 = re.sub(r"\s+", " ", t2).strip()
            t2 = t2.replace("，、", "，").replace("、，", "，")
            t2 = t2.replace("，。", "。").replace("；。", "。")
            t2 = re.sub(r"，\s*。", "。", t2)
            s["text"] = t2
    return segs


# ⚠️ V1.9.11 中文数字 → 阿拉伯数字 兜底（**确定性代码层**，不依赖模型服从）。
#   根因：prompt 里「把数字念出来 / 翻译数字」被模型理解成"用中文读数"，
#   实测反复输出「两万四千一百三十一块四二」「百分之一点六七」「两个点」，
#   且**绕过** _fix_market_pct 的 `\d+%` 校验（中文读法匹配不到 → 一路放行）。
#   模型先验太强（V1.9.10 加铁律 6 仍无效），故在这里做确定性转换：
#   把中文读数还原成阿拉伯数字，既好读、又让下游百分比校验真正生效。
#   ⚠️ 只处理 4 类明确形态（点 / 块 / 百分之 / 个点 / 个半），
#   **不做 blanket 转所有中文整数**（否则「一个」「两次」会被误改），控制爆炸半径。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_CN_INT_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS)   # 零一二两三四五六七八九十百千万亿


def _cn_to_int(s):
    """中文整型（位值制）解析。含非数字字符返回 None。"""
    if not s or any(c not in _CN_DIGITS and c not in _CN_UNITS for c in s):
        return None
    r, tmp = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            tmp = _CN_DIGITS[ch]
        else:
            unit = _CN_UNITS[ch]
            if unit >= 10000:                 # 万 / 亿：进位
                r = (r + tmp) * unit
                tmp = 0
            else:                              # 十 / 百 / 千（单独出现当 1×unit）
                if tmp == 0:
                    tmp = 1
                r += tmp * unit
                tmp = 0
    return r + tmp


def _cn_or_int(s):
    """整数解析：纯 ASCII 直接 int，否则走中文位值制；无法解析返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return _cn_to_int(s)


def _cn_decimal_digits(s):
    """小数点后逐位读数（四二 → 42，二零 → 20，零5 → 05，允许中英文混排）。"""
    out = []
    for ch in s:
        if ch in _CN_DIGITS:
            out.append(str(_CN_DIGITS[ch]))
        elif ch.isdigit():
            out.append(ch)
        else:
            return None
    return "".join(out) if out else None


# ⚠️⚠️ V1.9.33：清理「悬空的正负号」—— 行情数字被删后残留在原位的 +/−。
#   实例（师傅 2026-10-09 截图）：「创业板指-，科创50-」「印制电路板-、半导体材料-」，
#   以及「有机硅+、蓄电池及其他电池+」。这类残缺既会上页，也会被 narrate 回灌 prompt
#   当「上一轮口播」（load_history → prev_rounds），所以 **narrate（生成后）与
#   build_live（渲染前）都要过一遍**，否则历史污染会反复回灌。
#   只清「汉字/数字/百分号/右括号 之后、紧跟标点或句尾」的悬空符号：
#   正常写法「-1.83%」「涨了+3%」的符号后面紧挨数字，不被匹配。
_DANGLING_SIGN_RE = re.compile(
    r"(?<=[\u4e00-\u9fa5A-Za-z0-9%）)])\s*[-+]\s*(?=[，,、；;。！？!?]|$)")


def _strip_dangling_sign(txt):
    """去掉悬空的 +/−（幂等；narrate 与 build_live 共用）。"""
    if not txt:
        return txt
    return _DANGLING_SIGN_RE.sub("", txt)


def _cn_to_arabic_in_text(txt):
    if not txt:
        return txt

    def _re_block(m):
        g1 = _cn_or_int(m.group(1))
        g2 = _cn_decimal_digits(m.group(2))
        if g1 is None or g2 is None:
            return m.group(0)
        return "%d.%s" % (g1, g2)

    def _re_pct(m):
        g = m.group(1)
        if "点" in g:                                   # 百分之一点六七
            a, b = g.split("点", 1)
            ia = _cn_or_int(a) if a else 0
            db = _cn_decimal_digits(b)
            if db is None:
                return m.group(0)
            return "%d.%s%%" % (ia, db)
        v = _cn_or_int(g)
        if v is None:
            return m.group(0)
        return "%d%%" % v

    def _re_half_point(m):
        g1 = _cn_or_int(m.group(1))
        return "%d.5个点" % g1 if g1 is not None else m.group(0)

    def _re_point_unit(m):
        g1 = _cn_or_int(m.group(1))
        return "%d个点" % g1 if g1 is not None else m.group(0)

    def _re_half(m):
        g1 = _cn_or_int(m.group(1))
        return "%d.5" % g1 if g1 is not None else m.group(0)

    # 顺序：百分之（含点小数）→ 块小数 → 点小数 → 个半点 → 个点 → 个半
    # 块/点 的整数部分允许 ASCII（兼容「79块零5」这类中英混排读数）；
    # 个半点/个点/个半 的整数部分**只允许中文**（避免误吞「2.5个点」里的 2.5）。
    txt = re.sub(r"百分之([\d零一二两三四五六七八九十百千万亿点]+)", _re_pct, txt)
    txt = re.sub(r"([\d%s]+)块([\d零一二两三四五六七八九]+)" % _CN_INT_CHARS, _re_block, txt)
    txt = re.sub(r"([\d%s]+)点([\d零一二两三四五六七八九]+)" % _CN_INT_CHARS, _re_block, txt)
    txt = re.sub(r"([零一二两三四五六七八九]+)个半点", _re_half_point, txt)
    txt = re.sub(r"([零一二两三四五六七八九]+)个点", _re_point_unit, txt)
    txt = re.sub(r"([零一二两三四五六七八九]+)个半", _re_half, txt)
    return txt


def cn_num_to_arabic(segs):
    """把每段正文里的中文读数转回阿拉伯数字（确定性兜底，幂等）。"""
    for s in segs or []:
        if isinstance(s, dict) and "text" in s:
            s["text"] = _cn_to_arabic_in_text(s.get("text") or "")
    return segs


def _fix_metric_name(segs):
    """校验财务指标口径，防止"数字对、指标名错"（V1.9.1 新增）。

    ⚠️ 实测案例：eulerpool 原文 `Nvidia's **55.6% net margin** and $96.7B free cash flow`
      被解说成「英伟达**毛利**率55.6%」—— 数字分毫不差，**指标名却换了**。
      这比数字错更隐蔽（对账查不出来），且**事实相反**：毛利率远高于净利率，
      说成毛利率等于把英伟达的成本结构讲宽松了。根因是中文里"margin"只能译
      "利润率"，模型在"净/毛"之间随手挑了一个。

    修法：把源文本里出现过的中英口径对照写进 prompt；**并且**在输出后做代码兜底 ——
    源里出现 net margin 时若正文写成"毛利率"，直接替换回正确口径。
    （不做泛化猜测，只认这一组高频混淆项。）
    """
    # 源 → 正确中文口径（按 F 里实际给出的快讯文本判定）
    _src = " ".join((n.get("text") or "") + " " + (n.get("title") or "")
                    for n in (F_NEWS_CACHE or []))
    pairs = [
        ("net margin", "净利率", "毛利率"),
        ("gross margin", "毛利率", "净利率"),
        ("operating margin", "营业利润率", "净利率"),
        ("profit margin", "利润率", "毛利率"),
    ]
    for en, right, wrong in pairs:
        if en not in _src.lower():
            continue
        for s in segs or []:
            txt = s.get("text") or ""
            if wrong in txt and right not in txt:
                # 该段同时出现了这个百分比语境 → 替换口径词
                s["text"] = txt.replace(wrong, right)
                log("[fix] 财务指标口径纠正：%s → %s" % (wrong, right))
                txt = s["text"]
    return segs


def _fix_stale_quote(segs, F):
    """盘前场次：删掉"现在/今天在涨在跌"的说法（V1.9.2 新增）。

    ⚠️ 实测 bug：盘前拿到的是**上一交易日收盘价**（道琼斯报价距今 14 小时），
      prompt 即使标了时点，模型仍顺嘴说「道琼斯**现在** 51267.90 点，**涨了** 0.18%」
      —— 师傅批「还没开盘，这个涨幅哪条资讯来的，乱说了啊」。

    ⚠️⚠️ 第一版写错了两次，都实测才发现（教训见下）：
      ① 窗口太窄：`(现在|今天)[^，。]{0,10}?(涨|跌)` —— 「现在 51267.90 点，涨了」
         中间隔了 11 字以上 → **师傅的原句没被改到**。
      ② 替换产生病句：把「今天涨了 1.05%」换成「上一交易日收在 1.05%」→
         **"收在了1.05%"** —— 涨跌幅是相对昨收的量，不能说"收在"。
      正确做法：**只删、不改写**。涨跌幅在盘前本来就该整段消失（prompt 层已不给），
      这里兜底把"（现在|目前|今天|今早|盘前）+ 涨跌"的**整个短语删掉**，
      剩下的句子读起来仍通顺——实测删掉后是「道琼斯51267.90点。说实话…」。
      **规律：兜底改写比原句更危险时，宁可删。**
    """
    if not F.get("mk_pre"):
        return segs
    # 禁用组合：**盘前语境的时间词** + **涨跌说法**。两者不必相邻 —— 实测
    # 「道琼斯**现在** 51267.90 点，**涨了** 0.18%」中间隔了 11 字，
    # 用正则邻接匹配必然漏。所以改成**分句判定**：一句里同时出现
    #「时间词」与「涨跌动词」→ 该句的涨跌部分整体不可信，**整句丢**。
    # （盘前一句里本就不该有"今早涨了X%"这种内容，丢整句损失最小。）
    TIME_WORD = ("现在", "目前", "今天", "今早", "盘前", "开盘", "盘中", "到目前为止")
    UP_DOWN = ("涨", "跌", "走高", "走低", "拉升", "跳水", "飘红", "飘绿", "上行", "下行")
    out = []
    for s in segs or []:
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        # 按中文句读切分，逐句判定
        parts = re.split(r"(?<=[。！？；])", txt)
        keep = []
        for p in parts:
            if not p.strip():
                continue
            has_t = any(w in p for w in TIME_WORD)
            has_d = any(w in p for w in UP_DOWN)
            if has_t and has_d:
                # 该句把"陈旧收盘"讲成了当下涨跌 → 丢掉整句
                log("[fix] 盘前「现在/今天…涨跌」整句丢弃：%s" % p.strip()[:44])
                continue
            # 句子里带百分比且带时间词（即使没涨跌动词）也危险，如「现在是+0.18%」
            if has_t and re.search(r"\d+\s*%", p):
                log("[fix] 盘前句含时间词+百分比 → 丢弃：%s" % p.strip()[:44])
                continue
            keep.append(p)
        new = "".join(keep).strip()
        if not new:
            log("[fix] 该段剔除涨跌句后为空 → 整段丢弃")
            continue
        s["text"] = new
        out.append(s)
    return out


def _strip_junk_segs(segs):
    """剔除垃圾段：markdown 残留 / 纯免责元叙述 / 预测句式（V1.9.1 新增）。

    ⚠️ 触发场景（实测）：**盘前场次数据少**（只有 3 个指数 + 几条快讯，且按纪律
      禁掉了外围与别家），模型"没料可讲"就转向两个坏习惯：
        ① **预测**：「今天要是真开了，科技股那边估计还得热闹」—— 铁律 3 已禁，但它
           仍会出现（而且往往紧跟一句自我免责声明，典型输出见第 9/10 段）。
        ② **元叙述免责段**：「（这句不负责任，别较真）*」—— 独立成段、还带 markdown
           星号，等于直播里放了个免责声明。
      两者都会**同段或邻段互相掩护**（预测 + 免责连着来），只禁文字容易漏，
      故这里**成段处理**：命中即整段丢弃。

    判定要看**实质内容**：删掉免责/评论性括号后，如果剩下的可讲内容不足成段
    （少于 8 个字），说明整段本来就是免责话术 → 整段删。
    """
    PRED = ("要是真开", "估计还", "我觉得会", "预计会", "我猜", "八成", "大概会",
           "明天会", "后天会", "下周会", "接下来会")
    META = ("别较真", "不负责任", "这句不算", "我不懂", "我不确定", "我记不清",
            "仅供参考", "不构成", "见谅", "别怪", "我不是", "原谅")
    keep = []
    for s in segs or []:
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        #① 剥掉 markdown 标记与所有括号注释，再看还剩什么
        core = re.sub(r"\*+", "", txt)
        core = re.sub(r"[（(][^）)]*[）)]", "", core).strip()
        core = re.sub(r"^[，,。、；;：:\s]+|[，,。、；;：:\s]+$", "", core)
        if len(core) < 8:
            log("[fix] 整段为免责/元叙述话术 → 丢弃：%s" % txt[:40])
            continue
        if any(k in txt for k in META):
            # 有实质内容就剥掉免责部分，剩不下什么就整段丢
            log("[fix] 元叙述免责 → 剥离：%s" % txt[:40])
            txt = core
            if len(txt) < 8:
                continue
        if any(k in txt for k in PRED):
            # 预测句直接整段丢：盘前场次"没料"时最容易在这里露馅
            log("[fix] 预测句式 → 丢弃：%s" % txt[:40])
            continue
        s["text"] = txt
        keep.append(s)
    return keep


def _fix_relative_dates(segs, F):
    """把 LLM 顺嘴说的相对日期换成真实日期（V1.8.3 后处理兜底）。

    ⚠️ 为什么 prompt 禁令不够：system 与 user 两处都写了「禁止自己换算星期/说成上周X」，
      实测口语化人设（尤其 retail/talk，要"像真人说话"）仍会顺嘴带出
      「昨天那场」「周一那天」—— 人设的"口语化"诉求与"日期准确"直接冲突，
      而人设优先于附加约束。**所以代码层必须兜**：检测相对日期 → 替换为绝对日期。
      只在 A股 休市（复盘）场次生效；开市场次说"今天"是对的，不动。
    """
    # ⚠️ `(F.get("mk") or "cn")` 是错的：mk=None（全球速览场）时会被 or 兜成 "cn"，
    #   于是走进 A股 复盘分支。必须直接判 `F.get("mk") != "cn"`。
    if not segs or F.get("mk") != "cn" or F.get("cn_fresh"):
        return segs
    d = str(F.get("date") or "")
    try:
        _dt = dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
    except Exception:                                                # noqa: BLE001
        return segs
    wd = "一二三四五六日"[_dt.weekday()]
    abs_md = "%d月%d日" % (_dt.month, _dt.day)
    abs_full = "%d年%d月%d日" % (_dt.year, _dt.month, _dt.day)
    # ⚠️ 负向断言（防过度替换，实测踩到）：若该段**已经含正确日期**，整段不动。
    #   否则「2026年9月30日（周三）」会被 `周[一二三四五六日]` 啃成
    #   「2026年9月30日（2026年9月30日）」——把对的改成错的，比不改更糟。
    #   ⚠️ 这里不能用单个 `%d年?%d月%d日` —— 那样会编译成 `2026年?9月30日`，
    #   "年"变成可选后 "2026" 后面必须紧跟 "9"，匹配不到「9月30日（周三）」这种简写。
    #   故用两个 alternation：带年份的 与 纯月日的，任一命中即放过。
    guard = re.compile(r"(?:%d年%d月%d日)|(?:%d月%d日)"
                       % (_dt.year, _dt.month, _dt.day, _dt.month, _dt.day))
    # 相对日期 → 绝对日期。顺序要紧：先长后短，避免"上周五"被"上周"先吃掉。
    pats = [(re.compile(r"上(?:个)?(?:周|星期)[一二三四五六日]"), abs_md),
            (re.compile(r"(?:周|星期)[一二三四五六日](?:那天|这天|收盘|的盘)"), abs_full),
            (re.compile(r"周[一二三四五六日]"), abs_full),
            (re.compile(r"昨天|前天|昨日"), abs_md)]
    n = 0
    for s in segs:
        t = s.get("text") or ""
        o = t
        # 已有正确日期 → 只跳过「相对日期替换」（它会把对的改成错的），
        # 但**叠字压缩仍要跑**（LLM 会写「9月30号2026年9月30日」这种叠字，
        #  它含正确日期，若整段 continue 就压不掉）。
        if not guard.search(t):
            for pat, rep in pats:
                t = pat.sub(rep, t)
        t = _dedup_date(t, _dt.year, _dt.month, _dt.day)
        if t != o:
            s["text"] = t
            n += 1
    if n:
        log("[fix] 相对日期归一 %d 段（%s 周%s）" % (n, abs_md, wd))
    return segs


def _parse_llm(txt):
    """把 LLM 段落切成 {t,text,tag}。用段数均分时间轴。
    （api 引擎不出 refs → 页面自动不渲染标签行，不报错。）"""
    paras = [x.strip() for x in re.split(r"\n\s*\n", txt.strip()) if x.strip()]
    if len(paras) == 1:
        paras = [x.strip() for x in re.split(r"\n", txt.strip()) if x.strip()]
    n = len(paras)
    step = 6 if n > 6 else 8
    out = []
    for i, s in enumerate(paras):
        s = re.sub(r"^\s*(?:\d+[.、)]\s*|[-*]\s*)", "", s).strip()
        out.append(_seg(i * step, s, "解说"))
    return out


if __name__ == "__main__":
    main()
