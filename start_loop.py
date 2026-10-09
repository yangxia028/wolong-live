#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股市解说直播 · 循环启动器（V1.9.3）

为什么需要这个文件：WorkBuddy 每次 Bash 调用结束会**清理自己启动的整个进程组**，
所以 `nohup ./run_live.sh &` / `trap '' HUP` / `nohup & disown` 全都会被打断
（实测报 `Hangup: 1`，while 循环只活几秒）。**double-fork（经典 daemonize）**
才能彻底脱离当前进程组、被 init 收养。

用法：
  启动：  python3 start_loop.py
  状态：  python3 start_loop.py status
  停止：  python3 start_loop.py stop
  重启：  python3 start_loop.py restart

停止也可以直接： pkill -f "run_live.sh"
（run_live.sh 只 `trap '' HUP`，TERM 仍可杀，所以 pkill 有效）
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PID_PATTERN = "run_live.sh"
LOG = os.path.join(HERE, "logs", "live_loop.log")

# API key 走环境变量（config.json 里只存变量名，不落明文）
ENV = {
    "EULERPOOL_API_KEY": os.environ.get(
        "EULERPOOL_API_KEY", "eu_prod_1791287355164_r9cro0w0pf8"),
    "ENGINE": "api",
    "PRESET": "agnes",
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME": os.environ.get("HOME", "/Users/yangxia"),
    "LANG": "zh_CN.UTF-8",
    # V1.9.18：显式把出口代理传入 daemon。否则 run_live.sh 子进程无 HTTP(S)_PROXY，
    # urllib 直连 agnes/eulerpool 会 SSL UNEXPECTED_EOF_WHILE_READING（实测 13:57 起解说流卡死）。
    "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
    "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
    "http_proxy": os.environ.get("HTTPS_PROXY", ""),
    "https_proxy": os.environ.get("HTTPS_PROXY", ""),
}

DAEMON = r'''
import os, sys
pid = os.fork()
if pid > 0:
    sys.exit(0)                 # 第一父：立刻退出
os.setsid()                     # 新会话 → 不再是前台进程组的成员
pid2 = os.fork()
if pid2 > 0:
    os._exit(0)                 # 第二父：立刻退出 → 子进程被 init 收养
os.chdir(%(here)r)
for k, v in %(env)r.items():
    os.environ[k] = v
fd = os.open(%(log)r, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
# ⚠️ stdin 必须给 /dev/null（只读）：若给日志 fd（O_WRONLY），
#    后续每个 python 都会报 init_sys_streams: Bad file descriptor 而崩。
devnull = os.open("/dev/null", os.O_RDONLY)
os.dup2(devnull, 0)
os.execv("/bin/bash", ["bash", "run_live.sh"])
'''


def _py():
    return "/Users/yangxia/.workbuddy/binaries/python/versions/3.13.12/bin/python3"


def running_pids():
    try:
        out = subprocess.run(["pgrep", "-f", PID_PATTERN],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                                            # noqa: BLE001
        return []
    return [x for x in out.split() if x.strip()]


def rounds_done():
    """数已完成轮数 —— 不能只看"进程还在"，断点就在轮次边界。"""
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return f.read().count("一轮完成")
    except Exception:                                            # noqa: BLE001
        return 0


def start():
    if running_pids():
        print("已在运行：PID %s（已完成 %d 轮）" % (",".join(running_pids()), rounds_done()))
        return
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    src = DAEMON % {"here": HERE, "env": ENV, "log": LOG}
    subprocess.run([_py(), "-c", src], check=False)
    time.sleep(3)
    pids = running_pids()
    if pids:
        print("✓ 循环已启动：PID %s" % ",".join(pids))
        print("  日志：%s" % LOG)
        print("  停止：python3 start_loop.py stop  或  pkill -f run_live.sh")
    else:
        print("✗ 启动失败，先看 %s 里的报错" % LOG)


def _clear_lock():
    """V1.9.15：清掉 run_live.sh 的单实例锁（双保险）。

    为什么需要：run_live.sh 自带 mkdir 原子锁防并发双开，正常退出（含 Ctrl-C/
    pkill）时会在 EXIT trap 里自己删锁。但若上一轮是被 `kill -9` 打断的（EXIT
    trap 不执行），锁目录会残留 —— 虽然新循环能靠「pid 已死 → 僵尸锁接管」
    自愈，这里再显式清一次，避免任何边界情况把新循环挡在门外。
    """
    import shutil as _sh
    _sh.rmtree(os.path.join(HERE, ".loop.lock"), ignore_errors=True)


def stop():
    pids = running_pids()
    if not pids:
        print("未在运行")
        _clear_lock()
        return
    subprocess.run(["pkill", "-f", PID_PATTERN], check=False)
    time.sleep(2)
    left = running_pids()
    if left:
        subprocess.run(["pkill", "-9", "-f", PID_PATTERN], check=False)
        time.sleep(1)
    _clear_lock()
    print("✓ 已停止（原 PID %s）" % ",".join(pids))


def status():
    pids = running_pids()
    if not pids:
        print("✗ 未运行")
        return
    print("✓ 运行中：PID %s" % ",".join(pids))
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f.readlines() if x.strip()]
    except Exception:                                            # noqa: BLE001
        lines = []
    print("  已完成轮数：%d" % rounds_done())
    for l in lines[-6:]:
        print("   | %s" % l[:130])
    bad = sum(1 for x in lines if any(k in x for k in ("Fatal", "Traceback", "Hangup")))
    print("  报错行数：%d%s" % (bad, "（需关注）" if bad else " ✓"))


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    {"start": start, "stop": stop, "restart": lambda: (stop(), start()),
     "status": status}.get(cmd, start)()
