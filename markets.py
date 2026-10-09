#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 三市场扩展层
=====================================================================
职责：A 股 / 港股 / 美股 的指数行情 + 全球股指走马灯 + 财经快讯 + 时段判定。
输出：挂进 snapshot 的markets / global_ticker / news 三块，供页面渲染与解说层取用。
实测（2026-10-05）：港股与美股**没有可靠的全市场排行源**（东财 clist 的 fs 不含外盘，
  新浪 vip 排行只有 A 股节点），故港美股只做「指数 + 快讯」，异动榜/涨跌家数仅 A 股有。
  这是数据源的真实边界，不是实现偷懒—— 页面上会明确标注。
纪律：
  * 全部走新浪/腾讯单次批量请求（一把抓完，不逐个请求，避免限流）
  * 时段判定用 datetime 算，不用固定 UTC 偏移（美股夏令时会变）
运行：python3 markets.py --out data/snapshot.json --patch
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cn_tz  # noqa: E402,F401  —— 锚定进程时区为北京时间（V1.9.40 时区铁律）
from collect_live import http, SINA_REF, _log, _f# noqa: E402

# Nasdaq 官方 API 对默认 UA 会 403，必须带浏览器 UA（2026-10-06 实测）
UA_NASDAQ = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 指数
HK_IDX = [("hsi", "恒生指数", "rt_hkHSI"),
           ("hstech", "恒生科技", "rt_hkHSTECH"),
           ("hscei", "国企指数", "rt_hkHSCEI")]

# ⚠️ V1.9.6 港股个股（师傅：「为何没有港股个股的动态，一直只有指数」）。
#   根因：此前**只取了上面 3 个指数**，没有任何个股清单 —— 所以解说里"港股"只能
#   讲指数。代码里"港美股无全市场排行源"那句注释说的是**全市场涨跌幅榜**（东财
#   clist 的 fs 不含外盘），**不等于取不到个股** —— 新浪 rt_hkXXXXX 对具体代码
#   一律可取（上面的指数本身就是走这套），且 parse_hk 对个股/指数同一套字段序。
#   这里放**流动性最好的港股龙头**（不是全市场扫榜，见上），一次批量请求取回，
#   与指数同一个 sina_batch，零额外请求。代码 = 新浪 5 位（rt_hk + code）。
HK_STOCKS = [
    ("00700", "腾讯控股"), ("09988", "阿里巴巴-W"), ("03690", "美团-W"),
    ("09618", "京东集团-SW"), ("01810", "小米集团-W"), ("01211", "比亚迪股份"),
    ("00941", "中国移动"), ("01299", "友邦保险"), ("09999", "网易-S"),
    ("00388", "香港交易所"), ("02318", "中国平安"), ("01024", "快手-W"),
    ("09888", "百度集团-SW"), ("00981", "中芯国际"), ("00175", "吉利汽车"),
]

US_IDX = [("dji", "道琼斯", "gb_dji"),
           ("ixic", "纳斯达克", "gb_ixic"),
           ("inx", "标普500", "gb_inx")]
CN_IDX = [("sh", "上证指数", "sh000001"),
           ("sz", "深证成指", "sz399001"),
           ("cyb", "创业板指", "sz399006"),
           ("kc50", "科创50", "sh000688")]

# 全球走马灯（新浪一套代码全拿。A 股用 s_ 短格式，港股 rt_，美股 gb_，其余 int_）
GLOBAL = [("s_sh000001", "上证指数"), ("s_sz399001", "深证成指"), ("s_sz399006", "创业板指"),
          ("rt_hkHSI", "恒生指数"), ("rt_hkHSTECH", "恒生科技"),
          ("gb_dji", "道琼斯"), ("gb_ixic", "纳斯达克"), ("gb_inx", "标普500"),
          ("int_nikkei", "日经225"), ("int_ftse", "英国富时100"), ("int_bovespa", "巴西Bovespa")]


def sina_batch(codes):
    """新浪批量取，一次请求拿全部代码。返回 {code: 原始字段串}。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    raw = http(url, headers=SINA_REF, enc="gbk", retries=2, backoff=1.5)
    out = {}
    for line in raw.strip().split("\n"):
        if '="' not in line:
            continue
        k = line.split("=")[0].replace("var hq_str_", "").strip()
        out[k] = line.split('"')[1]
    return out


def _pct_of(s):
    """新浪字符串统一取涨跌幅百分比（0.28 表示 0.28%）。"""
    for x in s.split(","):
        v = _f(x)
        if v is not None:
            pass
    return None


def asof_dt(s):
    """新浪给的「报价时间」串 → datetime。
    ⚠️ 这是本项目的时段判定基石：行情源自己带的时间戳，比自建节假日表更硬
      （台风休市/临时停市/半日市都能自动识别，且零年度维护）。
    实测三种格式：A股 '2026-09-30 16:19:58'、港股 '2026/10/06 11:21:42'、美股 '2026-10-06 05:30:00'。"""
    if not s:
        return None
    t = str(s).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def parse_cn(fields):
    """A股 **长** 格式 sh000001：名,今开,昨收,现价,高,低,...,涨跌额,涨跌幅%,... [30]日期 [31]时间
    ⚠️ 别把短格式 s_sh000001 喂进来——那是另一套字段序，见 parse_cn_short。"""
    p = fields.split(",")
    if len(p) < 4:
        return None
    prev, price = _f(p[2]), _f(p[3])
    pct = ((price / prev - 1) * 100) if (prev and price) else None
    return {"name": p[0], "price": price, "prev": prev, "pct": pct,
            "open": _f(p[1]), "high": _f(p[4]), "low": _f(p[5]),
            "amount": _f(p[9]) if len(p) > 9 else None,
            "asof": (p[30] + " " + p[31]) if len(p) > 31 else None}


def parse_cn_short(fields):
    """A股 **短** 格式 s_sh000001：名,现价,涨跌额,涨跌幅%,成交量(万手),成交额(万)
    ⚠️ 实测踩坑：曾误当长格式解析（拿 p[2] 当现价）→ 走马灯显示「上证 -97.36%」这种鬼数。"""
    p = fields.split(",")
    if len(p) < 4:
        return None
    return {"name": p[0], "price": _f(p[1]), "chg": _f(p[2]), "pct": _f(p[3]),
            "amount": (_f(p[5]) or 0) * 1e4 or None if len(p) > 5 else None}


def parse_hk(fields):
    """港股 rt_ 格式：英文名,中文名,今开,昨收,最高,最低,现价,涨跌额,涨跌幅%,...,[17]日期 [18]时间"""
    p = fields.split(",")
    if len(p) < 9:
        return None
    price, chg, pct = _f(p[6]), _f(p[7]), _f(p[8])
    prev = (price - chg) if (price is not None and chg is not None) else None
    return {"name": p[1], "price": price, "pct": pct, "chg": chg, "prev": prev,
            "open": _f(p[2]), "high": _f(p[4]), "low": _f(p[5]),
            "amount": _f(p[11]) if len(p) > 11 else None,
            "asof": (p[17].replace("/", "-") + " " + p[18]) if len(p) > 18 else None}


def parse_us(fields):
    """美股 gb_ 格式：名,现价,涨跌幅%,[3]时间,涨跌额,今开,最高,最低,52高,52低,成交量,成交额"""
    p = fields.split(",")
    if len(p) < 8:
        return None
    return {"name": p[0], "price": _f(p[1]), "pct": _f(p[2]), "ts": p[3],
            "asof": p[3],
            "chg": _f(p[4]), "open": _f(p[5]), "high": _f(p[6]), "low": _f(p[7])}


def parse_int(fields):
    """全球指数 int_ 格式：名,点位,涨跌额,涨跌幅%"""
    p = fields.split(",")
    if len(p) < 4:
        return None
    return {"name": p[0], "price": _f(p[1]), "chg": _f(p[2]), "pct": _f(p[3])}


def parse_gb(fields):
    """美股指数 gb_ 格式：名,现价,涨跌幅%,时间,涨跌额,昨收,..."""
    # ⚠️ 与 A股长格式(parse_cn)字段序不同：gb_ 的 p[2] 直接是涨跌幅%(p[4] 是涨跌额)，
    #   若误走 parse_cn 会把 p[3](时间戳串)当现价 → price=None → pct=None 被丢弃
    #   （V1.9.30 前道指/纳指/标普三指数因此在走马灯消失）。
    p = fields.split(",")
    if len(p) < 5:
        return None
    return {"name": p[0], "price": _f(p[1]), "chg": _f(p[4]), "pct": _f(p[2])}


# ---------------------------------------------------------------- 美股兜底源（V1.8.9 新增）
# ⚠️ 起因：美股此前**只有新浪一个源**（A股是东财→腾讯→新浪三级），新浪一挂美股就断。
#   师傅问"为何不走路透外媒渠道"——实测结论（勿重复试错，2026-10-06 17:45 逐个curl）：
#     ❌ Yahoo Finance v8/v7 API → 403 sad-panda 反爬页，换 UA 无效
#     ❌ Stooq CSV → 404
#     ❌ Google Finance 全站 → 000 超时（**github.com/topics/google-finance 那批仓库
#        全是 cheerio/playwright 抓 HTML + Google Sheets 方案，本质依赖能否直连
#        google.com；本机不可达 → 这类方案一律不可用**）
#     ❌ Google News RSS → 000 超时
#     ⚠️ 东财 100.DJIA/100.NDX/100.SPX → 首次成功，连打3 次全000（且限流按 host 计，
#        卧龙台多项目共用）→ 不作主源
#   ✅ 可用：**Nasdaq 官方 API**（api.nasdaq.com，纳指；连打3 次稳定）
#   ✅ 可用：**腾讯 us***（qt.gtimg.cn，道指+标普+纳指；一次批量拿全）
#   注：Nasdaq API 只有 COMP(纳指综合) 通，INDU/SPX/DJI 全部返回 data:null（已逐个试过）。

def nasdaq_index():
    """Nasdaq 官方 API —— 只覆盖纳斯达克综合指数(COMP)。

    ⚠️ 实测边界（别扩大化）：`?assetclass=index` 只有 COMP 有数据，
       INDU / SPX / SP500 / DJI / DJIA / OEX 全部 `data: null`。
    ⚠️ lastTradeTimestamp 格式是 `Oct 5, 2026`（无时分），**只够判断"哪一天"，
       不能当 asof 用**（时段判定仍必须走新浪的精确到分的报价时间）。
    """
    url = ("https://api.nasdaq.com/api/quote/COMP/info?assetclass=index")
    raw = http(url, headers={"User-Agent": UA_NASDAQ, "Accept": "application/json"},
               timeout=12, retries=2, backoff=1.2)
    d = json.loads(raw) or {}
    data = d.get("data") or {}
    pd_ = data.get("primaryData") or {}
    price = _f(pd_.get("lastSalePrice"))
    pct = _f((pd_.get("percentageChange") or "").replace("%", "").replace("+", ""))
    if price is None:
        return {}
    return {"ixic": {"name": "纳斯达克", "price": price, "pct": pct,
                     "chg": _f((pd_.get("netChange") or "").replace(",", "").replace("+", "")),
                     "ts": pd_.get("lastTradeTimestamp"), "asof": None,
                     "open": None, "high": None, "low": None, "src": "nasdaq"}}


def tx_us_index():
    """腾讯 us* —— 道指/纳指/标普一次批量拿全，作新浪的兜底。

    腾讯字段序（本项目既有惯例，与 collect_live.tx_index 一致）：
      1名 2代码 3现价 4昨收 5今开 30时间 31涨跌额 32涨跌幅 33高 34低 37成交额(万)
    ⚠️ 腾讯与新浪**同源**（都是行情批发商转手），但字段/时间戳独立解析，
       实测可作兜底 —— 目的不是"多一个不同口径的源"，而是"新浪挂时不至于瞎"。
    """
    url = "https://qt.gtimg.cn/q=usDJI,usIXIC,usINX"
    raw = http(url, enc="gbk", timeout=12, retries=2, backoff=1.2)
    m = {"usDJI": "dji", "usIXIC": "ixic", "usINX": "inx"}
    out = {}
    for line in (raw or "").strip().split("\n"):
        if "=" not in line or '"' not in line:
            continue
        code = line.split("=")[0].replace("v_", "").strip()
        key = m.get(code)
        if not key:
            continue
        p = line.split('"')[1].split("~")
        if len(p) < 35:
            continue
        price = _f(p[3])
        if price is None:
            continue
        out[key] = {"name": p[1], "price": price, "pct": _f(p[32]), "chg": _f(p[31]),
                    "open": _f(p[5]), "high": _f(p[33]), "low": _f(p[34]),
                    "ts": p[30], "asof": p[30], "src": "tencent"}
    return out


def collect_indexes():
    """三市场指数 + **各市场报价时间戳**(asof)。港美股只走新浪（腾讯同源且字段少，无增益）。
    asof 是时段判定的基石：节假日时行情源会一直停在「上一个交易日」的时刻，
      这个陈旧度就是「今天没开市」的铁证，无需自建节假日表。"""
    out = {"cn": [], "hk": [], "us": []}
    warn = []
    # A 股：与主流程的指数源可能已取过，这里用新浪 s_ 轻量补齐（失败不影响）
    try:
        raw = sina_batch([c for _, _, c in CN_IDX])
        for k, nm, c in CN_IDX:
            if c in raw:
                d = parse_cn(raw[c])
                if d:
                    d["name"] = nm
                    out["cn"].append(d)
    except Exception as e:                                        # noqa: BLE001
        warn.append("A股指数(新浪)失败(%s)" % type(e).__name__)
    for key, lst, parser in (("hk", HK_IDX, parse_hk), ("us", US_IDX, parse_us)):
        try:
            raw = sina_batch([c for _, _, c in lst])
            for k, nm, c in lst:
                if c in raw:
                    d = parser(raw[c])
                    if d:
                        d["name"] = nm
                        d["key"] = k
                        d.setdefault("src", "sina")
                        out[key].append(d)
        except Exception as e:                                    # noqa: BLE001
            warn.append("%s指数失败(%s)" % (key, type(e).__name__))

    # ---- V1.8.9：美股兜底（新浪缺项时用 Nasdaq API / 腾讯 us* 补，不整份替换）----
    # ⚠️ 关键设计：**只补缺、不覆盖**。新浪给的 asof 精确到分（时段判定的基石），
    #   Nasdaq 的 lastTradeTimestamp 只有日期、腾讯的时间戳也有延迟 —— 谁更"新"
    #   由新浪说了算。兜底源的价值是"新浪整条挂掉时仍有报价"，不是"挑好看的口径"。
    have = {r.get("key") for r in out["us"]}
    missing = [k for k, _, _ in US_IDX if k not in have]
    if missing:
        _log("[--] 美股新浪缺 %s → 兜底源补" % "/".join(missing))
        back = {}
        if "ixic" in missing:
            try:
                back.update(nasdaq_index())
            except Exception as e:                                # noqa: BLE001
                warn.append("Nasdaq指数失败(%s)" % type(e).__name__)
        try:
            back.update(tx_us_index())
        except Exception as e:                                    # noqa: BLE001
            warn.append("腾讯美股失败(%s)" % type(e).__name__)
        nm_by_key = {k: nm for k, nm, _ in US_IDX}
        for k in missing:
            d = back.get(k)
            if not d:
                continue
            d = dict(d)
            d["key"] = k
            d["name"] = nm_by_key.get(k, d.get("name") or k)
            out["us"].append(d)
            _log("[ok] 美股兜底 %s ← %s %s（新浪无此项）"
                 % (d["name"], d.get("src", "?"), d.get("pct")))
    # 取该市场「最晚的一个」报价时间：休市时各指数一致陈旧，取 max 仍陈旧；
    # 盘中各指数一致新鲜，取 max 不会误判。全失败 → None（此时退回纯时钟判定）。
    # ⚠️ V1.8.9：**asof 只采信 src=="sina" 的条目**。时段判定（"今天开没开市"的
    #   唯一基石）靠 asof 的陈旧度判断，兜底源给的是**昨日收盘**时间（实测腾讯
    #   us* 停在 10-05 16:48，Nasdaq 只有日期无时分）—— 一旦混进来，休市日会被
    #   判成"报价停在上一个交易日"（还能对），但盘中日也可能因兜底条目更旧而被误判
    #   为休市 → **"看起来在交易其实没开"或反之**。宁可 asof=None 退回纯时钟，
    #   也不能用不可比的兜底时间戳去下确定性判决。
    asof = {}
    for k, rows in out.items():
        ts = [asof_dt(r.get("asof")) for r in rows
              if r.get("src", "sina") == "sina" and r.get("asof")]
        ts = [t for t in ts if t]
        asof[k] = max(ts) if ts else None
    return out, asof, warn


# ---------------------------------------------------------------- 日内分时 → K 线（V1.9.22 新增）
# 起因（师傅 2026-10-08）：本场统计卡的「日内走势」原是 open/high/low/close 四点骨架线，
#   太粗糙（看不出高开低走还是低开高走、看不出横盘/单边），改为 **5 分钟 K 线**。
#
# 数据源 = 腾讯分时（实测 2026-10-08，勿重复试错）：
#   ✅ A股  appstock/app/minute/query?code=sh000001   → 242 个 1 分钟点（9:30–15:00 全天）
#   ✅ 港股 appstock/app/minute/query?code=hkHSI      → 331 个点（含午休跳空 12:00→13:00）
#   ✅ 美股 appstock/app/usMinute/query?code=usDJI    → 391 个点，date=上一交易日
#   ❌ 美股不能走 minute/query（只回 1 行快照，实测 1396 字节）；mkline（真 OHLC）只认 A股
#   ❌ 东财 push2his trends2 同一时刻直连/代理均 RemoteDisconnected，不作主源
#   ⚠️ 域名用 proxy.finance.qq.com（web.ifzq.gtimg.cn 会 301 且不跟随）；
#      三个市场各 1 次请求、无 key、无限流，代价可忽略。
TX_MIN = [("cn", "sh000001", "minute", "上证指数"),
          ("hk", "hkHSI", "minute", "恒生指数"),
          ("us", "usDJI", "usMinute", "道琼斯")]
KLINE_BUCKET = 5          # 每根 K 线聚合的分钟数：A股 48 根 / 港股 66 根 / 美股 79 根
SESSION_CLOSE = {"cn": "1500", "hk": "1600", "us": "1600"}   # 各市场收盘分钟（尾行归位用）


def _snap_tail(pts, close_hm):
    """把「收盘后的最新报价占位行」时间戳归位到收盘时刻（V1.9.24）。

    实测（腾讯分时，2026-10-08 港股）：收盘后源仍保留一行最新报价，其**时间戳 = 抓取时刻**
    —— 同一价格两次抓取分别打 1712 / 1713、总行数恒为 332（覆盖而非追加），价格则是当日收盘价。
    若直接采信这一行：
      * 统计卡走势图下方的「数据时间」会显示 17:13 这种与图毫无关系、且每轮刷新都漂移的时间；
      * 收盘价的 K 线柱被钉在盘中序列之外的时刻上（图尾出现一根孤立柱）。
    规则：**只看尾行**，与前一行间隔 >10 分钟即判为占位行（真实盘中相邻分钟恒为 1 分钟；
    午休 12:00→13:00 的 60 分钟缺口在序列中间，不影响尾行判定）→ 时间戳改写为该市场收盘时刻，
    **价格原样保留**（它就是当日收盘价，改写的是标签不是数据）。A股/美股实测无此行，天然 no-op。
    """
    if len(pts) < 2 or not close_hm:
        return pts
    t_last, t_prev = str(pts[-1][0]), str(pts[-2][0])
    if not (len(t_last) == 4 and len(t_prev) == 4 and t_last.isdigit() and t_prev.isdigit()):
        return pts
    def _m(t):
        return int(t[:2]) * 60 + int(t[2:])
    if _m(t_last) - _m(t_prev) > 10:
        pts = list(pts)
        pts[-1] = (close_hm, pts[-1][1])
    return pts


def _minute_rows(node):
    """腾讯分时原始行 → [(HHMM, price)]。行格式 `HHMM price vol [amount]`（空格分隔）。"""
    dd = (node.get("data") or {}).get("data") or []
    out = []
    for line in dd:
        p = str(line).split()
        if len(p) < 2:
            continue
        t, px = p[0].strip(), _f(p[1])
        if len(t) == 4 and t.isdigit() and px is not None and px > 0:
            out.append((t, px))
    return out


def _candles(points, bucket=KLINE_BUCKET):
    """分钟点列 → K 线 [[HHMM, open, high, low, close]]。

    ⚠️ 按**数组顺序**分桶，不按时钟分桶。理由：港股 12:00→13:00 午休、美股/A股跨段
      边界若按时钟分桶会产出大量只有 1 个点的残桶，图上一根极宽一根极窄（忽胖忽瘦）。
      顺序分桶 = 「每连续 bucket 个交易分钟一根」，三市场都得等宽 K 线。
    ⚠️ 分时只给每分钟一个价格（=该分钟收盘价），故 high/low 取自桶内分钟价的极值
      —— 影线是「分钟级」而非「成交级」的高低，做形态阅读足够，但别当逐笔高低用。
    """
    out = []
    for i in range(0, len(points), bucket):
        chunk = points[i:i + bucket]
        px = [x[1] for x in chunk]
        out.append([chunk[-1][0], round(px[0], 2), round(max(px), 2),
                    round(min(px), 2), round(px[-1], 2)])
    return out


SNAP_PATH = os.path.join(HERE, "data", "snapshot.json")


def _prev_intraday():
    """上一轮快照里的 intraday（「同日回退」用）。读不到就当空 dict，绝不抛。"""
    try:
        with open(SNAP_PATH, encoding="utf-8") as f:
            d = json.load(f)
        v = d.get("intraday")
        return v if isinstance(v, dict) else {}
    except Exception:                                             # noqa: BLE001
        return {}


def collect_intraday():
    """三市场日内分时 → 5 分钟 K 线。逐个市场独立：任一失败只缺该项（页面降级显示 N/A）。

    ⚠️ V1.9.29 两处加固（师傅 2026-10-09：「统计台的日内走势数据为 N/A」）：
      ① **门槛 3 点 → 1 点**：腾讯分时在**开盘前几分钟**只回 1–2 个点（2026-10-09 实测：
         09:21 轮 1 点、09:28 轮 2 点，同一时刻接口本身正常，开盘后 09:34 已回 8 点）。
         旧口径 `len(pts) < 3` 直接判失败 → A股/港股整段 N/A，且要等下一轮（最长 300s）
         才可能恢复 —— 开盘那几分钟正好是直播最热闹的时候。现在有多少画多少（1 点也出图）。
      ② **同日回退**：若本轮点数**少于**上一轮同一市场、且两者**同一交易日**（date 相等），
         沿用上一轮那份更完整的。防的是盘中偶发抽风（本轮回 1 点）把已画好的整日图抹成
         N/A —— 一整天里最完整的图，不该被最残缺的那一轮覆盖。
         **跨交易日不沿用**：宁可 N/A，也不显示错日期的图（图下说明文字写的就是日期）。
    """
    prev = _prev_intraday()
    out, warn = {}, []
    for key, code, path, nm in TX_MIN:
        try:
            url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/%s/query?code=%s"
                   % (path, code))
            raw = http(url, headers={"Referer": "https://gu.qq.com/"}, timeout=10,
                       retries=2, backoff=1.2)
            d = json.loads(raw) or {}
            node = (d.get("data") or {}).get(code) or {}
            date_s = str((node.get("data") or {}).get("date") or "")
            pts = _snap_tail(_minute_rows(node), SESSION_CLOSE.get(key))   # 收盘后占位行归位
            cd = _candles(pts) if pts else []
            p = prev.get(key) or {}
            p_cd = p.get("candles") or []
            p_date = str(p.get("date") or "")
            if len(cd) < len(p_cd) and p_date and p_date == date_s:        # 同日回退（见 ②）
                out[key] = p
                _log("[..] %s分时本轮 %d 根 < 上一轮 %d 根（同日 %s）→ 沿用上一轮"
                     % (nm, len(cd), len(p_cd), p_date))
                continue
            if not cd:
                raise RuntimeError("分时点不足(%d)" % len(pts))
            qt = (node.get("qt") or {}).get(code) or []
            prec = _f(qt[4]) if len(qt) > 4 else None
            last = _f(qt[3]) if len(qt) > 3 else pts[-1][1]
            out[key] = {"name": nm, "date": date_s,
                        "prec": round(prec, 2) if prec else None,
                        "last": round(last, 2) if last else None,
                        "rows": len(pts), "candles": cd}
            _log("[ok] %s分时 %d 点 → %d 根 K 线（%s）"
                 % (nm, len(pts), len(cd), date_s or "-"))
        except Exception as e:                                    # noqa: BLE001
            warn.append("%s分时失败(%s)" % (nm, type(e).__name__))
            _log("[--] %s分时失败：%s" % (nm, e))
    return out, warn


# ---------------------------------------------------------------- VIX（美股情绪维度 · V1.9.22）
# 师傅点名的源：datahub.io/core/finance-vix 指向的就是 CBOE 官方日线 CSV，实测可用。
#   ✅ cdn.cboe.com → 307 → cdn-api.cboe.com（urllib 自动跟随），473KB、日频、含 OHLC
#   ⚠️ 直接 URL 必须带 `api/global/us_indices/daily_prices/VIX_History.csv`，
#      cdn.cboe.com 的 307 目标域是 cdn-api.cboe.com —— 写死前者即可，别抄 cdn-api。
#   ⚠️ 日频 + 文件大 → 本地缓存 6h，避免每轮 300s 重下 473KB（也少打人家 CDN）。
#   ⚠️ 情绪必须用**分位**而非绝对水平：VIX 无跨期可比性（2017 常态 10 上下、2020 常态 30
#      上下，同一张图里 15 到底算高算低根本说不清），故取最近 252 个交易日分位。
VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_CACHE = os.path.join(HERE, "data", "vix_cache.json")
VIX_TTL_H = 6
VIX_WINDOW = 252


def _vix_rows(text):
    """CBOE CSV → [{d,o,h,l,c}]。首行是表头 `DATE,OPEN,HIGH,LOW,CLOSE`，日期是 MM/DD/YYYY。"""
    rows = []
    for line in (text or "").strip().split("\n")[1:]:
        p = line.split(",")
        if len(p) < 5:
            continue
        try:
            d = dt.datetime.strptime(p[0].strip(), "%m/%d/%Y").date()
        except ValueError:
            continue
        vals = [_f(x) for x in p[1:5]]
        if vals[3] is None:
            continue
        rows.append({"d": d.isoformat(), "o": vals[0], "h": vals[1],
                     "l": vals[2], "c": round(vals[3], 2)})
    return rows


def collect_vix(force=False):
    """VIX 最新收盘 + 前收 + 252 交易日分位。缓存 6h；失败回退旧缓存（页面不因 CDN 抖动变空）。"""
    cache = {}
    try:
        with open(VIX_CACHE, encoding="utf-8") as f:
            cache = json.load(f) or {}
    except Exception:                                             # noqa: BLE001
        cache = {}
    fresh = False
    try:
        if force or (time.time() - float(cache.get("fetched") or 0) > VIX_TTL_H * 3600):
            raw = http(VIX_URL, timeout=15, retries=2, backoff=1.5)
            rows = _vix_rows(raw)
            if len(rows) < 60:
                raise RuntimeError("解析出的行数异常(%d)" % len(rows))
            cache = {"fetched": time.time(), "rows": rows[-VIX_WINDOW - 8:]}
            with open(VIX_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            fresh = True
    except Exception as e:                                        # noqa: BLE001
        _log("[--] VIX 拉取失败（用旧缓存）：%s" % e)
    rows = cache.get("rows") or []
    if len(rows) < 2:
        return None
    win = rows[-VIX_WINDOW:]
    closes = [r["c"] for r in win if isinstance(r.get("c"), (int, float))]
    if len(closes) < 10:
        return None
    last, prev = rows[-1]["c"], rows[-2]["c"]
    pctile = sum(1 for x in closes if x <= last) / float(len(closes))
    out = {"last": last, "prev": prev, "date": rows[-1]["d"],
           "chg": round(last - prev, 2),
           "pctile": round(pctile, 4), "n": len(closes),
           "lo": min(closes), "hi": max(closes)}
    _log("[%s] VIX %s 收 %.2f（%+.2f）· %d 日分位 %.0f%%%s"
         % ("ok" if fresh else "缓存", out["date"], last, last - prev,
            len(closes), pctile * 100, "（本轮新拉）" if fresh else ""))
    return out


DEANFI_BREADTH_URL = "https://r2.deanfi.com/advance-decline/daily_breadth.json"


def collect_deanfi_breadth():
    """DeanFi 免费市场广度（S&P 500 口径，来源 Yahoo Finance yfinance）。

    免费、无需鉴权、无频率限制（GitHub + Cloudflare R2 静态托管）。README 称盘中每
    15 分钟更新；日频快照为当日收盘口径。字段远比旧「待接入」丰富：涨跌家数、A-D 比、
    量能、新高新低、各均线突破占比。

    ⚠️ 宇宙只有 S&P 500（503 只），**不是 NYSE、也不是 HKEX**。对直播卡而言 S&P 500
    广度是美股最被引用的标准口径，比原始 NYSE 更适合做广度瓦片；前端 sub 已显式标注
    「S&P500」，避免与全市场混淆。

    ⚠️ 其 interpretation 文案有 bug（declines>advances 仍写 strength），一律不取，只用
    原始数值，解读层由 build_live.compute_market_stats 现算。

    失败不阻塞：返回 (None, warn)；调用方保留上轮缓存（markets.py main 已先 load 旧 snap）。
    """
    try:
        raw = http(DEANFI_BREADTH_URL, timeout=12, retries=2, backoff=1.5)
        d = json.loads(raw)
    except Exception as e:                                            # noqa: BLE001
        return None, "DeanFi 广度拉取失败(%s)" % e
    data = (d.get("data") or {}) if isinstance(d, dict) else {}
    ad = data.get("advances_declines") or {}
    ma = data.get("moving_averages") or {}
    nh = data.get("new_highs_lows") or {}
    adv = ad.get("advances"); dec = ad.get("declines")
    if not isinstance(adv, int) or not isinstance(dec, int):
        return None, "DeanFi 广度字段缺失(advances/declines)"
    out = {
        "advances": adv, "declines": dec,
        "unchanged": ad.get("unchanged"),
        "adv_pct": ad.get("advance_percentage"),
        "ad_ratio": ad.get("advance_decline_ratio"),
        "above_20ma_pct": (ma.get("above_20_day_ma") or {}).get("percentage"),
        "above_50ma_pct": (ma.get("above_50_day_ma") or {}).get("percentage"),
        "above_200ma_pct": (ma.get("above_200_day_ma") or {}).get("percentage"),
        "near_52w_high": nh.get("stocks_near_52w_high"),
        "near_52w_low": nh.get("stocks_near_52w_low"),
        "date": data.get("date"),
        "generated_at": (d.get("metadata") or {}).get("generated_at"),
        "universe": (d.get("metadata") or {}).get("universe", "S&P 500"),
    }
    return out, None


def collect_hk_stocks():
    """港股龙头个股实时行情（V1.9.6 新增）。

    数据源 = 新浪 rt_hkXXXXX（与 HK_IDX 同一套接口、同一套 parse_hk 字段序，
    **指数能解析，个股就也能解析** —— 此前缺的是"没去取"，不是"取不到"）。

    ⚠️ 为什么是"指定龙头"而不是"全市场扫榜"：
      港股**全市场**涨跌幅榜没有可靠免费源（东财 clist 的 fs 不含外盘），
      但**指定代码**逐个取完全可行。所以这里取流动性最好的 15 只龙头
      （权重股 + 科技 + 内银 + 保险），够代表港股情绪，且一次请求拿完。
    失败/非交易时段返回空 list，**不阻塞**指数与时段判定（旁路数据）。
    """
    out = []
    try:
        raw = sina_batch(["rt_hk" + c for c, _ in HK_STOCKS])
    except Exception as e:                                        # noqa: BLE001
        return [], "港股个股失败(%s)" % type(e).__name__
    for code, nm in HK_STOCKS:
        d = parse_hk(raw.get("rt_hk" + code, ""))
        if not d or d.get("pct") is None:
            continue
        d["name"] = nm            # 用我们自己的规范名（新浪给的英文名不便读）
        d["code"] = code
        d["src"] = "sina"
        out.append(d)
    # 涨跌幅从大到小 —— 解说和页面都关心"今天谁在动"
    out.sort(key=lambda x: -(x.get("pct") or 0))
    return out, None


def collect_global():
    """全球股指走马灯。"""
    try:
        raw = sina_batch([c for c, _ in GLOBAL])
        rows = []
        seen = set()
        for c, nm in GLOBAL:
            if c in raw and raw[c].strip():
                d = (parse_gb(raw[c]) if c.startswith("gb_")
                     else parse_int(raw[c]) if c.startswith("int_")
                     else parse_hk(raw[c]) if c.startswith("rt_")
                     else parse_cn_short(raw[c]) if c.startswith("s_")
                     else parse_cn(raw[c]))
                if d and d.get("pct") is not None:
                    rows.append({"name": nm, "price": d.get("price"),
                                 "pct": round(d["pct"], 2)})
                    seen.add(nm)
        return rows, None
    except Exception as e:                                        # noqa: BLE001
        return [], "全球指数失败(%s)" % type(e).__name__


TAG_RE = re.compile(r"<[^>]+>")


#外媒财经快讯源（2026-10-05 实测）
# 全部免费公开RSS，无需 key；每条都带原文链接（页面可跳转）。
# 已实测不可用（留给未来复测，不必重踩）：Yahoo Finance 403 / Investing.com 403 /
#   FT 502 / BBC 502 / SCMP 502 / Reuters 404 / SEC 403 / StockTwits 403 /
#   Google News RSS 超时(000) / Google Finance 全站不可达(000，见 V1.8.9 复测)。
# ⚠️ 沙箱走代理，境外站偶发 502/timeout —— 每源独立 try，单源挂不影响整体。
#⚠️ V1.8.9 剔除「市场脉搏」= mw_marketpulse：**死源**，HTTP200 但 pubDate 全是
#   2025 年（最新 2025-07-03），被 24h 新鲜度过滤全丢，白占一个请求位。
#   同一机构的 mw_topstories 是活的（当天 09:20 GMT），已验证保留。
FOREIGN_FEEDS = [
    ("CNBC", "财经", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC", "经济", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC", "全球", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
    ("MarketWatch", "头条", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("NPR", "商业", "https://feeds.npr.org/1006/rss.xml"),
    ("SeekingAlpha", "市场快讯", "https://seekingalpha.com/market_currents.xml"),
    # V1.8.9 新增：外媒财经播客 RSS —— CNBC 视频/音频条目的description 常含
    # 具体数字与时段（实测「美股盘前/收盘」类内容），补足盘中无 headline 时的素材。
    ("CNBC", "视频", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"),
]

# 国内源（中国本土信源）。两条不同 fastColumn 构成两个独立的中文快讯流：
#   102 = 7×24 泛财经；103 = 公司/产业。两者均为北京时间实时推送。
# ⚠️ V1.9.13 师傅要求「全球资讯里不能没有中国本土信源」——此前国内源只在外国源不足时
#   才补位（collect_news 的 len(out) < max(10, limit//2) 闸），外媒充足时整段被压掉，
#   页面看不到任何中文消息。现改为「始终保留在场」（见 collect_news 国内源块）。
DOMESTIC_FEEDS = [
    ("东财快讯", "7×24", "https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=20&req_trace=1"),
    ("东财财经", "公司", "https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=103&sortEnd=&pageSize=20&req_trace=1"),
]

TAG_RE = re.compile(r"<[^>]+>")
ENT_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#39|#x2019|#x2018|#8220|#8221);")


def _unescape(s):
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&nbsp;", " "), ("&#39;", "'"),
                 ("&#x2019;", "’"), ("&#x2018;", "‘"),
                 ("&#8220;", "“"), ("&#8221;", "”")):
        s = s.replace(a, b)
    return s


def _rss_items(xml):
    """取 RSS 2.0 的 item 或 Atom 的 entry。"""
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    if not items:
        items = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    return items


def _pick(blk, tag):
    m = re.search(r"<" + tag + r"[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</" + tag + r">",
                  blk, re.S)
    if not m:
        return ""
    v = TAG_RE.sub(" ", m.group(1))
    v = re.sub(r"\s+", " ", v).strip()
    return _unescape(v)


def _rss_time(blk):
    """返回 (显示文本, 可比较的北京时间 datetime|None)。"""
    import datetime as _dt
    bj = _dt.timezone(_dt.timedelta(hours=8))
    for t in ("pubDate", "published", "updated", "dc:date"):
        v = _pick(blk, t)
        if not v:
            continue
        try:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(v)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            d = d.astimezone(bj)
            return d.strftime("%m-%d %H:%M"), d
        except Exception:                                         # noqa: BLE001
            pass
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(:\d{2})?(Z|[+-]\d{2}:?\d{2})?", v)
        if m:
            try:
                d = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5)),
                                 tzinfo=_dt.timezone.utc)
                tzs = m.group(8)
                if tzs and tzs not in ("Z",):
                    tzs = tzs.replace(":", "")
                    sign = 1 if tzs[0] == "+" else -1
                    d -= _dt.timedelta(hours=sign*int(tzs[1:3]), minutes=sign*int(tzs[3:5]))
                d = d.astimezone(bj)
                return d.strftime("%m-%d %H:%M"), d
            except Exception:                                     # noqa: BLE001
                pass
    return "", None


def collect_news(limit=30, foreign_first=True, per_feed=4):
    """全球财经快讯。**每源限量 per_feed 条、然后多源交错排列**——
    ⚠️ 不限量的话 CNBC 的 3 个频道会一口气占满 30 条，页面看起来像"只有一个外媒"。
    交错后CNBC/MarketWatch/NPR/SeekingAlpha 都能露脸，才像"全球媒体"覆盖。
    ⚠️ V1.8.9：per_feed 2 → 4。原先 2 条/源 ×7 源 = 14 条天花板，且每源永远只能
    露 2 条脸（一批内容永远进不来）。提到 4 后上限 24，配合 history 累积更够用。"""
    warn = []
    out = []
    seen = set()
    buckets = []

    # ---------- 外媒 RSS：每源限量 ----------
    for src, chan, url in FOREIGN_FEEDS:
        got = []
        try:
            xml = http(url, retries=2, backoff=1.2, timeout=12)
            for blk in _rss_items(xml):
                title = _pick(blk, "title")
                link = _pick(blk, "link") or _pick(blk, "guid")
                if not title or not link.startswith("http"):
                    continue
                key = title[:40]
                if key in seen:
                    continue
                seen.add(key)
                ttxt, ts = _rss_time(blk)
                it = {"title": title[:140], "text": title[:90], "link": link,
                      "time": ttxt, "src": src, "chan": chan, "foreign": True}
                if ts:
                    it["_ts"] = ts.isoformat()
                    it["_tsdt"] = ts
                got.append(it)
                if len(got) >= per_feed:
                    break
        except Exception as e:                                    # noqa: BLE001
            warn.append("%s(%s)" % (src, type(e).__name__))
        if got:
            buckets.append(got)
    # 交错：第 1 条来自各源，再第 2 条来自各源…
    n_foreign = 0
    for i in range(per_feed):
        for b in buckets:
            if i < len(b):
                out.append(b[i])
                n_foreign += 1
                if len(out) >= limit:
                    break
        if len(out) >= limit:
            break

    # ---------- 国内源（中国本土信源）：始终保留在场，与外媒按时间统一排序交错 ----------
    # ⚠️ V1.9.13 修：去掉「外媒不足才补位」的闸 → 无论外媒多满，中国本土信源都恒定
    #   贡献若干条（domestic_quota），与外媒 RSS 一并进 _sort_and_filter_recent 按时间倒序交错。
    #   两条东财 column 各出 2 条，保证页面永远看得到中文快讯。
    import datetime as _dt
    _bj_tz = _dt.timezone(_dt.timedelta(hours=8))
    _dom_per = 2          # 每个国内源恒定贡献条数（两条东财 column 各 2，共 4）
    for src, chan, url in DOMESTIC_FEEDS:
        domestic_quota = _dom_per
        try:
            d = json.loads(http(url, retries=2))
            lst = (d.get("data") or {}).get("fastNewsList") or []
            for x in lst:
                if domestic_quota <= 0:
                    break
                txt = (x.get("title") or x.get("summary") or "").strip()
                if not txt or txt[:40] in seen:
                    continue
                seen.add(txt[:40])
                _show = (x.get("showTime") or "").strip()
                _tsdt = None
                try:
                    _tsdt = _dt.datetime.strptime(_show[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_bj_tz)
                except Exception:                            # noqa: BLE001
                    try:
                        _tsdt = _dt.datetime.strptime(_show[-11:] if len(_show) >= 11 else _show, "%m-%d %H:%M").replace(
                            tzinfo=_bj_tz).replace(year=_dt.datetime.now(_bj_tz).year)
                    except Exception:                        # noqa: BLE001
                        _tsdt = None
                # ⚠️ V1.9.13b：显示文本必须与外媒 RSS 同格式「MM-DD HH:MM」！
                #   原 `_show[-11:]` 对 "2026-10-08 11:04:00" 得到 "08 11:04:00"（以日打头、11 位），
                #   字符串倒序排序时 '0' < '1' → 国内源全部沉到最旧、被 eulerpool_src 的 news[:80]
                #   整批截断（实测东财 12 条全丢），页面因此看不到国内信源；且 build_live 的
                #   今天/昨天 正则也解析不了该格式。改由 _tsdt 统一格式化，与外媒严格同构。
                _tt = _tsdt.strftime("%m-%d %H:%M") if _tsdt else (
                    _show[-11:] if len(_show) >= 11 else _show)
                it = {"title": txt, "text": txt[:90],
                      "link": "https://finance.eastmoney.com/a/%s.html" % (x.get("code") or ""),
                      "time": _tt, "src": src, "chan": chan, "foreign": False}
                if _tsdt is not None:
                    it["_ts"] = _tsdt.isoformat()
                    it["_tsdt"] = _tsdt
                out.append(it)
                domestic_quota -= 1
        except Exception as e:                                    # noqa: BLE001
            warn.append("%s(%s)" % (src, type(e).__name__))
    # ---- 排序（新→旧）+ 只留最近 24 小时 ----
    out = _sort_and_filter_recent(out, hours=24)
    out = out[:limit]
    # ⚠️ n_foreign 必须在过滤「之后」重算，否则日志会打印出「国内 -4」这种负数
    n_foreign = sum(1 for x in out if x.get("foreign"))
    return out, ("; ".join(warn) if warn else None), n_foreign


def _sort_and_filter_recent(items, hours=24):
    """按发布时间倒序 + 剔除超过 hours 的条目。
    ⚠️ RSS 的 pubDate 是 RFC822 带 GMT/时区，必须转北京时间再比较，
       直接字符串比较会错（实测有 feed 时间为空 → 保留但排最后）。"""
    import datetime as _dt
    bj = _dt.timezone(_dt.timedelta(hours=8))
    now = _dt.datetime.now(bj)
    ok, unknown = [], []
    for it in items:
        ts = it.get("_tsdt")
        if ts is None:
            unknown.append(it)
            continue
        age = (now - ts).total_seconds() / 3600.0
        if -2<= age <= hours:                # 容忍小幅时钟漂移
            it["_age_h"] = round(age, 1)
            ok.append(it)
    ok.sort(key=lambda x: x["_tsdt"], reverse=True)
    # 剥掉内部字段（datetime 不可 JSON 序列化，且页面用不到）
    for it in ok + unknown:
        it.pop("_tsdt", None)
        it.pop("_ts", None)
    return ok + unknown                          # 无时间戳的排最后，不丢内容


# ---------------------------------------------------------------- 快讯 history（V1.8.9 新增）
# ⚠️ 为什么要这个：改之前 snap["news"] = news **每轮整份覆盖** —— 页面只剩最近
#   15 分钟的快讯，稍早的内容全被冲掉，**盘前/休市时几乎没素材可讲**，且各源
#   每轮只露 per_feed 条脸。现在改成「本轮新拉 + 历史池合并去重」，池子按
#   keep_hours 滚动保留 → **页面永远有一整天的外媒素材**，这才是"全天候覆盖"。
#   注意：这解决的是"内容留存"，不是"拉取频率"；RSS 更新是分钟级甚至小时级，
#   拉太密纯属浪费请求（见 README 纪律：拉取节奏与轮询节奏要解耦）。
NEWS_HISTORY = os.path.join(HERE, ".cache", "news_history.json")
NEWS_KEEP_HOURS = 24  # 用户要求：仅保留 24h 内，超期清出（跨时区源已在 _sort_and_filter_recent 转北京时间，勿再放宽）


def _news_key(it):
    """去重键：优先 link（同一篇报道在多源转载时 link 不同），退回标题前 40 字。"""
    return (it.get("link") or it.get("title") or "")[:120].strip()


def _age_hours_from_time(timestr, now):
    """'MM-DD HH:MM'（北京时间）→ 相对 now 的小时数；无法解析返回 None。
    处理跨年/跨月边界：若候选比 now 还晚超 36h，回退一年
    （避免 12-31 在 01-05 时被误判为「未来新条目」而误留）。"""
    import re as _re
    if not timestr:
        return None
    m = _re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})", timestr.strip())
    if not m:
        return None
    mo, da, hh, mm = (int(x) for x in m.groups())
    try:
        cand = dt.datetime(now.year, mo, da, hh, mm, tzinfo=now.tzinfo)
    except Exception:                                       # noqa: BLE001
        return None
    if (now - cand).total_seconds() < -36 * 3600:            # 未来超 36h → 上一周期
        try:
            cand = dt.datetime(now.year - 1, mo, da, hh, mm, tzinfo=now.tzinfo)
        except Exception:                                   # noqa: BLE001
            return None
    return (now - cand).total_seconds() / 3600.0


def load_news_history():
    try:
        with open(NEWS_HISTORY, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:                                            # noqa: BLE001
        return []


def merge_news_history(fresh, keep_hours=NEWS_KEEP_HOURS, cap=120):
    """把本轮新拉到的快讯并进历史池，按 _key 去重 + 按发布时间倒排 + 过期剔除。

    ⚠️ 不去重会导致同一条新闻每轮被重复拉一次、页面滚动时反复出现同一句 ——
     RSS 是「最新 N 条」语义，轮询必然反复命中同一批头几条。
    ⚠️ 历史池只存**原样条目**（不含 datetime），落盘直接 json 可序列化。
    """
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    pool = {}
    for it in load_news_history():
        k = _news_key(it)
        if k:
            pool[k] = it
    added = 0
    for it in fresh:
        k = _news_key(it)
        if not k:
            continue
        if k not in pool:
            added += 1
        pool[k] = it# 同 key 用新的覆盖旧的（RSS 会更新标题/时间）
    # 过期剔除：以 time 字符串「现算」年龄 —— 不信任历史 _age_h。
    # ⚠️ 历史池条目存入时带的是「首次拉取时算的 age≈0」，之后永不重算，
    #    若只信 _age_h 老条目永远 age≈0、永不淘汰（实测 10-03~10-05 沉底不消失）。
    #    用户要求：仅保留 24h 内，超期一律清出。
    out = []
    for it in pool.values():
        # ⚠️ V1.9.14：历史池里可能残留 V1.9.14 之前写入的**坏格式**时间串
        #   （东财 showTime 误取 `[-11:]` → "08 11:08:24"，以日打头、缺月份）。
        #   坏格式双重危害：① 字符串排序沉底 → 被 eulerpool_src 的 news[:80] 截掉；
        #   ② `_age_hours_from_time` 解析不了 → age=None → **永不过期**，永久滞留在池里。
        #   这里按「24h 窗口内月份只可能是当月或上月」重建月份，使其能正常排序/淘汰。
        _t = str(it.get("time") or "").strip()
        if _t and not re.match(r"^\d{1,2}-\d{1,2}\s+\d{1,2}:\d{1,2}", _t):
            _m = re.match(r"^(\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::\d{1,2})?$", _t)
            if _m:
                _da, _hh, _mi = (int(x) for x in _m.groups())
                _mo = now.month
                if _da - now.day > 5:                    # 日号明显"未来" → 属上月
                    _mo = now.month - 1 or 12
                it["time"] = "%02d-%02d %02d:%02d" % (_mo, _da, _hh, _mi)
        age = _age_hours_from_time(it.get("time"), now)
        if age is not None and age > keep_hours:
            continue
        if age is not None:
            it["_age_h"] = round(age, 1)
        out.append(it)
    out.sort(key=lambda x: (x.get("time") or "", x.get("title") or ""), reverse=True)
    out = out[:cap]
    os.makedirs(os.path.dirname(NEWS_HISTORY), exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=os.path.dirname(NEWS_HISTORY), suffix=".tmp")
    with os.fdopen(_fd, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(_tmp, NEWS_HISTORY)
    return out, added


# ---------------------------------------------------------------- 时段
def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- 时段
# 交易时段定义（北京时间）。来源与依据见 README §6：
#   A股 09:30-11:30 / 13:00-15:00（沪深交易所交易时间）
#   港股 09:30-12:00 / 13:00-16:00（港交所，含半日市差异，此处按常规日）
#   美股 常规 21:30-次日 04:00（夏令时 ET 9:30-16:00；冬令时整体 +1h）
#        盘前 16:00-21:30 / 盘后 次日 04:00-08:00
#        （Nasdaq 官方定义：Pre-Market 4:00-9:30 ET、Post-Market 16:00-20:00 ET；
#          夏季 ET=UTC-4 → 北京时间 = ET+12h；冬季 ET=UTC-5 → ET+13h）
# segs 里的分钟数是「距 00:00 的分钟」，可跨 1440（美股盘后跨天）。
# ⚠️ segs 的 kind 直接存英文枚举（open/pre/post），页面 JS 透传使用，
#    不再在两端各写一套中文映射——这正是 V1.4.0 时段图 class 全 undefined 的病根。
MARKET_SEGS = {
    "cn": {
        "name": "A股", "label": "A联赛场",
        "segs": [(480, 570, "pre"), (570, 690, "open"), (780, 900, "open")],  # 08:00-09:30 盘前 / 09:30-11:30 / 13:00-15:00
    },
    "hk": {
        "name": "港股", "label": "港联赛场",
        "segs": [(570, 720, "open"), (780, 960, "open")],       # 09:30-12:00 / 13:00-16:00
    },
    "us": {
        "name": "美股", "label": "美联赛场",
        "segs": [(960, 1290, "pre"),     # 16:00-21:30 盘前
                (1290, 1440, "open"),   # 21:30-24:00 常规交易
                (0, 240, "open"),       # 00:00-04:00 常规交易（跨天）
                (240, 480, "post")],    # 04:00-08:00 盘后
    },
}
# 冬令时（11月初-3 月初）美股整体 +1 小时。us_dst() 由月份判定。
SEASON_NOTE = "美股时段为夏令时（3-11月）；冬令时整体顺延 1 小时"


# ---------------------------------------------------------------- 空档命名
# 「三市场全 closed」不等于「一整天结束」。逐分钟扫全天（V1.9.27 实测，见
# docs/时段状态总表.md）后只剩两个真空档，一律写「全场休息」会让师傅误判：
#   12:00-13:00  A股 11:30 收、港股 12:00 收，13:00 双双续盘 → 是一段**午休**
#   16:00-17:00  仅冬令时出现：港股已收（16:00），美股盘前 17:00 才开 → 场次**交接**
# 本表**只用于给空档起名**，不参与任何状态判定：
#   active 仍为 None、不发任何节目，narrate 的选场逻辑（只看 active）也完全不受影响。
# 夏令时 16:00-17:00 落在美股盘前段内、根本不会 idle，故此条天然 no-op。
IDLE_WINDOWS = [
    (720, 780, "午间休市", "13:00 续盘"),          # 12:00-13:00
    (960, 1020, "场次交接", "美股盘前 17:00"),      # 16:00-17:00（仅冬令时 idle）
]


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _us_winter(month):
    """美东夏令时：3 月中旬-11 月初。粗判：11-2 月视为冬令时（顺延 1h）。"""
    return month >= 11 or month <= 2


def us_shift(month):
    """美股时段相对夏令时的偏移（分钟）。冬令时 +60。"""
    return 60 if _us_winter(month) else 0


def _state_of(key, mins, month):
    """判定单市场在某一分钟的状态。返回 (状态码, 状态文案)。
    状态码：open 常规交易 / pre 盘前 / post 盘后 / closed 休市
    segs 的 kind 已是英文枚举，这里只做状态码→文案映射（交易中/盘前/盘后）。"""
    cfg = MARKET_SEGS[key]
    sh = us_shift(month) if key == "us" else 0
    LABEL = {"open": "交易中", "pre": "盘前", "post": "盘后"}
    for a, b, kind in cfg["segs"]:
        aa, bb = a + sh, b + sh
        # 跨天归一化：段端点会被 dst 平移推出 1440（冬令时美股常规段 1290-1440 → 1350-1500），
        # 所以当前分钟要同时与 m / m+1440 / m-1440 比较。旧写法只做「向后 +1440」且带
        # `m2 < aa-1440` 门槛，冬令时 00:00-00:59（m=0，阈值 -90 不触发）会漏判，
        # 把明明在美股常规交易中的时段显示成「全场休息」。
        # V1.9.27 修：改为三向比较，冬令时 22:30-05:00 连续覆盖，不再有 00:00-01:00 空洞。
        for m2 in (mins, mins + 1440, mins - 1440):
            if aa <= m2 < bb:
                return kind, LABEL.get(kind, "休市")
    return "closed", "休市"


def market_state(now=None, asof=None):
    """判定三市场状态。返回供页面画 24h 条形图 + 文案表达的数据。

    ⚠️ V1.8.0 数据驱动（本项目最关键的一处口径修正）：
      时段表只回答「这个钟点理论上该不该开」，**不回答「今天到底开没开」**。
      节假日/周末/台风休市时，行情源会一直停在「上一交易日的报价时刻」——
      这个陈旧度就是铁证，比自建节假日表更硬（无需年度维护，并能覆盖台风停市这类
      日历外的休市）。实测：2026-10-06 A股报价停在 09-30 16:19 → 国庆休市；
      港股报价 10-06 11:21:42 → 正常交易。旧口径（纯时钟）会误判 A股「交易中」。

    参数 asof = {mkt: 行情源最后报价时间(datetime)}。
      只对「常规交易(open)」段做陈旧覆盖：盘前/盘后是否有 tick 取决于数据源是否发布
      盘前行情，用新鲜度判会误杀，故 pre/post 仍交给时钟。
      取不到报价（网络挂）→ 该市场不在 asof 里 → 退回纯时钟判定，绝不因抓取失败误报休市。
    """
    now = now or dt.datetime.now()
    mins = now.hour * 60 + now.minute
    shift = us_shift(now.month)
    asof = asof or {}
    is_weekend = now.weekday() >= 5

    out = {}
    for k, cfg in MARKET_SEGS.items():
        code, label = _state_of(k, mins, now.month)
        a = asof.get(k)
        traded_today = bool(a and a.date() == now.date())
        holiday = False
        # 陈旧阈值：盘中报价秒级刷新，45min(港A) / 150min(美股) 足以覆盖源抖动，
        # 又远小于「差一个节假日就是一天以上」的缺口。
        thr = 150 if k == "us" else 45
        near_open = any(abs(mins - aa) <= 5 for aa, _bb, _t in cfg["segs"])
        stale = False
        if a is not None:
            gap = (now - a).total_seconds() / 60.0
            # 负 gap = 报价时间在未来（源时钟超前/时区标注差异），不算陈旧。
            stale = (gap > thr) or (gap < -thr)
        # ⚠️ V1.8.1：holiday 必须独立于「当前钟点是否在交易段」判定。
        #   早先只在 code=="open" 段做覆盖 → 一旦进入午休/收盘（clock 判 closed），
        #   holiday 就回落 False，页面时段图的「今日休市」压暗与标注在 11:30 后凭空消失。
        #   正确语义：「今天这个市场按日历该开，但报价停在上一交易日」= 全天休市，
        #   与现在恰好在不在交易段无关。周末/长假恒为 True（报价永远不是今天）。
        # ⚠️ V1.9.28：周末强制覆盖（A股/港股）。时钟段 09:30-15:00 在周六/周日仍会命中 →
        #   若不单独压制，周末 10:00 会被 _state_of 判成「A股 交易中」，与「周末全休」矛盾。
        #   只压 cn/hk，不压 us——美股常规段 21:30-次日04:00 跨到北京周六 00:00-04:00 本就是
        #   真实盘中（美东周五常规段），须保留其时钟判定（elif 的 not is_weekend 守卫即为此）。
        #   此覆盖独立于 asof（a 为 None 也要压），因周末恒休与「报价是否新鲜」无关。
        if is_weekend and k in ("cn", "hk"):
            code, label, holiday = "closed", "休市", True
        # ⚠️ V1.9.26：工作日节假日/休市覆盖**只对「常规交易(open)段」生效**。盘前(pre)/盘后(post)
        #   本就是无连续 tick 的时段，状态由时钟给定、不应被陈旧 asof 误判成「休市」。旧口径
        #   (stale or code=="open") 会把盘前段的陈旧报价压成 holiday，直接导致 08:00-09:30 的
        #   A股盘前被显示成「全场休息」。周末已在上方单独覆盖，此处不再处理 is_weekend。
        elif a is not None and not traded_today and not is_weekend and code == "open":
            code, label, holiday = "closed", "休市", True
        # ⚠️ V1.9.21：删除「交易中但报价陈旧 → 强行改 closed」的旧逻辑。
        #   原意是源挂了/临时停牌时把市场压成闭市；但它会把「循环被卡住、快照 asof
        #   停在上一交易时段」这种**上游故障**误判成「市场没开」，导致导播台在港股明明
        #   开盘时显示「全场休息」。盘中开闭本就是时钟问题，应由 _state_of 的钟点判定
        #   说了算；日级休市（今天该开却没交易）已由上方 traded_today 分支覆盖，此处不再压。
        out[k] = {
            "name": cfg["name"], "label": cfg["label"], "state": code, "status": label,
            "open": code == "open",                  # 是否常规交易（决定谁在播）
            "holiday": holiday,                      # 今天该开却没开（节假日/停市），全天有效
            "traded_today": traded_today,            # 今日是否真有行情（决定要不要采 A股全市场）
            "asof": a.strftime("%Y-%m-%d %H:%M") if a else None,
            "segs": [[aa + (shift if k == "us" else 0), bb + (shift if k == "us" else 0), t]
                     for aa, bb, t in cfg["segs"]],
        }
    order = ["cn", "hk", "us"]
    # 播出优先级：常规交易 > 盘前 > 盘后 > 全休
    def rank(k):
        return {"open": 0, "pre": 1, "post": 2, "closed": 3}[out[k]["state"]]
    active = sorted(order, key=rank)[0]
    if out[active]["state"] == "closed":
        active = None
    nxt = next((k for k in order if not out[k]["open"]), None)
    # ⚠️ V1.9.14：AH 并行时段（A股 9:30–15:00 与港股 9:30–16:00 高度重叠）徽标要同时体现活着
    #   的市场。旧口径只给单一 active（cn 优先）→ 页面「状态」永远只写 A股，师傅指出 H股漏了。
    #   live_names = 所有 state∈{open,pre} 的市场名（按 cn→hk→us 序），供导播台徽标并列展示；
    #   active/active_name/active_status 语义保持不变（仍是单一主角，供解说/日志用）。
    live_names = [MARKET_SEGS[k]["name"] for k in order if out[k]["state"] in ("open", "pre")]
    # V1.9.27 空档命名：active 为空时区分「午间休市 / 场次交接 / 真·全场休息」。
    # 纯展示层——active 保持 None，节目单与解说选场逻辑不变。
    idle_name, idle_note = "全场休息", ""
    if active is None:
        for a_, b_, nm, nt in IDLE_WINDOWS:
            if a_ <= mins < b_:
                idle_name, idle_note = nm, nt
                break
        # V1.9.28：周末若不是「午间休市/场次交接」空档 → 标「周末休市」而非「全场休息」，
        # 语义更准确（师傅原话「全天 24 小时怎么划定状态」，周末是全天的一部分）。
        if idle_name == "全场休息" and is_weekend:
            idle_name = "周末休市"
    return {
        "active": active,
        "idle_name": idle_name,
        "idle_note": idle_note,
        "active_name": MARKET_SEGS[active]["name"] if active else "全场休息",
        "active_label": MARKET_SEGS[active]["label"] if active else "",
        "active_status": out[active]["status"] if active else "休市",
        "active_names": live_names,
        "next": nxt, "next_name": MARKET_SEGS[nxt]["name"] if nxt else "-",
        "all": out,
        "us_shift_min": shift,
        "season_note": SEASON_NOTE,
        "weekday": "周" + "一二三四五六日"[now.weekday()],
        "beijing_now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "checked_at": now.strftime("%Y-%m-%d %H:%M"),
    }


def live_session(snapshot_path=None):
    """V1.9.16：**按当前钟点实时**判定三市场状态（供观看服务秒级刷新导播台）。

    为什么必须单独有它：market_state() 每轮只在采集时算一次，结果烙进 live.html
    与 live_data.json，而一轮 = 300s 档位 + 采集/解说约 2min → 开盘/午休/收盘这类
    **分钟级边界**上页面最长滞后 7 分钟。实测 2026-10-08 13:02（下午已开盘）页面
    仍写「全场休息」——那正是 12:53 午休窗口（A股 11:30-13:00、港股 12:00-13:00
    双双闭市）建出来的页。「市场现在开不开」是时钟问题，不该等数据轮次。

    口径拆分（关键，别把日级事实也按时钟重算）：
      * 钟点部分 → 用**当前时间**重算（open/pre/post/closed 随分钟变化）；
      * 日级部分 → holiday / traded_today 沿用上一轮快照里各市场**行情源最后报价
        时刻**。那是「今天到底开没开」的日级证据（节假日/周末/台风停市靠它识别，
        见 market_state 文档），一轮之内不会变，重算反而会失去判定基石。
    快照缺失/读失败 → asof={} → 退回纯时钟判定（与 market_state 既有兜底一致，
    绝不因抓取失败误报休市）。
    """
    p = snapshot_path or os.path.join(HERE, "data", "snapshot.json")
    asof = {}
    try:
        with open(p, encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:                                            # noqa: BLE001
        snap = {}
    prev = ((snap.get("session") or {}).get("all") or {})
    for k, cell in prev.items():
        s = (cell or {}).get("asof")
        if not s:
            continue
        try:
            # 快照里的 asof 已是 "%Y-%m-%d %H:%M"（分钟精度足够：陈旧阈值 45/150min）
            asof[k] = dt.datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M")
        except Exception:                                        # noqa: BLE001
            pass
    return market_state(asof=asof)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.path.join(HERE, "data", "snapshot.json"))
    ap.add_argument("--news", type=int, default=24)
    a = ap.parse_args()

    with open(a.snapshot, encoding="utf-8") as f:
        snap = json.load(f)

    idx, asof, w1 = collect_indexes()
    for k in ("cn", "hk", "us"):
        a_ = asof.get(k)
        _log("[%s] %s指数 %d 条%s" % ("ok" if idx[k] else "--", k, len(idx[k]),
                                      ("，行情源最后报价 " + a_.strftime("%m-%d %H:%M")) if a_ else "（无报价时间）"))

    glob, w2 = collect_global()
    _log("[%s] 全球走马灯 %d 条" % ("ok" if glob else "--", len(glob)))

    hk_stocks, w1b = collect_hk_stocks()
    _log("[%s] 港股个股 %d 条%s" % ("ok" if hk_stocks else "--", len(hk_stocks),
                                    ("，领涨 " + hk_stocks[0]["name"]) if hk_stocks else ""))

    # ---- V1.9.22：日内分时（K 线）+ VIX ----
    # 都在 markets.py 之内完成，不新增流水线步骤（少一个步骤 = 少一个卡死点）。
    intra, w1c = collect_intraday()
    _log("[%s] 日内分时 %d 个市场" % ("ok" if intra else "--", len(intra)))
    vix = collect_vix()
    # ---- V1.9.25：DeanFi S&P500 免费广度（补美股卡「待接入」缺口）----
    deanfi_b, w4 = collect_deanfi_breadth()
    if deanfi_b:
        snap["us_breadth_deanfi"] = deanfi_b
        _log("[ok] DeanFi S&P500 广度 涨%d/跌%d (A-D %.2f · >200MA %s%%)" % (
            deanfi_b["advances"], deanfi_b["declines"],
            deanfi_b.get("ad_ratio") or 0, deanfi_b.get("above_200ma_pct")))
    elif snap.get("us_breadth_deanfi"):
        _log("[!] DeanFi 拉取失败，沿用上轮缓存")
    else:
        _log("[--] DeanFi 广度暂不可用（保持待接入）")

    news, w3, n_foreign = collect_news(a.news)
    _log("[%s] 快讯本轮新拉 %d 条（外媒 %d / 国内 %d）" % (
        "ok" if news else "--", len(news), n_foreign, len(news) - n_foreign))
    if news:
        from collections import Counter
        srcs = Counter(x["src"] for x in news)
        _log("本轮来源分布：%s" % "、".join("%s×%d" % (k, v) for k, v in srcs.most_common()))
    # ---- V1.8.9：并进历史池（跨轮累积，解决"页面只剩最近 15 分钟"）----
    news_all, added = merge_news_history(news)
    _log("[ok] 快讯历史池 %d 条（本轮新增 %d）· 保留 %dh" % (len(news_all), added, NEWS_KEEP_HOURS))
    if news_all:
        from collections import Counter
        allsrc = Counter(x["src"] for x in news_all)
        _log("历史池来源分布：%s" % "、".join("%s×%d" % (k, v) for k, v in allsrc.most_common()))
    news = news_all

    st = market_state(asof=asof)
    _log("[ok] 当前市场 %s（下一场：%s）· 检查于 %s" % (st["active_name"], st["next_name"], st["checked_at"]))
    # 节假日/休市显式点名，避免"看起来在交易其实没开"的静默错误
    off = [st["all"][k]["name"] for k in ("cn", "hk", "us") if st["all"][k].get("holiday")]
    if off:
        _log("[!] 以下市场按时段表本应在交易，但行情源报价停在上一个交易日 → 判为休市：%s" % "、".join(off))

    snap["markets"] = idx
    snap["hk_stocks"] = hk_stocks
    snap["global_ticker"] = glob
    # V1.9.22：本场统计卡的 K 线 + 美股情绪（VIX）源数据。
    # 每轮 markets.py 都重算，故 collect_live.py 的「继承上一轮字段」不会让它们变陈旧。
    if intra:
        snap["intraday"] = intra
    if vix:
        snap["vix"] = vix
    snap["news"] = news
    snap["session"] = st
    snap["session"]["src"] = "sina:hq" if (idx["cn"] and idx["hk"]) else "sina"
    warn = [w for w in (w1 if isinstance(w1, list) else [w1],
                        w1b, w1c, w2, w3, w4) if w]
    if warn:
        snap.setdefault("warnings", []).extend(warn)
    # 原子写：先写临时文件再 os.replace，中途崩溃不会毁掉原文件
    import os as _os, tempfile as _tf
    _fd, _tmp = _tf.mkstemp(dir=_os.path.dirname(a.snapshot) or '.', suffix='.tmp')
    with _os.fdopen(_fd, 'w', encoding='utf-8') as _f:
        json.dump(snap, _f, ensure_ascii=False, indent=1)
    _os.replace(_tmp, a.snapshot)
    _log("=== 已写回 %s ===" % a.snapshot)


if __name__ == "__main__":
    main()
