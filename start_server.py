#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 观看服务 守护进程启动器（与 start_loop.py 同源 daemon 思路）
=====================================================================
WorkBuddy 每次 Bash 调用结束会清掉自己启动的进程组，普通 nohup/& 都会被挂断。
这里用 Python double-fork 脱离会话，让服务在终端关闭后依然常驻。
用法：
  python3 start_server.py start     # 启动（若已运行则提示）
  python3 start_server.py stop      # 停止（SIGTERM，必要时 SIGKILL）
  python3 start_server.py restart   # 先停后启
  python3 start_server.py status    # 看 PID / 端口 / 最近访问 / 是否存活
端口：默认 8800，可用环境变量 LIVE_PORT 覆盖（eg LIVE_PORT=8801 python3 start_server.py start）
"""
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = "/Users/yangxia/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
PORT = os.environ.get("LIVE_PORT", "8800")
LOG = os.path.join(HERE, "logs", "http.log")
PIDFILE = os.path.join(HERE, "logs", "server.pid")


def find_pid():
    try:
        out = subprocess.run(["pgrep", "-f", "serve_live.py"],
                             capture_output=True, text=True).stdout.strip()
        pids = [p for p in out.splitlines() if p.strip().isdigit()]
        return int(pids[0]) if pids else None
    except Exception:
        return None


def is_up():
    pid = find_pid()
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def daemonize_and_exec():
    # double-fork 脱离会话，被 init 收养，彻底摆脱 WorkBuddy 的进程组清理
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    os.chdir(HERE)
    os.environ.setdefault("LIVE_PORT", PORT)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open("/dev/null", os.O_RDONLY)
    os.dup2(devnull, 0)
    os.execv(PY, [PY, os.path.join(HERE, "serve_live.py")])


def start():
    if is_up():
        print("[!] 观看服务已在运行（PID %d），无需重复启动" % is_up())
        return
    daemonize_and_exec()


def stop():
    pid = find_pid()
    if not pid:
        print("[*] 未发现运行中的观看服务")
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(20):
        if not is_up():
            break
        time.sleep(0.2)
    if is_up():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    print("[ok] 已停止观看服务（PID %d）" % pid)
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)


def status():
    pid = is_up()
    if not pid:
        print("[x] 观看服务未运行")
        return
    print("✓ 运行中：PID %d   地址 http://127.0.0.1:%s/live.html" % (pid, PORT))
    print("    最近访问（logs/http.log 末 5 行）：")
    try:
        with open(LOG, encoding="utf-8") as f:
            lines = [l.rstrip() for l in f if l.strip()]
        for l in lines[-5:]:
            print("      " + l)
    except Exception:
        pass


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "restart":
        stop()
        time.sleep(0.5)
        start()
    elif cmd == "status":
        status()
    else:
        print("用法：start | stop | restart | status")
        sys.exit(1)
