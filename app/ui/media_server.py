#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.ui.media_server — 本地媒体服务（视频预览用）。

为什么不用 file:// 直播: WebView2 对 file→file 子资源有安全限制,
中文/特殊字符路径还易出编码问题; 127.0.0.1 的 HTTP 流最可靠,
且支持 Range(拖动进度条)。

仅绑定 127.0.0.1 随机端口(不触发防火墙提示), 只读 downloads 内容。
"""
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

_port = None
_lock = threading.Lock()


class _MediaHandler(BaseHTTPRequestHandler):
    root: Path = Path(".")          # 启动时注入(APP_ROOT)
    prefix = "/video/"

    def do_GET(self):
        if not self.path.startswith(self.prefix):
            self.send_error(404)
            return
        rel = unquote(self.path[len(self.prefix):]).lstrip("/")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root.resolve())   # 防 ../ 越界
        except ValueError:
            self.send_error(403)
            return
        if not p.is_file():
            self.send_error(404)
            return
        size = p.stat().st_size
        rng = self.headers.get("Range")
        ctype = "video/mp4"
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1) or 0)
            end = min(int(m.group(2) or size - 1), size - 1)
            if start >= size:
                self.send_error(416)
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", ctype)
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionAbortedError, ConnectionResetError):
                        return
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Type", ctype)
            self.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def log_message(self, *args):   # 静默(无控制台可写)
        pass


def start(root: Path) -> int:
    """启动本地媒体服务(root=可访问目录)，返回端口；幂等。"""
    global _port
    with _lock:
        if _port is not None:
            return _port
        _MediaHandler.root = Path(root).resolve()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
        _port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="media-server").start()
        return _port
