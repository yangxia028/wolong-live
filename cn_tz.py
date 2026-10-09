#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 时区锚定（V1.9.40）
=====================================================================
为什么需要这个模块（V1.9.39/1.9.40「本地通≠云端通」第 2 例的根治）：
  本项目所有「裸时间」都走进程本地时区 —— datetime.now() / time.localtime() /
  time.strftime()。本地 macOS 是 CST(UTC+8) 所以永远正确，一旦换到 CI runner
  （默认 TZ=UTC）就整体错位 8 小时：北京 17:12 被判成 A 股「盘前」，
  时间戳、cn_fresh 日期比较、交易日取值全部连带出错。

  只靠 CI 脚本里的 `export TZ=Asia/Shanghai` 是**外挂补丁**：依赖每个调用方
  自觉设置，换环境/换入口就再次踩坑。故在此做**进程级强制锚定**，
  任何脚本只要 `import cn_tz` 就与操作系统时区解耦。

用法（必须在任何时间函数调用之前 import）：
    import cn_tz  # noqa: F401  —— 仅为其副作用：锚定进程时区为北京时间

纪律：
  * 北京时间恒为 UTC+8、无夏令时 → 固定值安全（美股夏令时另由 us_shift 处理）
  * 强制覆盖而非 setdefault：CI 若被显式设为 UTC，也要能救回来
  * tzset() 在 macOS/Linux 均可用；Windows 无此调用 → try/except 兜底
"""

import os
import time

TZ_NAME = "Asia/Shanghai"

os.environ["TZ"] = TZ_NAME
try:
    time.tzset()
except AttributeError:  # Windows 无 tzset；本项目部署目标为 macOS/Linux
    pass
