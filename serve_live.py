#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股市解说直播 · 本地观看服务（127.0.0.1 常驻）
=====================================================================
职责：
  * 提供 http://127.0.0.1:8800/live.html —— 浏览器持续观看「直播页」的入口
  * 提供 /__heartbeat__.json 端点：返回 live.html 的 mtime / size，
    页面据此判断「是否已有新一轮构建完成」再自动刷新，避免定时硬刷打断观看
  * 仅绑定 127.0.0.1（本机回环），不对外网暴露
  * "/" 默认 302 跳转到 /live.html
用法：python3 serve_live.py [port]     端口走 $LIVE_PORT 或形参，默认 8800
"""
import http.server
import json
import os
import socketserver
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LIVE_PORT", "8800"))
LIVE = os.path.join(HERE, "live.html")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _heartbeat(self):
        try:
            mtime = os.path.getmtime(LIVE)
        except OSError:
            mtime = 0
        try:
            size = os.path.getsize(LIVE)
        except OSError:
            size = 0
        self._send_json({"mtime": mtime, "size": size})

    def _data(self):
        """V1.9.8：纯数据 JSON（data/live_data.json）—— 页面轮询它、比对 build_mtime，
        仅在真正新一轮时局部更新解说流，**不再整页重载打断直播**。"""
        p = os.path.join(HERE, "data", "live_data.json")
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state(self):
        """V1.9.10：轮次相位 —— run_live.sh 每轮开头写 busy、轮末写 idle 到
        data/loop_phase.json，页面据此把主播徽标在「ON AIR / 搜索中」之间切换。
        ⚠️ 加超时兜底：文件缺失或 ts 距今超过 10 分钟（循环被 kill 等情况），
           一律回 idle，免得徽标永久卡在「搜索中」。"""
        p = os.path.join(HERE, "data", "loop_phase.json")
        phase, ts = "idle", 0
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            phase = str(d.get("phase") or "idle")
            ts = float(d.get("ts") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        if phase == "busy" and (time.time() - ts) > 600:
            phase = "idle"
        self._send_json({"phase": phase, "ts": ts})

    def _session(self):
        """V1.9.16：**实时**三市场时段状态（导播台徽标 + 24h 时段条）。

        徽标回答的是「市场现在开不开」——时钟问题，不该等 300s 一轮的 build：
        实测 2026-10-08 13:02（下午已开盘）页面仍写「全场休息」，因为那是 12:53
        午休窗口建的页。这里直接复用 markets.live_session() 按当前钟点现算
        （纯函数、每次约 0.08ms；首次 import 约 0.26s，之后走 sys.modules），
        日级事实（holiday / traded_today）仍取自上一轮快照里的行情源报价时刻。
        失败 → 500，页面保持 build 时烙入的状态（不比原来差）。
        """
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import markets as _m
            st = _m.live_session()
        except Exception as e:                                   # noqa: BLE001
            self.send_error(500, "session unavailable: %s" % e)
            return
        self._send_json(st)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/__heartbeat__", "/__heartbeat__.json"):
            return self._heartbeat()
        if p in ("/__data__.json", "/__data__"):
            return self._data()
        if p in ("/__state__.json", "/__state__"):
            return self._state()
        if p in ("/__session__.json", "/__session__"):
            return self._session()
        if p in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/live.html")
            self.end_headers()
            return
        # 对 live.html 本体强制 no-store，保证自动刷新拿到的一定是新文件
        if p == "/live.html":
            try:
                with open(LIVE, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    srv = Server(("127.0.0.1", port), Handler)
    sys.stdout.write("live server on http://127.0.0.1:%d/ (serving %s)\n" % (port, HERE))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
