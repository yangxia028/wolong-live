#!/bin/bash
# 股市解说直播 · 云端数据推送（V1.9.42 架构升级）
# =====================================================================
# 职责：把本地循环每轮产出的 data/live_data.json 推到 GitHub 的 `data` 分支。
#   · `data` 是**非 Pages 生产分支**（生产分支=main），推送它**不会触发 Pages 构建**
#     → 零 Pages 部署额度消耗（这正是升级 ③ 的目的）。
#   · 线上固定页(pages.dev)首屏异步拉：
#       https://raw.githubusercontent.com/yangxia028/wolong-live/data/live_data.json
#     该 URL 返回 Access-Control-Allow-Origin: *，跨域放行。
#   · 本地预览(127.0.0.1:8800) 不受影响：serve_live.py 直接读本地 data/live_data.json。
# 失败容错：推不上去（网络/密钥）只告警、不中断直播主循环。
# 用法：./push_data.sh   （由 run_live.sh 每轮 build 之后调用）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/data/live_data.json"
[ -f "$SRC" ] || { echo "[push_data] 无 $SRC，跳过"; exit 0; }

BRANCH="${DATA_BRANCH:-data}"
URL="$(git -C "$HERE" remote get-url origin 2>/dev/null || echo "git@wolong-github:yangxia028/wolong-live.git")"
CACHE="$HOME/.cache/wolong-live-data"

# ---- 首次：浅克隆 data 分支；分支尚不存在则建 orphan ----
if [ ! -d "$CACHE/.git" ]; then
  rm -rf "$CACHE"
  if ! git clone --depth 1 --branch "$BRANCH" "$URL" "$CACHE" 2>/dev/null; then
    git clone --depth 1 "$URL" "$CACHE" 2>/dev/null || { echo "[push_data] 克隆失败，跳过"; exit 0; }
    ( cd "$CACHE" && git checkout --orphan "$BRANCH" && git rm -rf . >/dev/null 2>&1 ) || { echo "[push_data] 建分支失败，跳过"; exit 0; }
  fi
fi

cd "$CACHE" || { echo "[push_data] 无法进入缓存仓，跳过"; exit 0; }
git fetch origin "$BRANCH" 2>/dev/null && git checkout "$BRANCH" 2>/dev/null && git reset --hard "origin/$BRANCH" 2>/dev/null \
  || git checkout -B "$BRANCH" 2>/dev/null || { echo "[push_data] 切分支失败，跳过"; exit 0; }

cp "$SRC" "$CACHE/live_data.json"
git add live_data.json
# 无变化则静默退出（避免空提交刷历史）
if git diff --cached --quiet; then
  echo "[push_data] 数据无变化，跳过提交"
  exit 0
fi
git -c user.email=wolong@local -c user.name=wolong commit -q -m "data $(date '+%F %T %Z')" \
  || { echo "[push_data] 提交失败，跳过"; exit 0; }
if git push origin "$BRANCH" 2>&1 | tail -3; then
  echo "[push_data] ✓ 已推送到 $BRANCH 分支"
else
  echo "[push_data] ✗ 推送失败（不影响本地直播）"
fi
exit 0
