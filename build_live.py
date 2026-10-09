#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 一期 渲染层
=====================================================================
职责：snapshot.json + commentary_<key>.json → 单文件静态页 live.html
设计：
  * 「伪直播」= 按 segment.t（秒）排队上屏，逐条淡入 + 自动滚动；可暂停/倍速/拖进度
  * 人设切换 = 纯前端，切commentary 数据重放（不需要重新请求）
  * 视觉：卧龙台 luxury 金墨象牙白（配色 token 锁死，见技术资源手册 §9.3，不得改）
  * 零依赖：echarts 用相对路径 vendor/，file:// 直接可开
运行：python3 build_live.py
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(line_buffering=True)


def log(m):
    print(m, flush=True)


# V1.9.11：渲染层兜底——把解说流（含历史滚动 feed）里的任何中文读数强制转回阿拉伯数字。
# narrate 管线已做一遍，这里再兜一层，确保修复前写入 history 的中文读数也绝不上页。
from narrate import _cn_to_arabic_in_text, _strip_dangling_sign


def _clean_commentary_text(c):
    """就地把一条 commentary 的所有 segment 正文里的中文读数转回阿拉伯数字（幂等）。

    ⚠️ V1.9.33：再加一层「悬空正负号」清理 —— 修复前 _fix_market_pct 删数字时把正负号
    留在原位（正文出现「创业板指-，科创50-」「印制电路板-、半导体材料-」「有机硅+」），
    这类残缺已写进 commentary_history.json，且会被 narrate 回灌 prompt 当「上一轮口播」，
    所以渲染层必须兜住（与 narrate 共用 _strip_dangling_sign，单一实现）。
    """
    if isinstance(c, dict):
        for s in (c.get("segments") or []):
            if isinstance(s, dict) and "text" in s:
                t = _cn_to_arabic_in_text(s.get("text") or "")
                s["text"] = _strip_dangling_sign(t)
    return c


VERSION = "1.9.37"


def load_json(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _real_refresh_secs():
    """测真实「页面重建间隔」：每次 build 写时间戳，与上次做差；
    只采纳 [60,3600]s 区间（手动连发 <60 的抖动不污染），取近 5 次中位数。
    返回真实样本的中位数；无样本返回 0（页面回退用配置档位）。"""
    import statistics
    meta_p = os.path.join(HERE, "data", "loop_meta.json")
    now = time.time()
    samples, prev = [], None
    try:
        with open(meta_p, encoding="utf-8") as f:
            m = json.load(f)
        prev = m.get("last_ts")
        samples = m.get("samples", [])
    except Exception:                                        # noqa: BLE001
        pass
    if prev is not None:
        d = now - prev
        if 60 <= d <= 3600:
            samples.append(d)
            samples = samples[-5:]
    try:
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump({"last_ts": now, "samples": samples}, f)
    except Exception:                                        # noqa: BLE001
        pass
    return round(statistics.median(samples)) if samples else 0


# ============================================================
# 本场统计（状况剖面）· 市场维度计算
#   框架兼容 A / H / 美股三市场；各市场按数据可获得性裁剪维度。
#   - 强度：本市场指数当日涨跌幅均值
#   - 广度：A股用涨跌家数；港股用成分股涨/跌近似（proxy）
#   - 情绪：A股用涨停/跌停比；港股无涨跌停 → N/A
#   - 外围：global_ticker 剔除本市场自身后的均值偏度
#   - 日内走势：主指数分时 → 5 分钟 K 线（V1.9.22 起，替换原四点骨架）
#   卡内布局：2×2 瓦片（强度/广度/情绪/外围）+ K 线；市场名走 panel 头部 tab，
#   数据日期时间写在 K 线下方说明文字（V1.9.24）
# ============================================================
def _kl_dt(day, hhmm):
    """日内 K 线数据时间 → "YYYY-MM-DD HH:MM"（V1.9.24：走势图下方说明文字用）。

    日期取分时接口自带的交易日（腾讯 data.date，形如 "20261008"），
    时间取**最后一根 K 线所在分钟柱**（HHMM）—— 与图同源，说明文字描述的就是这张图；
    不取行情快照 asof：那是「源最后报价时间」，收盘后会停在盘后刷新时刻
    （实测上证 15:35:31）而图只到 15:00，两者混用会假性「数据更新鲜」。
    任一缺失 → None（前端退回 asof 或显示 "—"）。
    """
    d = str(day or "").strip()
    t = str(hhmm or "").strip()
    if len(d) == 8 and d.isdigit() and len(t) == 4 and t.isdigit():
        return "%s-%s-%s %s:%s" % (d[:4], d[4:6], d[6:], t[:2], t[2:])
    return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _bar(v, half):
    """带符号指标 v → 0..1 位置（0.5=中性）；half=取满偏时的量纲。无数据返回 None。"""
    if not isinstance(v, (int, float)):
        return None
    t = _clamp(v / half, -1.0, 1.0)
    return {"pos": 0.5 + t * 0.5, "bull": v > 0}


def _fmt_pct(v):
    return ("%+.2f%%" % v) if isinstance(v, (int, float)) else "—"


def _state_of(s):
    if not isinstance(s, dict):
        return ""
    return s.get("status") or s.get("state") or ""


def _fmt_idx(idxs):
    return " · ".join("%s %+.2f%%" % (x.get("name"), x["pct"])
                      for x in idxs if isinstance(x.get("pct"), (int, float)))


def _shape(main):
    o, h, l, c = (main.get(k) for k in ("open", "high", "low", "price"))
    if not all(isinstance(v, (int, float)) for v in (o, h, l, c)):
        return None
    if not (h > l):
        return None
    return {"open": o, "high": h, "low": l, "close": c,
            "pos": (c - l) / (h - l), "bull": c >= o}


def compute_market_stats(snap):
    mk = snap.get("markets") or {}
    breadth = snap.get("breadth") or {}
    hk = snap.get("hk_stocks") or []
    glob = snap.get("global_ticker") or []
    sess = (snap.get("session") or {}).get("all") or {}
    intra = snap.get("intraday") or {}
    vix = snap.get("vix") or {}
    res = {}
    # V1.9.34：RRG 跨资产轮动象限（只读 data/rrg_state.json，由 collect_rrg.py 落盘）
    _rrg_full = load_json(os.path.join(HERE, "data", "rrg_state.json")) or {}
    _rrg_assets = _rrg_full.get("assets") or []
    _rrg_date = _rrg_full.get("data_date")
    RRG_URL = "https://yangxiaa.cc/rrg/"

    def _kl(mk_key):
        d = intra.get(mk_key) or {}
        cd = d.get("candles")
        if not cd:
            return None
        # dt：走势图下方说明文字用的「数据日期时间」（与图同源，V1.9.24）
        return {"candles": cd, "prec": d.get("prec"), "name": d.get("name"),
                "dt": _kl_dt(d.get("date"), cd[-1][0])}

    # ---------------- A股 ----------------
    cn_idx = [x for x in (mk.get("cn") or []) if isinstance(x, dict)]
    if cn_idx:
        pcts = [x["pct"] for x in cn_idx if isinstance(x.get("pct"), (int, float))]
        stg = (sum(pcts) / len(pcts)) if pcts else None
        up = breadth.get("up"); down = breadth.get("down")
        lu = breadth.get("limit_up"); ld = breadth.get("limit_down")
        br = ((up - down) / (up + down)) if isinstance(up, int) and isinstance(down, int) and (up + down) > 0 else None
        sent = ((lu - ld) / (lu + ld)) if isinstance(lu, int) and isinstance(ld, int) and (lu + ld) > 0 else None
        cn_names = {x.get("name") for x in cn_idx}
        ext = [g["pct"] for g in glob if g.get("name") not in cn_names and isinstance(g.get("pct"), (int, float))]
        ext_avg = (sum(ext) / len(ext)) if ext else None
        res["cn"] = {
            "label": "A股", "state": _state_of(sess.get("cn")),
            "asof": (cn_idx[0].get("asof") or "")[-8:],
            "strength": {"v": stg, "bar": _bar(stg, 2), "sub": _fmt_idx(cn_idx)},
            "breadth": {"up": up, "down": down, "lu": lu, "ld": ld, "bar": _bar(br, 1), "proxy": False,
                        "sub": ("涨 %s / 跌 %s · 涨停 %s 跌停 %s" % (up, down, lu, ld))
                        if isinstance(up, int) else None},
            "sentiment": {"bar": _bar(sent, 1), "proxy": False,
                          "sub": ("涨停 %s / 跌停 %s" % (lu, ld)) if isinstance(lu, int) else None},
            "external": {"v": ext_avg, "bar": _bar(ext_avg, 2),
                         "sub": ("外围均值 %s" % _fmt_pct(ext_avg)) if ext_avg is not None else None},
            "shape": _shape(cn_idx[0]),
            "kl": _kl("cn"),
        }

    # ---------------- 港股 ----------------
    hk_idx = [x for x in (mk.get("hk") or []) if isinstance(x, dict)]
    if hk_idx:
        pcts = [x["pct"] for x in hk_idx if isinstance(x.get("pct"), (int, float))]
        stg = (sum(pcts) / len(pcts)) if pcts else None
        hu = sum(1 for x in hk if isinstance(x.get("pct"), (int, float)) and x["pct"] > 0)
        hd = sum(1 for x in hk if isinstance(x.get("pct"), (int, float)) and x["pct"] < 0)
        br = ((hu - hd) / (hu + hd)) if (hu + hd) > 0 else None
        hk_names = {x.get("name") for x in hk_idx}
        ext = [g["pct"] for g in glob if g.get("name") not in hk_names and isinstance(g.get("pct"), (int, float))]
        ext_avg = (sum(ext) / len(ext)) if ext else None
        res["hk"] = {
            "label": "港股", "state": _state_of(sess.get("hk")),
            "asof": (hk_idx[0].get("asof") or "")[-8:],
            "strength": {"v": stg, "bar": _bar(stg, 2), "sub": _fmt_idx(hk_idx)},
            "breadth": {"up": hu, "down": hd, "lu": None, "ld": None, "bar": _bar(br, 1), "proxy": True,
                        "sub": ("个股 涨 %d / 跌 %d（近似）" % (hu, hd)) if (hu + hd) > 0 else None},
            "sentiment": {"bar": None, "proxy": True, "sub": None},
            "external": {"v": ext_avg, "bar": _bar(ext_avg, 2),
                         "sub": ("外围均值 %s" % _fmt_pct(ext_avg)) if ext_avg is not None else None},
            "shape": _shape(hk_idx[0]),
            "kl": _kl("hk"),
        }

    # ---------------- 美股 ----------------
    # 数据可得性：三大指数（道琼/纳指/标普）有；全市场涨跌家数暂无稳定源（streetstats 实测为 SPA 不可取）。
    # 情绪维度已接入 CBOE VIX（252 日分位）→ 低分位=偏多(金)、高分位=偏空(灰)。
    # 夜间美股交易时段，导播台/统计卡自动切到本市场。
    us_idx = [x for x in (mk.get("us") or []) if isinstance(x, dict)]
    if us_idx:
        pcts = [x["pct"] for x in us_idx if isinstance(x.get("pct"), (int, float))]
        stg = (sum(pcts) / len(pcts)) if pcts else None
        us_names = {x.get("name") for x in us_idx}
        ext = [g["pct"] for g in glob if g.get("name") not in us_names and isinstance(g.get("pct"), (int, float))]
        ext_avg = (sum(ext) / len(ext)) if ext else None
        # VIX 情绪维度（覆盖旧 N/A 占位）
        vix_bar = None
        vix_sub = "VIX 情绪维度待接入"
        if isinstance(vix.get("pctile"), (int, float)):
            vp = vix["pctile"]  # 0..1 分位
            vix_bar = {"pos": _clamp(1 - vp, 0, 1), "bull": vp < 0.5}
            vix_sub = "VIX %s（252日分位 %d%%）" % (_fmt_pct(vix.get("chg")), int(vp * 100))
        # V1.9.25：美股广度优先用 DeanFi S&P500 免费源（snap.us_breadth_deanfi），失败回退待接入
        db = snap.get("us_breadth_deanfi")
        if isinstance(db, dict) and isinstance(db.get("advances"), int) and isinstance(db.get("declines"), int):
            _adv = db["advances"]; _dec = db["declines"]
            _br = ((_adv - _dec) / (_adv + _dec)) if (_adv + _dec) > 0 else None
            _ma = db.get("above_200ma_pct")
            _breadth = {"up": _adv, "down": _dec, "lu": None, "ld": None,
                        "bar": _bar(_br, 1), "proxy": False,
                        "note": ("S&P500 · >200MA %.0f%%" % _ma) if isinstance(_ma, (int, float)) else "S&P500"}
        else:
            _breadth = {"bar": None, "proxy": True,
                        "sub": "美股广度数据待接入（DeanFi 暂不可达）"}

        res["us"] = {
            "label": "美股", "state": _state_of(sess.get("us")),
            "asof": (us_idx[0].get("asof") or "")[-8:],
            "strength": {"v": stg, "bar": _bar(stg, 2), "sub": _fmt_idx(us_idx)},
            "breadth": _breadth,
            "sentiment": {"bar": vix_bar, "proxy": False, "vix": vix.get("last"),
                          "pctile": vix.get("pctile"), "sub": vix_sub},
            "external": {"v": ext_avg, "bar": _bar(ext_avg, 2),
                         "sub": ("外围均值 %s" % _fmt_pct(ext_avg)) if ext_avg is not None else None},
            "shape": _shape(us_idx[0]),
            "kl": _kl("us"),
        }
    # V1.9.34：把 RRG 跨资产象限按市场切片挂到统计卡（替换原「外围均值」瓦片内容）
    _RRG_MAP = {"cn": ("沪深300", "创业板"), "hk": ("恒生",), "us": ("标普500", "纳指")}
    def _rrg_slice(mk_key):
        if not _rrg_assets:
            return None
        _names = _RRG_MAP.get(mk_key, ())
        _sl = [{"asset": a["asset"], "quadrant": a["quadrant"], "cat": a.get("category")}
               for a in _rrg_assets if a["asset"] in _names]
        if not _sl:
            return None
        return {"assets": _sl, "data_date": _rrg_date, "url": RRG_URL, "available": True}
    for _k in ("cn", "hk", "us"):
        if _k in res:
            res[_k]["rrg"] = _rrg_slice(_k)
    return res


def build(snap, commentaries, out_html, ver=VERSION, tier="auto", refresh=0):
    idx = snap.get("index") or {}
    order = ["000001", "399001", "399006", "000688"]
    idx_cards = []
    for code in order:
        v = idx.get(code)
        if not v or not v.get("name"):
            continue
        pct = v.get("pct")
        cls = "up" if isinstance(pct, (int, float)) and pct > 0 else "down" if isinstance(pct, (int, float)) and pct < 0 else ""
        idx_cards.append({
            "name": v["name"],
            "price": ("%.2f" % v["price"]) if isinstance(v.get("price"), (int, float)) else "—",
            "pct": ("%+.2f%%" % pct) if isinstance(pct, (int, float)) else "—",
            "cls": cls,
        })

    # A股微观数据（涨跌家数/榜/涨停池）是否「今日」的：页面对陈旧数据必须显式标注，
    # 不能拿上一个交易日的个股数据顶着当下场次展示（V1.8.0，节假日尤其明显）
    today = time.strftime("%Y%m%d")
    cn_fresh = (str(snap.get("date") or "") == today)

    # ---- 全球资讯时间显示（V1.9.9，师傅要求）----
    # 数据层给的是 "MM-DD HH:MM"（markets / eulerpool / 东财三处格式已统一）。
    # 既然只保留 24h 内，就没必要再打头写日期：
    #   今天 → "今天 15:05"；昨天 → "昨天 22:30"；更早（理论上不会）→ 退回 "MM-DD HH:MM"
    _now = time.localtime()
    _prev = time.localtime(time.time() - 86400)
    _cur_md = "%02d-%02d" % (_now.tm_mon, _now.tm_mday)
    _yest_md = "%02d-%02d" % (_prev.tm_mon, _prev.tm_mday)

    def _fmt_news_time(t):
        t = str(t or "").strip()
        m = re.match(r"^(?:\d{4}-)?(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", t)
        if not m:
            return t
        md = "%s-%s" % (m.group(1), m.group(2))
        hm = "%s:%s" % (m.group(3), m.group(4))
        if md == _cur_md:
            return "今天 " + hm
        if md == _yest_md:
            return "昨天 " + hm
        return t

    news_cards = []
    for x in (snap.get("news") or []):
        y = dict(x)
        y["time"] = _fmt_news_time(x.get("time"))
        news_cards.append(y)

    # V1.9.11：渲染层兜底——解说流（当前轮 + 历史滚动 feed）里的中文读数强制转阿拉伯数字。
    # narrate 管线已做过一遍，这里再兜一层，确保修复前写入 history 的中文读数也绝不上页。
    commentaries = [_clean_commentary_text(c) for c in (commentaries or [])]
    data = {
        "date": snap.get("date"),
        "today": today,
        "cn_fresh": cn_fresh,
        "generated_at": snap.get("generated_at"),
        "index": idx_cards,
        "markets": snap.get("markets") or {},
        "global_ticker": snap.get("global_ticker") or [],
        "news": news_cards,
        "session": snap.get("session") or {},
        "game": snap.get("game") or {},
        "breadth": snap.get("breadth"),
        "sectors": snap.get("sectors"),
        "gainers": (snap.get("gainers") or {}).get("rows") or [],
        "losers": (snap.get("losers") or {}).get("rows") or [],
        "active": (snap.get("active") or {}).get("rows") or [],
        "zt": snap.get("zt"),
        "dt": snap.get("dt"),
        "warnings": snap.get("warnings") or [],
        "sources": snap.get("sources") or [],
        "commentary": commentaries,
        "refresh_tier": tier,
        "refresh_interval": refresh,
        "real_refresh_secs": _real_refresh_secs(),
        # V1.9.8：滚动解说流（最近 ~1 小时，循环每轮整体覆盖 commentary_*.json；
        #   页面据此前置「接续」而非整页重载）。每轮 narrate 已裁到 1h/CAP。
        # ⚠️ V1.9.11：history feed 里可能含修复前（中文读数）的旧轮，渲染层强制转阿拉伯数字。
        "feed": [_clean_commentary_text(c)
                 for c in (load_json(os.path.join(HERE, "data", "commentary_history.json")) or [])],
        "build_mtime": time.time(),
        # V1.9.21：本场统计（状况剖面）—— 各市场维度由 compute_market_stats 现算，
        # 零客户端解析；AH 已填充，美股（三大指数）亦已接入（广度/VIX 待补）。
        "market_stats": compute_market_stats(snap),
    }

    src_txt = "、".join(sorted(set(
        [s.split(":")[0] for s in data["sources"]] +
        ([data["session"].get("src")] if data["session"].get("src") else [])
    ))) or "—"

    with open(os.path.join(HERE, "_live_shell.html"), encoding="utf-8") as f:
        tpl = f.read()
    html = (tpl
            .replace("__TITLE__", "股市直播")
            .replace("__VER__", "v" + ver)
            .replace("__DATE__", str(snap.get("date") or ""))
            .replace("__SRC__", src_txt)
            .replace("__DATA__", json.dumps(data, ensure_ascii=False)
                     .replace("</", "<\\/")))
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    # V1.9.8：另写一份纯数据 JSON（供页面就地接续刷新，避免整页重载打断直播）。
    #   页面轮询它、比对 build_mtime，仅在真正新一轮时才局部更新。
    try:
        with open(os.path.join(HERE, "data", "live_data.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:                                            # noqa: BLE001
        log("[warn] live_data.json 写失败：%s" % e)
    return out_html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.path.join(HERE, "data", "snapshot.json"))
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--out", default=os.path.join(HERE, "live.html"))
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--tier", default="auto", help="刷新档位：active|calm|manual|auto（仅展示用）")
    ap.add_argument("--refresh", type=int, default=0, help="刷新间隔秒数（仅展示用）")
    ap.add_argument("--allow-local", action="store_true",
                    help="放行 local-synth 本地模板产物（默认拒绝，页面只收 AI 产物）")
    ap.add_argument("--allow-stale", action="store_true",
                    help="放行陈旧解说词（默认拒绝：解说比行情旧超过 --max-age 就不上页）")
    ap.add_argument("--max-age", type=int, default=2700,
                    help="解说比行情旧多少秒判为陈旧（默认 2700s=45min，见注释里的取舍）")
    ap.add_argument("--allow-partial", action="store_true",
                    help="放行残缺快照（默认拒绝：见下方快照健康守卫）")
    a = ap.parse_args()

    snap = load_json(a.snapshot)
    if not snap:
        log("[x] 没有 snapshot，先跑 collect_live.py")
        sys.exit(1)

    # ⚠️ V1.9.15 快照健康守卫（fail-safe）
    #   背景：snapshot.json 是多个进程「接力」写的 —— collect_live.py 每轮先**整份重写**
    #   （只含 A股字段，没有 markets/news/session），紧随其后的 markets.py 才把这些字段补齐。
    #   若本脚本恰好读在这两步之间的窗口，就会用残缺快照重建页面：指数有、资讯 0 条、
    #   session 空 → 页面显示「指数数据暂不可用 / 资讯暂不可用 / 全场休息」，
    #   而且**把上一版好页面覆盖掉**（不可逆）。
    #   实测（2026-10-08 11:3x）：双循环并发期间页面挂掉，live_data.json 里
    #   index 4 条 / news 0 / session {} —— 正是这个签名。
    #   判据只取「markets.py 每轮必写、且不可能为空」的字段（session、markets）；
    #   故意不用 news/index —— 休市日或数据源失败时它们真的可能为空，拿它们当判据会误伤正常轮次。
    #   命中即拒绝重建：宁可页面停在上一版，也不上残缺数据。退出码 3 便于外层区分。
    _part = [k for k in ("session", "markets") if not snap.get(k)]
    if _part and not a.allow_partial:
        log("[x] 快照残缺（缺 %s）—— 疑似读到了 collect_live.py 与 markets.py 之间的"
            "半成品快照，拒绝重建，保留上一版 live.html（--allow-partial 可强制放行）"
            % "/".join(_part))
        sys.exit(3)

    commentaries = []
    for fn in sorted(os.listdir(a.data)):
        # ⚠️ V1.9.8：commentary_history.json 是「滚动接续 feed」（list 结构），
        #   不是单轮解说（dict），必须排除，否则 c.get() 崩。
        if fn == "commentary_history.json":
            continue
        if fn.startswith("commentary_") and fn.endswith(".json"):
            c = load_json(os.path.join(a.data, fn))
            if not isinstance(c, dict) or not c.get("segments"):
                continue
            # ⚠️ V1.8.3 引擎闸：只收 AI 产物。师傅要求「不要任何合成的内容」。
            #   engine=="local-synth" 是本地模板拼的（同一份 snapshot 套话术），
            #   混进页面会让"AI 解说"这个卖点变成假的。--allow-local 可临时放行（调试用）。
            eng = str(c.get("engine") or "")
            if not a.allow_local and not eng.startswith(("agnes", "deepseek", "qwen",
                                                          "gpt", "claude", "glm")):
                log("[skip] %s 引擎=%s 非 AI 产物，本轮不上页（--allow-local 可放行）"
                    % (fn, eng or "(空)"))
                continue
            # ⚠️ V1.8.3 新鲜度闸：解说词的 generated_at 必须与「本轮行情」同轮。
            #   基准时间用 `session.checked_at`（markets.py 每轮刷新）而**不是**
            #   snapshot.generated_at —— 后者是 A股全市场采集时点，A股休市时不更新，
            #   实测停在 10-05 19:08（17 小时前），会把刚生成的解说全判成陈旧。
            #   判据：解说比行情旧 > 阈值就不上页（--allow-stale 可放行）。
            #   阈值 45min 而非 10min：手动跑一次 narrate.py（不重启循环）时，
            #   行情停在上一轮而解说刚生成，差 20~40min 是正常的 —— 10min 阈值会
            #   把正常的手动产物全判陈旧（实测 26min 被误杀）。45min 仍能拦住
            #   "循环挂了、页面一直显示旧解说"这种真正该拦的情况（那会跨小时）。
            g = str(c.get("generated_at") or "")
            base = str((snap.get("session") or {}).get("checked_at") or "")
            base = base[:16] if len(base) >= 16 else str(snap.get("generated_at") or "")
            try:
                age = (time.mktime(time.strptime(g, "%Y-%m-%d %H:%M:%S"))
                       - time.mktime(time.strptime(base, "%Y-%m-%d %H:%M")))
            except Exception:                                        # noqa: BLE001
                age = 0
            if age > a.max_age and not a.allow_stale:
                log("[skip] %s 引擎=%s 解说比行情旧 %.0f 分钟（阈值 %d），判为陈旧，不上页"
                    % (fn, eng, age / 60.0, a.max_age))
                continue
            c["is_ai"] = True
            commentaries.append(c)
    if not commentaries:
        log("[x] 没有 AI 解说词（engine 非 LLM）。先跑："
            "narrate.py --engine api --preset agnes")
        sys.exit(2)

    # 合并人设形象字段（initials / role / one / slogan）—— 让页面能画"虚拟解说员"
    personas = {}
    pdir = os.path.join(HERE, "personas")
    for fn in sorted(os.listdir(pdir)):
        if fn.endswith(".json"):
            p = load_json(os.path.join(pdir, fn))
            if p:
                personas[p["key"]] = p
    # V1.8.5：先只留一个人设（杨夏/散户嘴替）。等这一条链路（人设→prompt→LLM→页面）
    # 跑通、师傅认可声纹之后，再往 personas/ 加文件 —— order 里补一个 key 即可。
    # 原 order = ["pro", "talk", "retail"]（解说员·陈播/段云飞/赵小明）已下线，
    # 历史三人设备份（personas_v183_backup/）已于 2026-10-08 清理时删除。
    order = ["yx"]
    for c in commentaries:
        src = personas.get(c.get("persona")) or {}
        for k in ("initials", "role", "one"):   # slogan 已不再渲染（V1.6.0）
            c[k] = src.get(k, "")
        c["name"] = src.get("name", c.get("name"))
    commentaries.sort(key=lambda c: order.index(c["persona"])
                       if c.get("persona") in order else 99)

    log("载入人设 %d 个：%s" % (len(commentaries),
                             "、".join("%s(%s)" % (c["short"], c["name"]) for c in commentaries)))

    p = build(snap, commentaries, a.out, a.version, tier=a.tier, refresh=a.refresh)
    log("[ok] 生成 %s（%.0f KB）· 刷新档位=%s/%ds" % (
        p, os.path.getsize(p) / 1024, a.tier, a.refresh))


if __name__ == "__main__":
    main()
