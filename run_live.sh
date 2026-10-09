#!/bin/bash
# 股市解说直播 · 本地「直播循环」（预览用，不影响生产快照口径）
# =====================================================================
# 职责：每轮刷新：A股全市场(按需) → 港美股行情+时段+外媒快讯(markets.py)
#       → eulerpool 个股新闻/经济日历(eulerpool_src.py, 需 EULERPOOL_API_KEY)
#       → 解说词(narrate.py) → live.html(build_live.py)
# 用法：./run_live.sh [间隔秒数]     默认走两档自动策略（见下）
# 停止：Ctrl-C，或 pkill -f run_live.sh
# 日志：logs/live_loop.log
#
# 【刷新频率 · V1.9.6 师傅要求：只保留默认平稳态】
#   师傅原话：「不需要活跃解说，只保留默认一个平稳态即可」。
#   默认 SPEED=calm（config.refresh.tiers.calm，默认 300s）—— **单一稳态**。
#   active(120s) 两档自动切换已**停用**（代码保留：SPEED=auto/active 仍可显式指定，
#   但默认不再进活跃档）。原因：单轮 narrate API 实测 85~142s 已接近/超过 120s，
#   跳活跃档并不会真的更快，只会多打行情源（东财按频率封），且解说"看起来每2分钟"
#   实际是 ~300s，属自欺（V1.9.5 已把真实间隔显式打到页面）。
#   ⚠️ 另：东财「按频率不按 host 封」，采 A股全市场时仍强制不低于 calm 档。
#
# 【交易日判定 = 数据驱动 · V1.8.0】不靠自建节假日表：
#   markets.py 读行情源自带的「报价时间戳」→ session.all.<mkt>.traded_today。
#   A股国庆/春节休市、港股台风停市、临时停市，都自动体现为「报价停在上一交易日」。
#   COLLECT_CN=auto(默认) —— 仅当 A股「今日确有行情」时才跑 collect_live.py；
#   COLLECT_CN=1 强制采集（调试用）；COLLECT_CN=0 永不采集。
#   ⚠️ 本轮一旦要采 A股全市场，强制落到平稳档：东财「按频率不按 host 封」，
#      高频拉全市场会被限流（卧龙台多项目踩出来的红线）。
#
# 【轮内顺序不可颠倒】collect_live.py 会整份重写 snapshot（丢掉 markets/news/session），
#   所以它必须跑在 markets.py **之前**，由 markets.py 在其上打补丁。
#   「今天要不要采 A股」用的 session 取自上一轮 markets.py 写的快照（最多滞后一个间隔）。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/Users/yangxia/.workbuddy/binaries/python/versions/3.13.12/bin/python3
# ⚠️ V1.9.3 脱离终端（V1.9.2 起循环首启即被打断，师傅要开循环才发现）：
#   症状：`nohup ./run_live.sh &` 启动后，第一轮跑到 markets.py 的 `| tail -12`
#   时报 `Hangup: 1` → 整个 while 循环退出，进程只剩秒级。
#   根因：**nohup 只忽略启动那一次 shell 的 SIGHUP，脚本内部所有子进程
#   （python / tail / pipeline）仍继承原终端的 SIGHUP 处置**，终端一收信号就全挂。
#   修法：① 脚本内显式 `trap '' HUP`（忽略挂断信号，且**子进程自动继承忽略**）；
#         ② macOS 无 `setsid`，用 `nohup ... & disown` 从外部脱离。
#   校验：启动后必须看到多轮 `本轮：档位=...`，只出现一轮 = 又被挂了。
# ⚠️ **只忽略 HUP，不忽略 INT/TERM** —— 否则 Ctrl-C 与文档里写的
#    `pkill -f run_live.sh` 都会失效，循环就停不掉了。
trap '' HUP 2>/dev/null || true
# ⚠️ 形参变量名必须与「本轮实际间隔」分开（V1.8.1 修）：
#   早先叫 INTERVAL 的形参，在循环里被 `INTERVAL=$(...cut -f2)` 覆盖成上一轮算出的
#   间隔 → 第二轮起 `[ -n "$INTERVAL" ]` 恒真 → 永远 tier=manual，自动选档静默失效
#   （页面显示「自定义档」而实际用的还是两档值）。现拆成 ARG_IV（只读形参）+ IV（本轮）。
ARG_IV="${1:-}"                     # 若传了数字则覆盖自动选档
COLLECT_CN="${COLLECT_CN:-auto}"    # auto | 1 | 0
SPEED="${SPEED:-calm}"              # calm(默认·单一稳态) | auto | active | <seconds>
ENGINE="${ENGINE:-api}"             # api（走 --preset，AI 解说，默认）| local（本地模板，不烧额度）
PRESET="${PRESET:-agnes}"
IV=""                               # 本轮实际间隔（每轮由 pick_round 填）

mkdir -p "$HERE/logs"
LOG="$HERE/logs/live_loop.log"
cd "$HERE" || exit 1

# ---- V1.9.15 单实例锁（根治「双循环并发 → 页面数据挂掉」） ----
# ⚠️ 症状（2026-10-08 11:3x 实测）：两个 run_live.sh 同时在跑时，A 循环的
#   collect_live.py 会**整份重写 snapshot.json**（丢掉 markets/news/session，
#   见上方「轮内顺序不可颠倒」注释）；若 B 循环的 build_live.py 恰在这两步之间的
#   窗口读到快照，就会重建出 news=[]、session={}、markets={} 的页面 ——
#   浏览器上表现为「指数数据暂不可用 / 资讯暂不可用 / 全场休息」，
#   而且**把上一版好页面覆盖掉**（实测 live_data.json：index 4 条、news 0、session {}）。
#   为什么原来没挡住：start_loop.py 的 start() 只在启动瞬间 pgrep 查一次，
#   两个启动请求挨得近就双双通过；手工 `./run_live.sh` 更完全无保护。
#   修法：脚本自带 mkdir 原子锁（macOS 自带 bash 3.2 无 flock，mkdir 是原子操作）；
#   锁目录内记持有者 pid，持有者已死则判为僵尸锁自动接管，避免死锁。
LOCKDIR="$HERE/.loop.lock"
_lock_acquire() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ >"$LOCKDIR/pid"; echo "$$"; return 0
  fi
  _h=""
  [ -f "$LOCKDIR/pid" ] && _h=$(cat "$LOCKDIR/pid" 2>/dev/null)
  if [ -n "$_h" ] && kill -0 "$_h" 2>/dev/null; then
    echo "$_h"; return 1          # 真有人在跑
  fi
  rm -rf "$LOCKDIR" 2>/dev/null   # 僵尸锁：持有者已不在，接管
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo $$ >"$LOCKDIR/pid"; echo "$$"; return 0
  fi
  echo "${_h:-?}"; return 1
}
_HOLDER=$(_lock_acquire) || {
  echo "=== [$(date '+%F %T')] 单实例锁：已有直播循环在运行（PID ${_HOLDER}），本次启动放弃 ===" >>"$LOG"
  echo "[!] 已有直播循环在运行（PID ${_HOLDER}）。要重启请用：python3 start_loop.py restart"
  exit 0
}
# 只 trap EXIT（**不要** trap INT/TERM）—— 否则 Ctrl-C / pkill 会被吞掉、循环停不下来。
# EXIT 在正常退出、Ctrl-C、pkill(SIGTERM) 三种情况下都会触发，足够释放锁。
# V1.9.18：EXIT 同时把 loop_phase 复位 idle，避免循环被异常打断后前端永久「搜索中」。
trap 'set_phase idle; rm -rf "$LOCKDIR" 2>/dev/null' EXIT

# ---- 两档间隔（config.json 是单一事实源，读不到用兜底） ----
read_tiers() {
  "$PY" -c "import json;c=json.load(open('config.json'));t=c['refresh']['tiers'];print('%d %d'%(t['active'],t['calm']))" 2>/dev/null || echo "120 300"
}
TIERS=$(read_tiers)
CFG_ACTIVE=$(echo "$TIERS" | cut -d' ' -f1)
CFG_CALM=$(echo "$TIERS" | cut -d' ' -f2)

# ---- 读上一轮快照里的时段判定（纯本地读文件，零网络）----
# 输出：opens cn_traded   （opens=有市场在常规交易；cn_traded=A股今天有没有行情）
probe_session() {
  "$PY" -c "
import json,sys
try:
    s=json.load(open('data/snapshot.json',encoding='utf-8'))
except Exception:
    print('0 0'); sys.exit()
a=((s.get('session') or {}).get('all') or {})
op=1 if any((a.get(k) or {}).get('state')=='open' for k in ('cn','hk','us')) else 0
cn=1 if (a.get('cn') or {}).get('traded_today') else 0
print('%d %d'%(op,cn))
" 2>/dev/null || echo "0 0"
}

# ---- 选档 + 是否采 A股。返回：tier interval collect ----
# ⚠️ 函数名不能叫 select —— 那是 bash 保留字（select name in ...），定义直接语法错误
pick_round() {
  local ops cn_traded collect tier iv
  set -- $(probe_session)
  ops="${1:-0}"; cn_traded="${2:-0}"
  collect=0
  if [ "$COLLECT_CN" = "1" ]; then collect=1
  elif [ "$COLLECT_CN" = "0" ]; then collect=0
  elif [ "$cn_traded" = "1" ]; then collect=1
  fi
  if [ -n "$ARG_IV" ]; then tier=manual; iv="$ARG_IV"
  elif [ "$SPEED" = "active" ]; then tier=active; iv="$CFG_ACTIVE"
  elif [ "$SPEED" = "calm" ]; then tier=calm; iv="$CFG_CALM"
  elif [ "$SPEED" != "auto" ]; then tier=manual; iv="$SPEED"
  elif [ "$ops" = "1" ]; then tier=active; iv="$CFG_ACTIVE"
  else tier=calm; iv="$CFG_CALM"; fi
  # 采 A股 → 强制平稳档（护东财）；手动指定间隔时不覆盖。
  # 2>/dev/null：iv 非数字时 -lt 会报错，静默跳过即可（不做算术比较）
  if [ "$collect" = "1" ] && [ "$tier" != "manual" ]; then
    if [ "$iv" -lt "$CFG_CALM" ] 2>/dev/null; then tier=calm; iv="$CFG_CALM"; fi
  fi
  echo "$tier $iv $collect"
}

# ---- 轮次相位（V1.9.10，师傅要求）----
#   「在下一轮正在采集的时候，把主播名称后面的 ON AIR 切换展示为：搜索中」。
#   本脚本把「本轮是否在忙」写成 data/loop_phase.json，观看服务经 /__state__.json
#   转给页面；页面据此把主播徽标在「ON AIR / 搜索中」之间切换。
#   busy = 本轮正在采集/生成；idle = 一轮已完成、正在 sleep 等下一轮。
#   ⚠️ 只写文件、不走网络，失败也不影响直播主流程（set -u 下加 || true 兜底）。
set_phase() {
  printf '{"phase":"%s","ts":%s}\n' "$1" "$(date +%s)" > "$HERE/data/loop_phase.json" 2>/dev/null || true
}
set_phase idle

echo "=== 直播循环启动 $(date '+%F %T')，引擎=${ENGINE}，采A股=${COLLECT_CN} ===" >>"$LOG"

while true; do
  SEL=$(pick_round)
  TIER=$(echo "$SEL" | cut -d' ' -f1)
  IV=$(echo "$SEL" | cut -d' ' -f2)
  COLLECT=$(echo "$SEL" | cut -d' ' -f3)
  TS="$(date '+%F %T')"
  set_phase busy
  {
    echo "--- [$TS] 本轮：档位=${TIER}(${IV}s)，采A股=${COLLECT} ---"
    if [ "$COLLECT" = "1" ]; then
      echo "--- [$TS] A股全市场采集（东财→腾讯→新浪兜底） ---"
      "$PY" collect_live.py 2>&1 | tail -8
    fi
    echo "--- [$TS] 港美股行情 / 时段 / 外媒快讯 ---"
    # V1.9.21：run_to 超时闸——markets 任一步卡死（源抖动/代理挂）都不会拖垮整轮循环
    "$PY" run_to.py 180 "$PY" markets.py 2>&1 | tail -12
    # ---- V1.9.0 eulerpool 个股新闻 + 经济数据官方发布日历 ----
    # 位置很关键：必须跑在 markets.py **之后**（要往它写的 news 池里并）、
    # narrate.py **之前**（解说词要用到这些内容）。
    # 优雅降级：key 未配/ 代理不通 / API 挂 → 本步静默跳过，快讯池照旧用RSS。
    # ⚠️ 代理与 key 都从环境/配置读，不落明文。
    # V1.9.22：eulerpool 已恢复（经 7897 出口代理可用）。脚本内部已改「线程+join 硬超时」
    #   根治代理隧道不超时导致的挂起；此处 run_to 90s 作双保险（任何情况下都不拖垮整轮）。
    if [ -n "${EULERPOOL_API_KEY:-}" ]; then
      echo "--- [$TS] eulerpool 个股新闻 / 经济日历 ---"
      "$PY" run_to.py 90 "$PY" eulerpool_src.py --patch 2>&1 | tail -6
    else
      echo "--- [$TS] eulerpool 跳过（未设 EULERPOOL_API_KEY） ---"
    fi
    # V1.9.34：RRG 跨资产轮动看板（只读 yangxiaa.cc/rrg/，落盘 data/rrg_state.json）
    #   供统计台「资产象限」瓦片 + 解说背景态消费。非致命：抓取失败整轮跳过，不拖垮主流程。
    echo "--- [$TS] RRG 跨资产轮动看板（只读） ---"
    "$PY" run_to.py 25 "$PY" collect_rrg.py 2>&1 | tail -4 || echo "[!] collect_rrg 失败，本轮跳过（不影响主流程）"
    echo "--- [$TS] 生成解说词（${ENGINE} 引擎） ---"
    if [ "$ENGINE" = "api" ]; then
      # V1.9.21：narrate API 偶发长连/限流，超时即跳过本轮解说（不阻塞后续 build/下一轮）
      "$PY" run_to.py 240 "$PY" narrate.py --engine api --preset "$PRESET" 2>&1 | tail -8
    else
      "$PY" run_to.py 240 "$PY" narrate.py --engine local 2>&1 | tail -8
    fi
    echo "--- [$TS] 重建 live.html（刷新档位=${TIER} ${IV}s） ---"
    # ⚠️ 引擎=local 时 build_live.py 会拒掉 local-synth 产物（exit 2，页面不更新）——
    #   这是有意的：师傅要求「不要任何合成的内容」。想跑本地模板必须显式 --allow-local。
    if [ "$ENGINE" = "api" ]; then
      "$PY" build_live.py --tier "$TIER" --refresh "$IV" 2>&1 | tail -3
    else
      echo "[!] ENGINE=local：build_live.py 默认拒收本地模板产物，页面将不更新"
      echo "    （要放行请手工执行：build_live.py --allow-local）"
    fi
    echo "--- [$TS] 一轮完成 ---"
  } >>"$LOG" 2>&1
  set_phase idle
  sleep "$IV"
done
