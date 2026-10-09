#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 比赛态势引擎（V1.2.0 新增）
=====================================================================
职责：把 A 股全市场维度翻译成「比赛态势」—— 用于页面的「本场状况」模块，
      而不是干巴巴的指数列表。
概念映射（体育语言）：
  比分      → 四大指数点位+ 涨跌幅
  攻守势→ 涨跌家数比 + 涨停/跌停数 = 「谁在攻谁在守」
  最佳球员  → 成交额≥10亿 且 换手≥5% 的池子里涨幅最高者 = 「本场最佳」
              （不设人工评分：页面上每个数字都必须能溯源到行情源）
  明星球员  → 连板高度股 = 「全场最耀眼的那个」
  出界/意外 → 跌幅榜第一 = 「本场乌龙」
  节奏     → 分布直方图 = 「比赛节奏（快攻/稳守/僵持）」
输出：attach 到 snapshot 的 game.json，页面直接渲染，不改采集层。
运行：python3 game_state.py
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(line_buffering=True)


def log(m):
    print(m, flush=True)


def _pct(x):
    return x if isinstance(x, (int, float)) else None


def classify_rhythm(dist):
    """按涨跌分布判「比赛节奏」：一方压倒= 压制；五五开 = 僵持。"""
    d = dist or {}
    up = sum(v for k, v in d.items() if k in ("涨停", ">+5%", "+2~5%", "0~2%"))
    dn = sum(v for k, v in d.items() if k in ("跌停", ">-5%", "-2~0%"))
    tot = up + dn
    if not tot:
        return {"key": "unknown", "label": "数据不足", "note": ""}
    r = up / float(tot)
    if r >= 0.62:
        return {"key": "hot", "label": "主队压制", "note": "红盘全面占优",
                "up": up, "down": dn, "ratio": round(r * 100, 1)}
    if r <= 0.38:
        return {"key": "cold", "label": "客队压制", "note": "绿盘全面压制",
                "up": up, "down": dn, "ratio": round(r * 100, 1)}
    return {"key": "even", "label": "上下焦灼", "note": "红绿势均力敌",
            "up": up, "down": dn, "ratio": round(r * 100, 1)}


def mvp(snap):
    """本场最佳球员：涨幅 / 成交额 / 换手 三项各挑冠军，综合打分。"""
    cands = {}
    for key in ("gainers", "active"):
        for x in (snap.get(key) or {}).get("rows") or []:
            c = cands.setdefault(x.get("code"), {"code": x.get("code"), "name": x.get("name"),
                                                 "price": x.get("price"), "pct": x.get("pct"),
                                                 "amount": x.get("amount"), "turn": x.get("turn")})
            c.update({k: x.get(k) for k in ("price", "pct", "amount", "turn") if x.get(k) is not None})
    # MVP 选取口径（三料齐备才叫MVP，不是打分——分数是人工构造，页面不展示）：
    #   成交额 >= 10 亿 且 换手 >= 5% 的池子里取涨幅最高者；池空则退化为涨幅榜第一。
    # 这样 MVP 的每个字段都是真实可溯源的行情数据，页面上不出现任何人工评分。
    pool = [x for x in cands.values()
            if (x.get("amount") or 0) >= 1e9 and (x.get("turn") or 0) >= 5]
    if pool:
        return max(pool, key=lambda x: _pct(x.get("pct")) or -999)
    return max(cands.values(), key=lambda x: _pct(x.get("pct")) or -999) if cands else None


def fail_of(snap):
    """本场乌龙：跌幅榜第一。"""
    rows = (snap.get("losers") or {}).get("rows") or []
    return rows[0] if rows else None


def star_of(snap):
    """全场最耀眼：连板高度股（无则用涨停第一）。"""
    pool = (snap.get("zt") or {}).get("pool") or []
    multi = [x for x in pool if (x.get("boards") or 0) >= 2]
    if multi:
        return max(multi, key=lambda x: x.get("boards") or 0)
    return pool[0] if pool else None


def build(snap):
    b = snap.get("breadth") or {}
    zt = (snap.get("zt") or {}).get("tc")
    dt = (snap.get("dt") or {}).get("tc")
    m = mvp(snap)
    f = fail_of(snap)
    st = star_of(snap)
    rh = classify_rhythm(b.get("dist"))

    # 比分卡：本市场主要指数
    cur = (snap.get("markets") or {})
    return {
        "rhythm": rh,
        "score": b,
        "mvp": m,
        "fail": f,
        "star": st,
        "zt": zt, "dt": dt,
        "mkt": {k: v for k, v in cur.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.path.join(HERE, "data", "snapshot.json"))
    a = ap.parse_args()
    with open(a.snapshot, encoding="utf-8") as f:
        snap = json.load(f)
    g = build(snap)
    snap["game"] = g
    # 原子写：先写临时文件再 os.replace，中途崩溃不会毁掉原文件
    import os as _os, tempfile as _tf
    _fd, _tmp = _tf.mkstemp(dir=_os.path.dirname(a.snapshot) or '.', suffix='.tmp')
    with _os.fdopen(_fd, 'w', encoding='utf-8') as _f:
        json.dump(snap, _f, ensure_ascii=False, indent=1)
    _os.replace(_tmp, a.snapshot)

    m, f, s, rh = g["mvp"], g["fail"], g["star"], g["rhythm"]
    log("[ok] 比赛态势已写入：节奏=%s" % rh["label"])
    if m:
        log("     本场最佳 %s（%+.2f%% · 成交%.1f亿 · 换手%.1f%% · 数据源=异动榜）" % (
            m["name"], m.get("pct") or 0, (m.get("amount") or 0) / 1e8, m.get("turn") or 0))
    if s:
        log("     全场最佳 %s（%s 连板）" % (s["name"], s.get("boards") or 1))
    if f:
        log("     本场乌龙 %s（%+.2f%%）" % (f["name"], f.get("pct") or 0))


if __name__ == "__main__":
    main()
