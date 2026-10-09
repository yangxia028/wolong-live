#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股市解说直播 · 单步超时闸（V1.9.21）

沙箱里没有 GNU `timeout` 命令，且 urllib 经代理的 HTTPS CONNECT 隧道
不执行 socket 超时（eulerpool_src.py 因此无限挂起、卡死整轮循环）。
本脚本用 subprocess 给任意子命令加硬超时：超时即 SIGKILL，循环绝不被拖死。

用法（在 run_live.sh 里）：
  "$PY" run_to.py 180 "$PY" markets.py 2>&1 | tail -12
  "$PY" run_to.py  90 "$PY" eulerpool_src.py --patch 2>&1 | tail -6
  "$PY" run_to.py 240 "$PY" narrate.py --engine api --preset agnes 2>&1 | tail -8
第 1 个参数是秒数，其余为要执行的命令（argv 原样透传）。
退出码转发子进程退出码；超时退出码为 124（与 GNU timeout 一致）。
"""
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: run_to.py <seconds> <cmd> [args...]\n")
        return 2
    secs = int(sys.argv[1])
    cmd = sys.argv[2:]
    try:
        p = subprocess.Popen(cmd)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[run_to] 启动失败：%s\n" % e)
        return 1
    try:
        rc = p.wait(timeout=secs)
        return rc
    except subprocess.TimeoutExpired:
        try:
            p.kill()
            p.wait()
        except Exception:  # noqa: BLE001
            pass
        sys.stderr.write("[run_to] 超时 %ds，已终止：%s\n" % (secs, " ".join(cmd)))
        return 124


if __name__ == "__main__":
    sys.exit(main())
