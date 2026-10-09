#!/usr/bin/env bash
# 股市解说直播 · GitHub Actions 单次管线
# 对应本地 run_live.sh 的「一轮」，但：无死循环、无 macOS 专属锁/trap、用环境 python3。
# 由 .github/workflows/deploy.yml 每 5 分钟调用一次；状态经 actions/cache 跨轮持久化。
#
# 环境（由 Actions Secrets 注入）：
#   AGNES_API_KEY      解说词 api 引擎必需；缺失则自动回退 local 引擎
#   EULERPOOL_API_KEY  可选；不设在云端直接跳过（无代理、且被 Cloudflare 拦）
# 可调环境变量：COLLECT_CN(auto|1|0) / ENGINE(api|local) / PRESET(agnes) / TIER(calm) / IV(300) / TZ
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
PY="${PYTHON:-python3}"

# ---- 时区铁律（V1.9.39 CI 侧兜底；V1.9.40 起代码内 cn_tz 已进程级锚定）------
#   V1.9.40 起：各脚本 import cn_tz 即强制进程时区 = Asia/Shanghai，
#   不再依赖本行环境变量；此处仅作双保险。详见 cn_tz.py 头部注释。
# CI runner 默认 TZ=UTC，而管线里凡是**裸时间**都按进程本地时区走：
#   · markets.market_state() 的 `now = now or dt.datetime.now()` → 判当前场次
#   · narrate / collect / build 里 time.strftime、time.localtime 生成的时间戳
#   · narrate 的 cn_fresh 日期比较、collect_live --date 默认交易日
# 本地 macOS 是 CST 所以一直正确，CI 上会整体错位 8 小时——
# 实测 2026-10-09：北京 17:12 → runner UTC 09:12 → 被判成 A股「盘前」(盘前段 480-570 分)，
# 页面/解说时间戳也全显示 09:12。这里显式对齐北京时间，行为与本地 100% 一致。
export TZ="${TZ:-Asia/Shanghai}"

ENGINE="${ENGINE:-api}"
PRESET="${PRESET:-agnes}"
COLLECT_CN="${COLLECT_CN:-auto}"
TIER="${TIER:-calm}"
IV="${IV:-300}"

mkdir -p "$HERE/_site" "$HERE/logs" "$HERE/data"
LOG="$HERE/logs/ci_run.log"
SUMMARY="$(mktemp -t live_summary 2>/dev/null || echo "$HERE/logs/.summary.md")"

{
  echo "=== CI 单次管线启动 $(date '+%F %T %Z(%z)') 引擎=$ENGINE 采A股=$COLLECT_CN ==="

  # ---- config.json：CI 无此文件（已 gitignore，避免密钥入仓）→ 由 example + Secret 现场生成 ----
  # narrate.py --preset 需从 config.json 读 base/model；key 优先走 key_env(AGNES_API_KEY) 环境变量。
  if [ ! -f "$HERE/config.json" ]; then
    if [ -f "$HERE/config.json.example" ]; then
      echo "--- 生成 config.json（来自 example + Secret）---"
      AGNES_API_KEY="${AGNES_API_KEY:-}" "$PY" - <<'PY'
import json, os
cfg = json.load(open("config.json.example", encoding="utf-8"))
ag = cfg.setdefault("llm", {}).setdefault("presets", {}).setdefault("agnes", {})
k = os.environ.get("AGNES_API_KEY", "")
if k:
    ag["key"] = k
json.dump(cfg, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("[ok] config.json 生成，agnes key %s" % ("已注入" if k else "缺失(将回退 local)"))
PY
    else
      echo "[!] 无 config.json.example，无法生成 config.json"
    fi
  fi

  # ---- 冷启动兜底：markets.py 假定 data/snapshot.json 已存在（本地一直由 collect_live.py 先建），
  #      CI 冷启动（首次 / actions-cache 失效）时 data/ 为空 → markets.py 会 FileNotFoundError 整轮崩。
  #      这里补一个空壳 {}，markets.py 对其赋值式打补丁即可自举出完整快照（main 内读均带 .get 兜底）。----
  if [ ! -f "$HERE/data/snapshot.json" ]; then
    echo "--- 冷启动：无 snapshot.json → 写入空壳自举 ---"
    printf '{}' > "$HERE/data/snapshot.json"
  fi

  # ---- 无 key 时 api 引擎必失败 → 提前回退 local，保证页面仍有解说 ----
  if [ "$ENGINE" = "api" ] && [ -z "${AGNES_API_KEY:-}" ]; then
    echo "[!] 未设 AGNES_API_KEY → 回退 local 引擎"
    ENGINE="local"
  fi

  # ---- A股全市场采集（护东财额度：auto 仅当今日确有行情才采）----
  if [ "$COLLECT_CN" = "1" ]; then
    echo "--- A股全市场采集 ---"
    "$PY" collect_live.py 2>&1 | tail -8
  elif [ "$COLLECT_CN" = "auto" ]; then
    traded=$("$PY" -c "import json
try:
    s=json.load(open('data/snapshot.json',encoding='utf-8'))
    print(1 if (((s.get('session') or {}).get('all') or {}).get('cn') or {}).get('traded_today') else 0)
except Exception:
    print(0)" 2>/dev/null || echo 0)
    if [ "$traded" = "1" ]; then
      echo "--- A股全市场采集（traded_today=1）---"
      "$PY" collect_live.py 2>&1 | tail -8
    else
      echo "--- 跳过 A股采集（traded_today=0/未知）---"
    fi
  fi

  # ---- 港美股行情 / 时段 / 外媒快讯（snapshot.json 主生产者，须在 narrate 之前）----
  echo "--- 港美股行情/时段/外媒快讯 ---"
  "$PY" run_to.py 180 "$PY" markets.py 2>&1 | tail -12

  # ---- eulerpool（云端默认跳过）----
  if [ -n "${EULERPOOL_API_KEY:-}" ]; then
    echo "--- eulerpool ---"
    "$PY" run_to.py 90 "$PY" eulerpool_src.py --patch 2>&1 | tail -6
  else
    echo "--- eulerpool 跳过（未设密钥）---"
  fi

  # ---- RRG 跨资产轮动（非致命：抓取失败整轮跳过）----
  echo "--- RRG 跨资产轮动（非致命）---"
  "$PY" run_to.py 25 "$PY" collect_rrg.py 2>&1 | tail -4 || echo "[!] collect_rrg 失败，跳过"

  # ---- 解说词（api 引擎需 AGNES_API_KEY）----
  echo "--- 生成解说词（$ENGINE 引擎）---"
  if [ "$ENGINE" = "api" ]; then
    "$PY" run_to.py 240 "$PY" narrate.py --engine api --preset "$PRESET" 2>&1 | tail -8 || echo "[!] narrate 失败，跳过"
  else
    "$PY" run_to.py 240 "$PY" narrate.py --engine local 2>&1 | tail -8 || echo "[!] narrate 失败，跳过"
  fi

  # ---- 重建 live.html（关键步骤：失败即终止，绝不部署陈旧页）----
  echo "--- 重建 live.html ---"
  "$PY" build_live.py --tier "$TIER" --refresh "$IV" >>"$LOG" 2>&1 || { echo "[!] build_live 失败，终止"; exit 1; }

  # ---- 拷贝产物到 _site（仅 live.html + 404 兜底；不泄露源码/状态）----
  cp live.html _site/live.html
  cat > _site/404.html <<'HTML'
<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>卧龙 · 股市直播</title>
<meta http-equiv="refresh" content="0;url=/live.html">
</head><body style="font-family:-apple-system,system-ui,'PingFang SC',sans-serif;text-align:center;padding-top:22vh;color:#232220">
<p>页面刷新中，正在跳转到 <a href="/live.html" style="color:#8A6A33">股市直播</a>…</p></body></html>
HTML

  # ---- 自检摘要（同步到 Actions 运行页，免受"翻全量日志"之苦）----
  "$PY" - <<'PY' > "$SUMMARY" 2>/dev/null || true
import glob, json, os, time


def j(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


print("## 股市直播 · 本轮自检")
print()
print("| 项 | 值 |")
print("|---|---|")
print("| 运行时间 | %s |" % time.strftime("%Y-%m-%d %H:%M:%S %Z"))

s = j("data/snapshot.json", {}) or {}
sess = s.get("session") or {}
act = sess.get("active_names") or sess.get("active") or sess.get("active_name") or "-"
if isinstance(act, (list, tuple)):
    act = "/".join(str(x) for x in act)
print("| 当前场次 | %s |" % act)
print("| 场次检查于 | %s |" % (sess.get("checked_at") or "-"))

nw = s.get("news")
if isinstance(nw, dict):
    nw = nw.get("items") or nw.get("list") or nw.get("all") or []
print("| 快讯池 | %s 条 |" % (len(nw) if isinstance(nw, list) else "-"))

cn = ((sess.get("all") or {}).get("cn") or {})
print("| A股 traded_today | %s |" % (cn.get("traded_today")))

for p in sorted(glob.glob("data/commentary_*.json") + glob.glob("commentary_*.json")):
    d = j(p) or {}
    n = "-"
    for k in ("segments", "segs", "items", "lines"):
        v = d.get(k)
        if isinstance(v, list):
            n = len(v)
            break
    print("| 解说 %s | %s 段 |" % (os.path.basename(p), n))

rrg = j("data/rrg_state.json", {}) or {}
if rrg:
    print("| RRG | %s |" % (rrg.get("fetched_at") or "-"))

try:
    print("| live.html | %d bytes |" % os.path.getsize("_site/live.html"))
except Exception:
    pass
PY
  echo "--- 自检摘要 ---"
  sed 's/^/    /' "$SUMMARY" 2>/dev/null
  echo "--- 一轮完成，产物 $(wc -c < _site/live.html) bytes ---"
} >>"$LOG" 2>&1

# 摘要同步到 Actions 运行页（非致命：失败不影响部署）
if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && [ -s "$SUMMARY" ]; then
  cat "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi
rm -f "$SUMMARY"
exit 0
