#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · eulerpool 资讯源（V1.9.0 新增）
=====================================================================
职责：拉「美股个股级新闻」+「经济数据官方发布日历」，并进快照的快讯池。
定位：**增量源，不是主源** —— 挂了整页照常出内容（优雅降级）。
产出：
  * snap["news"] 追加 eulerpool 源的条目（src="Eulerpool"）
  * snap["econ_cal"] 经济发布日历（给 prompt 当"今晚议程"，**不含任何数值**）

⚠️⚠️ 三个必须记住的实测边界（2026-10-06逐个 curl 验证，勿重复试错）：
 1. **必须走代理**。直连（curl/httpx/官方 SDK 全部试过）→ HTTP 403 +
    "Sorry, you have been blocked"（**Cloudflare 硬拉黑，不是 JS 挑战**），
    `cf-ray: ...-SEA`。换 UA / HTTP2 / Cookie jar / TLS 指纹全部无效。
    本机 WorkBuddy 默认代理 50416 的出口 IP 被封；**7897 的出口没被封**。
    代理端口在 config.eulerpool.proxy 里可改，端口变了改配置即可。
 2. **`news/feed` 线上 404**（官方 SDK 有这个方法，但路由不存在）。
    能用的是 `/research/news/{TICKER}`，**一次一个ticker**。
 3. **港股取不到**。试过 0700/700/00700:HK/0700.HK/9866/09866/HK00700
    共 7 种写法，全部返回 `[]` → **该端点只覆盖美股**。
 4. **经济日历没有数值**。`calendar/economic-calendar` 与其 `/history`
    版本都只给"何时发什么"（release_name/release_date/notes），
    **没有 actual / prev**（history 甚至忽略 release_id 参数）。
    → 解说只能说"几点有 FOMC 纪要"，**绝不许报数字**，否则模型必编
    "市场预期 3.8%"这种。铁律见 narrate.py。
 5. **代理抖动**：同一端点连打会偶发 000（不是被封，是代理不稳）→ 必须重试。

安全：API key 只从环境变量 `EULERPOOL_API_KEY` 读，**不入库、不打日志**。
日志只输出端点与条数。

运行：python3 eulerpool_src.py --out data/snapshot.json --patch
"""

import argparse
import datetime as dt
import json
import os
import ssl
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# 复用 markets.py 的快讯归一化结构，保证页面渲染逻辑完全一致
NEWS_FIELDS = ("title", "text", "link", "time", "src", "chan", "foreign")


def _log(msg):
    sys.stderr.write("[eulerpool] %s\n" % msg)
    sys.stderr.flush()


def _cfg():
    p = os.path.join(HERE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            return (json.load(f).get("eulerpool") or {})
    except Exception:                                            # noqa: BLE001
        return {}


def _opener(proxy):
    """构造带代理的 opener。proxy 为空则直连（会被 403，仅调试用）。"""
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        h = urllib.request.ProxyHandler({})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= 0x100  # OP_IGNORE_UNEXPECTED_EOF：兼容出口代理偶发 TLS 提前关闭
    return urllib.request.build_opener(h, urllib.request.HTTPSHandler(context=ctx))


def _timed_open(opener, req, timeout):
    """在线程里发起请求，join 硬超时返回。

    ⚠️ 关键：经代理的 HTTPS CONNECT 隧道里，urllib 的 socket 超时在握手/读阶段
    可能不生效（上游/代理挂起时线程静默阻塞）→ 整轮循环被卡死（V1.9.21 的根因）。
    改为线程 + join(timeout+3)，即便底层 socket 不超时，join 到点也强制收回控制权，
    主流程继续（该请求判失败、优雅降级），绝不再拖垮循环。"""
    box = {}

    def _w():
        try:
            box["r"] = opener.open(req, timeout=timeout)
        except Exception as e:                          # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_w, daemon=True)
    th.start()
    th.join(timeout + 3)
    if "r" in box:
        return box["r"], None
    if "e" in box:
        return None, box["e"]
    return None, RuntimeError("网络在 %ds 内无响应（代理/上游挂起，已放弃）" % timeout)


def _get_json(cfg, path, params=None):
    """GET 一个 eulerpool 端点，返回 (obj, err)。err 非 None 表示失败。"""
    key = os.environ.get(cfg.get("api_key_env") or "EULERPOOL_API_KEY", "").strip()
    if not key:
        return None, "无 API key（环境变量 %s 未设置）" % cfg.get("api_key_env")
    base = (cfg.get("base_url") or "https://api.eulerpool.com/api/1").rstrip("/")
    url = base + path
    if params:
        url += "?" + "&".join("%s=%s" % (k, v) for k, v in params.items())
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % key,
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json",
    })
    timeout = int(cfg.get("timeout") or 15)
    retries = max(1, int(cfg.get("retries") or 2))
    # V1.9.18：优先用环境变量出口代理（沙箱代理端口会变，config 里的 7897 是过期值）；
    # 仅在环境变量缺失时回退 config.json 的 eulerpool.proxy。
    op = _opener(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or cfg.get("proxy"))
    last = None
    for i in range(retries):
        try:
            resp, err = _timed_open(op, req, timeout)
            if err:
                raise err
            return json.loads(resp.read().decode("utf-8", "ignore")), None
        except Exception as e:                                    # noqa: BLE001
            last = e
            # 代理抖动（000/超时/断连）值得重试；403 是 IP 被拉黑，重试无意义
            if "403" in str(e):
                return None, "HTTP 403（出口 IP 被 Cloudflare 拉黑，换代理端口）"
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    return None, "%s: %s" % (type(last).__name__, last)


def _norm_news(items, ticker):
    """eulerpool news 原始字段 → 项目统一的快讯结构。

    ⚠️ 时间戳是 **unix 秒**（不是 ISO），实测字段：
       related/category/headline/id/image/source/summary/url/datetime/realUrl
    ⚠️ 优先用 realUrl（原文），退回 url（实测是 finnhub 代理链接）。
    """
    out = []
    for x in items or []:
        if not isinstance(x, dict):
            continue
        title = (x.get("headline") or "").strip()
        if not title:
            continue
        ts = x.get("datetime")
        when = ""
        try:
            ts = int(ts)
            if ts > 0:
                when = dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        except Exception:                                        # noqa: BLE001
            when = ""
        link = (x.get("realUrl") or x.get("url") or "").strip()
        summary = (x.get("summary") or "").strip()
        out.append({
            "title": title[:140],
            "text": (summary or title)[:90],
            "link": link,
            "time": when,
            "src": "Eulerpool",
            "chan": ("个股·%s" % ticker) if ticker else "个股",
            "foreign": True,
        })
    return out


def fetch_ticker_news(cfg, tickers=None, per_ticker=3):
    """拉美股个股新闻。**逐个 ticker 单独请求**（该 API 无批量端点）。"""
    lst = list(tickers or cfg.get("tickers") or [])
    allrows, ok, fail = [], 0, []
    for t in lst:
        obj, err = _get_json(cfg, "/research/news/%s" % t)
        if err:
            fail.append("%s(%s)" % (t, err))
            continue
        rows = _norm_news(obj, t)
        if not rows:
            fail.append("%s(空)" % t)
            continue
        ok += 1
        # 同 ticker 内按时间倒序取前 N 条，避免一股刷屏
        rows.sort(key=lambda r: r.get("time") or "", reverse=True)
        allrows.extend(rows[:per_ticker])
    return allrows, ok, fail


def fetch_econ_calendar(cfg, days=None, country=None):
    """拉经济数据官方发布日历。

    ⚠️ **只有"何时发什么"，没有 actual/prev 数值**（实测连 history 端点也
    拿不到）→ prompt 里只能当"议程"，**禁止报数字**。
    """
    days = int(days or cfg.get("calendar_days_ahead") or 5)
    ctry = country or cfg.get("calendar_country") or "US"
    d0 = dt.date.today()
    obj, err = _get_json(cfg, "/calendar/economic-calendar",
                         {"from": d0.isoformat(),
                          "to": (d0 + dt.timedelta(days=days)).isoformat(),
                          "countries": ctry})
    if err:
        return [], err
    items = obj if isinstance(obj, list) else (obj or {}).get("data") or []
    rows = []
    for x in items:
        if not isinstance(x, dict):
            continue
        name = (x.get("release_name") or "").strip()
        when = (x.get("release_date") or "").strip()
        if not name:
            continue
        # 官方原始出处藏在 notes 里（实测含 federalreserve.gov 等）
        src_url = ""
        notes = (x.get("notes") or "")
        if "http" in notes:
            import re
            m = re.search(r"https?://[^\s,;)]+", notes)
            src_url = m.group(0) if m else ""
        rows.append({"name": name, "date": when, "country": ctry,
                     "rid": x.get("release_id"), "src": src_url or None})
    # 同行合并（FOMC Press Release 之类可能多天），按日期+名称去重
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda z: (z["date"], z["name"])):
        k = (r["date"], r["name"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.path.join(HERE, "data", "snapshot.json"))
    ap.add_argument("--patch", action="store_true",
                    help="只把结果写进已有快照（不新建）")
    ap.add_argument("--tickers", default="", help="覆盖配置的 ticker 列表（逗号分隔）")
    ap.add_argument("--print", action="store_true", help="只打印不落盘")
    a = ap.parse_args()

    cfg = _cfg()
    if not cfg.get("enabled", True):
        _log("配置为 disabled，跳过")
        return

    tickers = [x.strip().upper() for x in a.tickers.split(",") if x.strip()] or None
    rows, ok, fail = fetch_ticker_news(cfg, tickers)
    _log("个股新闻：%d 只成功 / %d 条（失败 %d）"
         % (ok, len(rows), len(fail)))
    if fail:
        _log("  失败明细：%s" % "、".join(fail[:8]))
    cal, cerr = fetch_econ_calendar(cfg)
    _log("经济日历：%d 条%s" % (len(cal), ("（%s）" % cerr) if cerr else ""))
    for r in cal[:5]:
        _log("    %s %s" % (r["date"], r["name"][:56]))

    if a.print:
        for r in rows[:10]:
            _log("    · %s | %s" % (r["time"], r["title"][:70]))
        return

    if not rows and not cal:
        _log("本轮无任何结果，快讯池保持原样（优雅降级）")
        return

    # ---- 落盘：并进快照的 news 列表 + 挂 econ_cal ----
    snap = {}
    if os.path.exists(a.snapshot):
        with open(a.snapshot, encoding="utf-8") as f:
            snap = json.load(f)
    news = list(snap.get("news") or [])
    have = {(x.get("link") or x.get("title") or "") for x in news}
    added = 0
    for r in rows:
        k = r.get("link") or r.get("title")
        if k and k in have:          # 去重：不重复推同一篇
            continue
        news.append(r)
        have.add(k)
        added += 1
    # ⚠️ V1.9.4：与 markets.py 的合并池保持一致，按发布时间倒序，
    #   今天的全球快讯（CNBC/SeekingAlpha…）排在最前，避免个股新闻插队挤掉当日头条。
    news.sort(key=lambda x: (x.get("time") or "", x.get("title") or ""), reverse=True)
    # ⚠️ V1.9.13b：截断前给国内源（foreign=False）保留固定额度。
    #   原 `news[:80]` 纯靠 time 字符串排序，一旦某源时间格式不齐就会整批沉底被截掉
    #   （实测：东财显示文本误为 "08 11:04:00" 而非 "10-08 11:04"，12 条国内快讯全丢，
    #   页面完全看不到国内信源）。现改为「先按时间取足国内源 → 再用最新外媒补满 → 整体重排」。
    CAP = 80
    DOM_KEEP = 12
    dom = [x for x in news if x.get("foreign") is False][:DOM_KEEP]
    rest = [x for x in news if x.get("foreign") is not False][:max(0, CAP - len(dom))]
    kept = dom + rest
    kept.sort(key=lambda x: (x.get("time") or "", x.get("title") or ""), reverse=True)
    snap["news"] = kept
    if cal:
        snap["econ_cal"] = cal
    if a.patch and not os.path.exists(a.snapshot):
        _log("快照不存在且指定了 --patch，跳过写盘")
        return
    import tempfile
    d = os.path.dirname(a.snapshot) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, a.snapshot)
    _log("已写回 %s（新增 %d 条，快讯池 %d 条）" % (a.snapshot, added, len(snap["news"])))


if __name__ == "__main__":
    main()
