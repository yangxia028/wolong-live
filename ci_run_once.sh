#!/usr/bin/env bash
# 股市解说直播 · GitHub Actions 单次管线
# 对应本地 run_live.sh 的「一轮」，但：无死循环、无 macOS 专属锁/trap、用环境 python3。
# 由 .github/workflows/deploy.yml 每 5 分钟调用一次；状态经 actions/cache 跨轮持久化。
#
# 环境（由 Actions Secrets 注入）：
#   AGNES_API_KEY      解说词 api 引擎必需
#   EULERPOOL_API_KEY  可选；不设在云端直接跳过（无代理、且被 Cloudflare 拦）
# 可调环境变量：COLLECT_CN(auto|1|0) / ENGINE(api|local) / PRESET(agnes) / TIER(calm) / IV(300)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
PY="${PYTHON:-python3}"

ENGINE="${ENGINE:-api}"
PRESET="${PRESET:-agnes}"
COLLECT_CN="${COLLECT_CN:-auto}"
TIER="${TIER:-calm}"
IV="${IV:-300}"

mkdir -p "$HERE/_site" "$HERE/logs"
LOG="$HERE/logs/ci_run.log"

{
  echo "=== CI 单次管线启动 $(date -u '+%F %T UTC') 引擎=$ENGINE 采A股=$COLLECT_CN ==="

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

  # ---- 港美股行情 / 时段 / 外媒快讯 ----
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
  echo "--- 一轮完成，产物 $(wc -c < _site/live.html) bytes ---"
} >>"$LOG" 2>&1
