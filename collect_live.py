#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 一期 采集层
=====================================================================
职责：抓一份「赛况快照」——指数 / 涨跌家数 / 板块 / 异动榜 / 涨跌停池。
输出：data/snapshot.json（纯数据，零 LLM 依赖）
纪律：
  * 多源兜底 东财 → 腾讯 → 新浪（交接文档 §3.1；东财 5 分钟内被限流过是常态）
  * 大页 clist 会被对端断连 → 分页 + 指数退避重试
  * 写日志的 print 一律 flush=True（§6.2 块缓冲）
  * 采集层永不写解说词，解说层永不碰网络（单向依赖）
运行：python3 collect_live.py [--date 20260930] [--out data/snapshot.json]
"""

import argparse
import gzip
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cn_tz  # noqa: E402,F401  —— 锚定进程时区为北京时间（V1.9.40 时区铁律）

sys.stdout.reconfigure(line_buffering=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

EM_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 东财限流是**按 host 维度**的，不是按 IP—— 主站 push2 被拒时镜像仍通。
# 2026-10-05 实测：main/1~28/38~47/50~54/... 共 86 个 host 被拒，14 个通。
# 故采集层用镜像池轮转，而非死磕主站（这是预研 §3.1 未发现的关键事实）。
EM_MIRRORS = [
    "29.push2.eastmoney.com", "30.push2.eastmoney.com", "31.push2.eastmoney.com",
    "32.push2.eastmoney.com", "33.push2.eastmoney.com", "34.push2.eastmoney.com",
    "35.push2.eastmoney.com", "36.push2.eastmoney.com", "37.push2.eastmoney.com",
    "48.push2.eastmoney.com", "55.push2.eastmoney.com", "61.push2.eastmoney.com",
    "69.push2.eastmoney.com", "91.push2.eastmoney.com",
    "push2.eastmoney.com",                       # 主站放最后，兜底
]
_EM_POOL = list(EM_MIRRORS)                      # 进程内轮转游标
_EM_LOCK_FREE = True

# 指数：secid(东财) / 腾讯码 / 新浪码
INDEXES = [
    {"key": "sh", "name": "上证指数", "em": "1.000001", "tx": "sh000001", "sina": "sh000001"},
    {"key": "sz", "name": "深证成指", "em": "0.399001", "tx": "sz399001", "sina": "sz399001"},
    {"key": "cyb", "name": "创业板指", "em": "0.399006", "tx": "sz399006", "sina": "sz399006"},
    {"key": "kc50", "name": "科创50", "em": "1.000688", "tx": "sh000688", "sina": "sh000688"},
]

# 全市场（A 股主板 + 创业板 + 科创板）
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# ====================================================================
# 三大市场 · 2026-10-05 实测可用源
# --------------------------------------------------------------------
# A股：东财 push2 clist（异动榜/涨跌家数最全）→ 新浪 vip 排行（兜底）
# 港股：新浪 rt_hk*（延时约 15min）／腾讯 hk*（延时，同源）→ 无全市场排行源
# 美股：新浪 gb_*（含时间戳）／腾讯 us*（同上）
# 全球走马灯：新浪 int_* 系列（一次性拿全部，零额外请求）
# 快讯：东财 7x24 → 新浪 7x24 兜底
# ====================================================================
MARKETS = {
    "cn": {
        "name": "A股", "tz_offset_h": 0,
        "sessions": [("09:30", "11:30"), ("13:00", "15:00")],
        "indexes": INDEXES,
    },
    "hk": {
        "name": "港股", "tz_offset_h": 0,
        "sessions": [("09:30", "12:00"), ("13:00", "16:00")],
        "indexes": [
            {"key": "hsi", "name": "恒生指数", "sina": "rt_hkHSI", "tx": "hkHSI"},
            {"key": "hstech", "name": "恒生科技", "sina": "rt_hkHSTECH", "tx": "hkHSTECH"},
            {"key": "hscei", "name": "国企指数", "sina": "rt_hkHSCEI", "tx": "hkHSCEI"},
        ],
    },
    "us": {
        "name": "美股", "tz_offset_h": -13,          # 北京时间 = 当地时间 + 13h（夏令时）
        "sessions": [("21:30", "23:59"), ("00:00", "04:00")],
        "indexes": [
            {"key": "dji", "name": "道琼斯", "sina": "gb_dji", "tx": "usDJI"},
            {"key": "ixic", "name": "纳斯达克", "sina": "gb_ixic", "tx": "usIXIC"},
            {"key": "inx", "name": "标普500", "sina": "gb_inx", "tx": "usINX"},
        ],
    },
}

# 全球股指走马灯（新浪 int_ 系列，一次请求全拿）
GLOBAL_TICKER = [
    ("int_dji", "道琼斯"),("int_nasdaq", "纳斯达克"),("int_sp500", "标普500"),
    ("int_nikkei", "日经225"),("int_hangseng", "恒生指数"),("int_ftse", "英国富时100"),
    ("int_bovespa", "巴西Bovespa"),("int_dji", "道琼斯"),
    ("s_sh000001", "上证指数"),("s_sz399001", "深证成指"),
    ("s_sz399006", "创业板指"),("rt_hkHSI", "恒生指数"),
]


def _log(msg):
    print(msg, flush=True)


def http(url, headers=None, enc="utf-8", timeout=12, retries=3, backoff=1.6):
    """GET with 指数退避重试；对端断连/限流都走这里。"""
    last = None
    for i in range(retries):
        try:
            h = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode(enc, "ignore")
        except Exception as e:                                    # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(backoff ** i)
    raise last


def em_get(path, retries=None):
    """走东财镜像池取一个接口。逐个 host 试，直到某个通。
    限流是 host 维度 → 换 host 就能过；这是 2026-10-05 实测结论。"""
    n = len(EM_MIRRORS) if retries is None else min(retries, len(EM_MIRRORS))
    global _EM_POOL
    last = None
    for _ in range(n):
        host = _EM_POOL.pop(0)
        _EM_POOL.append(host)                      # 轮转，避免总打同一个
        try:
            return em_raw("https://%s%s" % (host, path))
        except Exception as e:                    # noqa: BLE001
            last = e
            time.sleep(0.25)
    raise last if last else RuntimeError("no mirror tried")


def em_raw(url, retries=2):
    return http(url, retries=retries, backoff=1.2)


# ---------------------------------------------------------------- 东财
def em_index():
    ids = ",".join(x["em"] for x in INDEXES)
    d = json.loads(em_get("/api/qt/ulist.np/get?secids=" + ids +
                          "&fields=f2,f3,f4,f5,f6,f12,f14&fltt=2"))["data"]["diff"]
    out = {}
    for x in d:
        out[str(x["f12"])] = {
            "price": x.get("f2"), "pct": x.get("f3"), "chg": x.get("f4"),
            "vol": x.get("f5"), "amount": x.get("f6"), "name": x.get("f14"),
        }
    return out


def em_clist(fid, pz=12, fs=FS_ALL, asc=False, fields="f2,f3,f5,f6,f8,f12,f14,f20,f21"):
    """全市场排序榜（一次小页请求，page=1 足够做异动榜）。"""
    d = json.loads(em_get("/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1&fltt=2&invt=2"
                          "&fid=%s&fs=%s&fields=%s" % (pz, 0 if asc else 1, fid, fs, fields)))["data"]
    rows = []
    for x in d.get("diff") or []:
        rows.append({
            "code": x.get("f12"), "name": x.get("f14"), "price": x.get("f2"),
            "pct": x.get("f3"), "vol": x.get("f5"), "amount": x.get("f6"),
            "turn": x.get("f8"), "mktcap": x.get("f20"), "floatcap": x.get("f21"),
        })
    return rows, d.get("total")


def em_sectors(pz=30):
    """行业板块涨幅榜（fs=m:90+t:2）+ 跌幅榜（po=0）。

    ⚠️ V1.9.36：pz 由 10 提到 30。原因：narrate 的「快讯 ↔ 板块」关联（link_news_sectors）
    需要一份**更宽的今日板块池**才有命中率 —— 实测 top10/bottom10 全是申万细分名
    （视频媒体/文字媒体/印制电路板…），而快讯说的是"房地产板块""有色板块"这类行业大类，
    窄池必然挂不上。**请求次数不变**（仍各 1 次，只是每页多 20 行），页面与 prompt 侧
    仍各自切片取 5 条（narrate sec_top/sec_bottom 的 [:5]），故对展示/费用零影响。"""
    out = {}
    for key, po in (("top", 1), ("bottom", 0)):
        d = json.loads(em_get("/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1&fltt=2&invt=2"
                              "&fid=f3&fs=m:90+t:2&fields=f2,f3,f12,f14,f104,f105" % (pz, po)))["data"]
        out[key] = [{"code": x.get("f12"), "name": x.get("f14"),
                     "pct": x.get("f3"), "price": x.get("f2")} for x in d.get("diff") or []]
    return out


def em_pool(kind, date):
    """涨停/跌停池。kind: 'ZT' | 'DT'。走push2ex 域（与 push2 限流独立）。"""
    sort = "fbt%3Aasc" if kind == "ZT" else "fund%3Aasc"
    url = ("https://push2ex.eastmoney.com/getTopic%sPool?ut=%s&dpt=wz.ztzt"
           "&Pageindex=0&pagesize=60&sort=%s&date=%s" % (kind, EM_UT, sort, date))
    d = json.loads(http(url)).get("data") or {}
    pool = []
    for x in d.get("pool") or []:
        pool.append({
            "code": x.get("c"), "name": x.get("n"), "pct": x.get("zdp"),
            "sector": x.get("hybk"), "boards": x.get("lbc"),
            "first_seal": x.get("fbt"), "seal_fund": x.get("fund"),
        })
    return d.get("tc"), pool


def em_breadth(page=800, pages=8):
    """全市场涨跌家数。⚠️ 必须 fid=f12（按代码排）—— 用 fid=f3（按涨幅排）
    逐页翻只会拿到榜单头部，算出「800涨0跌」的荒谬分布。同新浪的坑。"""
    up = dn = fl = zt = dt = 0
    n = 0
    dist = {}
    for p in range(1, pages + 1):
        rows = json.loads(em_get("/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2"
                                 "&fid=f12&fs=%s&fields=f3,f12" % (p, page, FS_ALL),
                                 retries=5))["data"].get("diff") or []
        if not rows:
            break
        n += len(rows)
        for x in rows:
            v = x.get("f3")
            if not isinstance(v, (int, float)):
                fl += 1
                continue
            if v > 0:
                up += 1
            elif v < 0:
                dn += 1
            else:
                fl += 1
            if v >= 9.8:
                zt += 1
            elif v <= -9.8:
                dt += 1
            b = "涨停" if v >= 9.8 else "跌停" if v <= -9.8 else \
                ">+5%" if v >= 5 else "+2~5%" if v >= 2 else "0~2%" if v > 0 else \
                "-2~0%" if v > -5 else ">-5%" if v > -9.8 else "<-9.8%"
            dist[b] = dist.get(b, 0) + 1
        time.sleep(0.3)                                     # 主动限速，别把源打疼
    return {"n": n, "up": up, "down": dn, "flat": fl, "limit_up": zt, "limit_down": dt, "dist": dist}


# ---------------------------------------------------------------- 腾讯 / 新浪兜底
TX_MAP = {x["key"]: x["tx"] for x in INDEXES}
SINA_MAP = {x["key"]: x["sina"] for x in INDEXES}


def tx_index():
    s = http("https://qt.gtimg.cn/q=" + ",".join(TX_MAP.values()), enc="gbk")
    out = {}
    for line in s.strip().split("\n"):
        if "=" not in line:
            continue
        code = line.split("=")[0].replace("v_", "")
        p = line.split('"')[1].split("~")
        #腾讯字段序：31=时间 32=涨跌 33=涨跌幅 4=昨收 3=今开 6=成交量(手) 37=成交额(万)
        out[code] = {
            "name": p[1], "price": _f(p[3]), "prev": _f(p[4]), "open": _f(p[5]),
            "pct": _f(p[32]), "chg": _f(p[31]), "vol": _f(p[6]), "amount": _f(p[37]),
            "ts": p[30],
        }
    return out


def sina_index():
    s = http("https://hq.sinajs.cn/list=" + ",".join(SINA_MAP.values()),
             headers={"Referer": "https://finance.sina.com.cn/"}, enc="gbk")
    out = {}
    for line in s.strip().split("\n"):
        if "=" not in line:
            continue
        code = line.split("=")[0].replace("var hq_str_", "")
        p = line.split('"')[1].split(",")
        # 新浪：0名 1今开 2昨收 3现价 4高 5低 8成交量(手) 9成交额(元) 30日期 31时间
        prev = _f(p[2])
        out[code] = {
            "name": p[0], "open": _f(p[1]), "prev": prev, "price": _f(p[3]),
            "high": _f(p[4]), "low": _f(p[5]), "vol": _f(p[8]), "amount": _f(p[9]),
            "pct": (_f(p[3]) / prev - 1) * 100 if prev else None,
            "chg": (_f(p[3]) - prev) if prev else None,
            "ts": p[30] + " " + p[31],
        }
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 新浪全市场排行（真正兜底）
# 2026-10-05 实测：东财被限流时，新浪 vip 排行接口仍通，且字段比东财还全
# （多 turnoverratio 换手 / nmc 流通市值 / ticktime）。故把它当涨跌家数 + 异动榜的备份源。
SINA_RANK = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData?page=%d&num=%d&sort=%s&asc=%d&node=hs_a&symbol=&_s_r_a=page")
SINA_REF = {"Referer": "https://finance.sina.com.cn/"}


def sina_rank_page(page, num, sort="changepercent", asc=0):
    """sort=None 时走「无排序」模式：按代码自然序翻页，可扫全市场。
    ⚠️ 关键教训（2026-10-05 实测）：带 sort 参数的接口逐页翻永远只给榜单头部
    （前 800 条全是上涨），算涨跌家数会得出「800 涨 0 跌」这种荒谬结果。
    算全市场分布**必须**用无排序分页；榜单位置另用带 sort 的单页取。"""
    if sort:
        url = SINA_RANK % (page, num, sort, asc)
    else:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=%d&node=hs_a&symbol=&_s_r_a=page"
               % (page, num))
    raw = http(url, headers=SINA_REF, enc="gbk", retries=2)
    # 新浪返回的是非严格 JSON（key 无引号），需轻量修正
    raw = raw.strip()
    if not raw or raw in ("null", "[]"):
        return []
    # 新浪这个接口历史上有时返回非严格 JSON（key 不带引号），故先直解再兜底正则补引号
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        fixed = re.sub(r"([{,])\s*([A-Za-z_][\w]*)\s*:", r'\1"\2":', raw)
        fixed = fixed.replace("'", '"')
        data = json.loads(fixed)
    out = []
    for x in data:
        out.append({
            "code": x.get("code"), "name": x.get("name"),
            "price": _f(x.get("trade")), "pct": _f(x.get("changepercent")),
            "chg": _f(x.get("pricechange")),
            "vol": (_f(x.get("volume")) or 0) / 100.0 or None,   # 新浪 volume 是股→手
            "amount": _f(x.get("amount")),
            "turn": _f(x.get("turnoverratio")),
            "mktcap": (_f(x.get("mktcap")) or 0) * 1e4 or None,  # 万元 → 元
            "floatcap": (_f(x.get("nmc")) or 0) * 1e4 or None,
        })
    return out


def sina_total():
    u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "Market_Center.getHQNodeStockCount?node=hs_a")
    return int(http(u, headers=SINA_REF, enc="gbk").strip().strip('"'))


def sina_breadth_and_lists(pages=60, per=100):
    """新浪一次性算出：涨跌家数 + 涨跌停家数 + 涨幅/跌幅/成交额榜。
    分布用**无排序分页**扫全市场（带排序翻页只给榜单头部，见 sina_rank_page 注释）。"""
    up = dn = fl = zt = dt = 0
    n = 0
    dist = {}
    seen = set()
    for p in range(1, pages + 1):
        try:
            rows = sina_rank_page(p, per, sort=None)
        except Exception:                                        # noqa: BLE001
            break
        if not rows:
            break
        fresh = 0
        for x in rows:
            code = x.get("code")
            if code in seen:                                     # 去重，防分页重叠
                continue
            seen.add(code)
            fresh += 1
            n += 1
            v = x.get("pct")
            if v is None:
                fl += 1
                continue
            if v > 0:
                up += 1
            elif v < 0:
                dn += 1
            else:
                fl += 1
            if v >= 9.8:
                zt += 1
            elif v <= -9.8:
                dt += 1
            b = ("涨停" if v >= 9.8 else "跌停" if v <= -9.8 else ">+5%" if v >= 5
                 else "+2~5%" if v >= 2 else "0~2%" if v > 0 else "-2~0%" if v > -5
                 else ">-5%" if v > -9.8 else "<-9.8%")
            dist[b] = dist.get(b, 0) + 1
        if fresh == 0:                                           # 该页全是重复 → 已到尾部
            break
        time.sleep(0.2)
    breadth = {"n": n, "up": up, "down": dn, "flat": fl,
               "limit_up": zt, "limit_down": dt, "dist": dist}
    lists = {}
    for key, sort, asc in (("gainers", "changepercent", 0), ("losers", "changepercent", 1),
                           ("active", "amount", 0)):
        try:
            lists[key] = {"rows": sina_rank_page(1, 12, sort, asc), "total": n}
        except Exception:                                        # noqa: BLE001
            lists[key] = None
    return breadth, lists


def sina_sectors():
    """新浪概念板块涨跌（东财 clist 板块挂了时的兜底）。
    ⚠️ 踩坑记录：`newSinaHy.php`（行业板块）的 [5] 字段是**加权平均价不是涨跌幅**，
       拿去当涨跌幅会算出 216% 这种荒谬值。必须用 `newFLJK.php?param=class`（概念板块），
       其字段序：[0]code [1]名 [2]家数 [3]均价 [4]涨跌额 [5]涨跌幅% [6]量 [7]额 [12]领涨股。
    行业分类没有可靠免费源，故板块维度用概念板块（解说里说"概念"更准确）。"""
    raw = http("https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class",
               headers=SINA_REF, enc="gbk", retries=2)
    body = raw[raw.find("{"):raw.rfind("}") + 1]
    data = json.loads(body)
    rows = []
    for v in data.values():
        p = str(v).split(",")
        if len(p) < 8:
            continue
        try:
            rows.append({"code": p[0], "name": p[1], "pct": float(p[5]),
                         "count": int(p[2]), "amount": float(p[7]),
                         "leader": p[12] if len(p) > 12 else None})
        except (ValueError, IndexError):
            continue
    rows.sort(key=lambda x: x["pct"], reverse=True)
    fmt = lambda xs: [{"code": x["code"], "name": x["name"], "pct": round(x["pct"], 2),
                       "price": None, "leader": x["leader"]} for x in xs]
    return {"top": fmt(rows[:10]), "bottom": fmt(rows[-10:][::-1]), "total": len(rows)}


# ---------------------------------------------------------------- 组装
def collect(date, deep=True):
    snap = {
        "date": date,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": [],
        "index": {},
        "index_src": None,
    }
    warn = []

    # 1) 指数：东财 → 腾讯 → 新浪
    em = None
    try:
        em = em_index()
        if not em:
            raise ValueError("empty")
        snap["index"] = em
        snap["index_src"] = "eastmoney"
        snap["sources"].append("eastmoney:index")
        _log("[ok] 指数 · 东财 %d 条" % len(em))
    except Exception as e:                                    # noqa: BLE001
        warn.append("东财指数失败(%s)" % type(e).__name__)
        _log("[--] 东财指数失败：%s → 腾讯兜底" % e)
        try:
            raw = tx_index()
            snap["index"] = {x["key"]: raw.get(x["tx"], {}) for x in INDEXES}
            snap["index_src"] = "tencent"
            snap["sources"].append("tencent:index")
            _log("[ok] 指数 · 腾讯兜底")
        except Exception as e2:                               # noqa: BLE001
            _log("[--] 腾讯也失败：%s → 新浪兜底" % e2)
            raw = sina_index()
            snap["index"] = {x["key"]: raw.get(x["sina"], {}) for x in INDEXES}
            snap["index_src"] = "sina"
            snap["sources"].append("sina:index")
            _log("[ok] 指数 · 新浪兜底")

    for name in ("breadth", "gainers", "losers", "active", "sectors", "zt", "dt"):
        snap[name] = None

    if not deep:
        snap["warnings"] = warn + ["deep=false，跳过全市场维度"]
        return snap

    # 2) 涨跌家数 + 分布（东财 → 新浪 双源；东财限流是常态，新浪是真正兜底）
    breadth_src = None
    try:
        snap["breadth"] = em_breadth()
        breadth_src = "eastmoney"
        b = snap["breadth"]
        _log("[ok] 涨跌家数 · 东财 涨%d 跌%d 平%d (样本%d)" % (b["up"], b["down"], b["flat"], b["n"]))
    except Exception as e:                                    # noqa: BLE001
        warn.append("东财涨跌家数失败(%s)" % type(e).__name__)
        _log("[--] 东财涨跌家数失败：%s → 新浪兜底" % e)
        time.sleep(1.5)
        try:
            snap["breadth"], lists = sina_breadth_and_lists()
            breadth_src = "sina"
            snap["sources"].append("sina:rank")
            b = snap["breadth"]
            _log("[ok] 涨跌家数 · 新浪兜底 涨%d 跌%d 平%d (样本%d)" % (b["up"], b["down"], b["flat"], b["n"]))
            # 新浪这一次就把三个榜都带回来了，直接落位，省三次东财请求
            for k2, v2 in lists.items():
                if v2 and not snap.get(k2):
                    snap[k2] = v2
                    _log("[ok] %s榜 · 新浪兜底 %d 条" % (k2, len(v2["rows"])))
        except Exception as e2:                               # noqa: BLE001
            warn.append("新浪涨跌家数也失败(%s)" % type(e2).__name__)
            _log("[--] 新浪也失败：%s（两源皆挂，涨跌家数留空）" % e2)
    snap["breadth_src"] = breadth_src

    # 3) 异动榜（若新浪兜底已填充则跳过，避免重复打源）
    for key, fid, asc, label in (("gainers", "f3", False, "涨幅"),
                                 ("losers", "f3", True, "跌幅"),
                                 ("active", "f6", False, "成交额")):
        if snap.get(key):
            _log("[--] %s榜 · 已有（新浪兜底），跳过东财" % label)
            continue
        try:
            rows, total = em_clist(fid, pz=12, asc=asc)
            snap[key] = {"rows": rows, "total": total}
            snap["sources"].append("eastmoney:clist:%s" % key)
            _log("[ok] %s榜 · 东财 %d 条" % (label, len(rows)))
        except Exception as e:                                # noqa: BLE001
            warn.append("东财%s榜失败(%s)" % (label, type(e).__name__))
            _log("[--] 东财%s榜失败：%s → 留空" % (label, e))
            time.sleep(1.0)

    # 4) 板块（东财 → 新浪 兜底）
    try:
        snap["sectors"] = em_sectors()
        snap["sources"].append("eastmoney:sectors")
        _log("[ok] 板块 · 东财 领涨%s 领跌%s" %
             (snap["sectors"]["top"][0]["name"] if snap["sectors"]["top"] else "-",
              snap["sectors"]["bottom"][0]["name"] if snap["sectors"]["bottom"] else "-"))
    except Exception as e:                                    # noqa: BLE001
        warn.append("东财板块失败(%s)" % type(e).__name__)
        _log("[--] 东财板块失败：%s → 新浪兜底" % e)
        time.sleep(1.0)
        try:
            snap["sectors"] = sina_sectors()
            snap["sources"].append("sina:sectors")
            _log("[ok] 板块 · 新浪兜底 领涨%s 领跌%s" %
                 (snap["sectors"]["top"][0]["name"] if snap["sectors"]["top"] else "-",
                  snap["sectors"]["bottom"][0]["name"] if snap["sectors"]["bottom"] else "-"))
        except Exception as e2:                               # noqa: BLE001
            warn.append("新浪板块也失败(%s)" % type(e2).__name__)
            _log("[--] 新浪板块也失败：%s（板块留空，解说层会跳过该段）" % e2)

    # 5) 涨停/跌停池
    for key, kind in (("zt", "ZT"), ("dt", "DT")):
        try:
            tc, pool = em_pool(kind, date)
            snap[key] = {"tc": tc, "pool": pool}
            _log("[ok] %s池 · %s 只" % ("涨停" if kind == "ZT" else "跌停", tc))
        except Exception as e:                                # noqa: BLE001
            warn.append("%s池失败(%s)" % (kind, type(e).__name__))
            _log("[--] %s 池失败：%s" % (kind, e))
            time.sleep(1.2)

    snap["warnings"] = warn
    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y%m%d"), help="交易日 YYYYMMDD（涨跌停池用）")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "snapshot.json"))
    ap.add_argument("--shallow", action="store_true", help="只抓指数（快速探活）")
    a = ap.parse_args()

    _log("=== 股市解说直播 · 采集开始 date=%s ===" % a.date)
    t0 = time.time()
    snap = collect(a.date, deep=not a.shallow)
    snap["elapsed"] = round(time.time() - t0, 2)

    # ⚠️ V1.9.15：本脚本是「从零构建」的快照（不含 markets/news/session 等由
    #   markets.py 后续补的字段），直接整份覆盖会让**同一时刻的读者**（另一循环的
    #   build_live.py、页面对 /__state__.json 的轮询）读到 news=[]、session={}
    #   → 整个页面变成「指数数据暂不可用 / 资讯暂不可用 / 全场休息」
    #   （2026-10-08 11:3x 实测：双循环并发时页面挂掉，live_data.json 里
    #    index 4 条 / news 0 / session {} —— 正是这个签名）。
    #   修：落盘前把旧快照里**本脚本不产出**的字段原样带过来（markets/news/session/
    #   global_ticker/hk_stocks/econ_cal…）。本脚本产出的字段（index/breadth/gainers/
    #   losers/active/sectors/zt/dt…）仍以新数据为准；markets.py 紧随其后（同轮、秒级）
    #   会把这些字段刷新成本轮真值。
    #   效果：窗口态从「空」变成「上一轮的值」—— 页面最坏是滞后一轮，不会再挂。
    if os.path.exists(a.out):
        try:
            with open(a.out, encoding="utf-8") as _f:
                _old = json.load(_f)
            if isinstance(_old, dict):
                _carry = [k for k in _old if k not in snap]
                for _k in _carry:
                    snap[_k] = _old[_k]
                if _carry:
                    _log("[carry] 继承上一轮字段 %d 个：%s" % (len(_carry), ",".join(_carry)))
        except Exception as _e:                                   # noqa: BLE001
            _log("[--] 旧快照读取失败（跳过字段继承）：%s" % _e)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    # 原子写：先写临时文件再 os.replace，中途崩溃不会毁掉原文件
    import os as _os, tempfile as _tf
    _fd, _tmp = _tf.mkstemp(dir=_os.path.dirname(a.out) or '.', suffix='.tmp')
    with _os.fdopen(_fd, 'w', encoding='utf-8') as _f:
        json.dump(snap, _f, ensure_ascii=False, indent=1)
    _os.replace(_tmp, a.out)
    _log("=== 完成 %.1fs → %s ===" % (snap["elapsed"], a.out))
    if snap.get("warnings"):
        _log("告警 %d 条：%s" % (len(snap["warnings"]), "; ".join(snap["warnings"])))


if __name__ == "__main__":
    main()
