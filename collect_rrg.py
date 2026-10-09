#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · D 维度采集 · RRG 跨资产轮动看板（只读解析）
=====================================================================
职责：抓取已上线的 RRG 静态页 https://yangxiaa.cc/rrg/ ，解析其中的
      「跨资产轮动状态」表格，落盘为 data/rrg_state.json，供解说层
      当作「盘面背景态」消费。

设计铁律（师傅 2026-10-09 拍板）：
  * **只读不改** —— 绝不碰 RRG 源项目生态；只 GET 公开静态页 + 解析 DOM。
  * 云端可达 —— 直播项目后续要部署到云端，本地看板读不到；RRG 是已上线的
    公开静态页（HTTP 200 / 无鉴权 / 数据在 DOM），符合「云端可达」原则。
  * 不依赖外部 JSON 端点（RRG 没有对外 API），直接 HTML 表格解析。
  * 纯标准库（urllib + re），零第三方依赖，沙箱可执行。

输出 data/rrg_state.json：
  {
    "source": "https://yangxiaa.cc/rrg/",
    "fetched_at": "2026-10-09T13:30:00",
    "data_date": "2026-10-08",          # 页内「数据截止」
    "version": "v2.23.1",               # 页内版本
    "assets": [ {asset, category, quadrant, rs_ratio, rs_mom, heat,
                 prob_10d, prob_10d_n, prob_60d, prob_60d_n, direction}, ... ],
    "summary": {
        "quadrant_counts": {"强势":2,"回升":2,"弱势":6},
        "overheated": [...],            # 方向参考含「过热/回避」
        "strengthening": [...],          # RS-Mom>100 且非过热
        "a_share_related": [...],       # 沪深300/创业板/恒生（与 A股盘面直接联动）
        "risk_read": "..."              # 一句话风险偏好判读
    }
  }

运行：python3 collect_rrg.py [--out data/rrg_state.json] [--url https://yangxiaa.cc/rrg/]
"""
import argparse
import datetime as _dt
import json
import os
import re
import ssl
import sys
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

DEFAULT_URL = "https://yangxiaa.cc/rrg/"
OUT_DEFAULT = os.path.join(HERE, "data", "rrg_state.json")
# A股盘面直接联动的资产（RRG 是跨资产，但这几类与 A股解说强相关）
A_SHARE_KEYS = ("沪深300", "创业板", "恒生")

_HEADERS = ["asset", "category", "quadrant", "rs_ratio", "rs_mom", "heat",
            "prob_10d", "prob_10d_n", "prob_60d", "prob_60d_n", "direction"]


def _fetch(url, timeout=25, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read().decode("utf-8", "ignore")
            if raw.strip():
                return raw
            last = "empty body"
        except Exception as e:
            last = e
            _sleep = 1.5 * (i + 1)
            print("[rrg] 第%d次抓取失败：%s，%.1fs 后重试" % (i + 1, e, _sleep), flush=True)
            import time
            time.sleep(_sleep)
    raise RuntimeError("RRG 抓取失败（已重试%d次）：%s" % (retries, last))


def _parse_prob(cell):
    """ '56% n=419' -> (0.56, 419) ; 异常返回 (None, None) """
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:n\s*=\s*(\d+))?", cell)
    if not m:
        return None, None
    pct = float(m.group(1)) / 100.0
    n = int(m.group(2)) if m.group(2) else None
    return pct, n


def _parse(html):
    # 元信息：数据截止 / 版本
    data_date = None
    version = None
    m = re.search(r"数据截止\s*(\d{4}-\d{2}-\d{2})", html)
    if m:
        data_date = m.group(1)
    m = re.search(r"v(\d+\.\d+\.\d+)", html)
    if m:
        version = m.group(1)

    # 唯一表格
    tabs = re.findall(r"<table.*?</table>", html, re.S)
    if not tabs:
        raise RuntimeError("RRG 页未找到 <table>")
    rows = re.findall(r"<tr.*?</tr>", tabs[0], re.S)

    assets = []
    for r in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[hd].*?</t[hd]>", r, re.S)]
        if not cells:
            continue
        # 表头行跳过
        if cells[0] == "资产" and "RS-Ratio" in cells:
            continue
        if len(cells) < 9:
            continue
        p10, n10 = _parse_prob(cells[6])
        p60, n60 = _parse_prob(cells[7])
        try:
            rec = {
                "asset": cells[0],
                "category": cells[1],
                "quadrant": cells[2],
                "rs_ratio": float(cells[3]),
                "rs_mom": float(cells[4]),
                "heat": int(cells[5]),
                "prob_10d": p10,
                "prob_10d_n": n10,
                "prob_60d": p60,
                "prob_60d_n": n60,
                "direction": cells[8],
            }
        except (ValueError, IndexError) as e:
            print("[rrg] 跳过无法解析的行 %r: %s" % (cells, e), flush=True)
            continue
        assets.append(rec)

    if not assets:
        raise RuntimeError("RRG 表格解析为空（结构可能已变）")

    # ---- summary ----
    qc = {}
    for a in assets:
        qc[a["quadrant"]] = qc.get(a["quadrant"], 0) + 1
    overheated = [a["asset"] for a in assets
                  if "过热" in a["direction"] or "回避" in a["direction"]]
    strengthening = [a["asset"] for a in assets
                     if a["rs_mom"] > 100 and "过热" not in a["direction"]]
    a_rel = [a for a in assets if a["asset"] in A_SHARE_KEYS]

    # 一句话风险偏好判读（轻量规则，仅作背景提示）
    n_strong = qc.get("强势", 0)
    n_weak = qc.get("弱势", 0)
    if overheated:
        risk_read = ("风险资产偏热（%s 过热/回避），追高性价比低；"
                     % "、".join(overheated))
    else:
        risk_read = ""
    if a_rel:
        weak_a = [a["asset"] for a in a_rel if a["quadrant"] == "弱势"]
        if weak_a:
            risk_read += ("A股相关（%s）仍处弱势，盘面承压背景未改；"
                          % "、".join(weak_a))
        else:
            risk_read += "A股相关资产已脱离弱势。"
    if not risk_read:
        risk_read = "跨资产轮动均衡，未见明显过热或极端弱势。"

    return {
        "data_date": data_date,
        "version": version,
        "assets": assets,
        "summary": {
            "quadrant_counts": qc,
            "overheated": overheated,
            "strengthening": strengthening,
            "a_share_related": [a["asset"] for a in a_rel],
            "risk_read": risk_read,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    print("[rrg] 抓取 %s ..." % args.url, flush=True)
    html = _fetch(args.url)
    parsed = _parse(html)

    out = {
        "source": args.url,
        "fetched_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "data_date": parsed["data_date"],
        "version": parsed["version"],
        "assets": parsed["assets"],
        "summary": parsed["summary"],
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[rrg] 已落盘 %s：%d 类资产 | 象限分布 %s"
          % (args.out, len(out["assets"]), parsed["summary"]["quadrant_counts"]),
          flush=True)
    print("[rrg] 风险判读：%s" % parsed["summary"]["risk_read"], flush=True)


if __name__ == "__main__":
    main()
