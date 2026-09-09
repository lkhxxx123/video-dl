#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.ui.gui — pywebview 窗口入口（run.py ui）。

单进程：GUI 循环在主线程；抓取任务在 daemon 线程（TaskManager 调度），
进度经 core.reporting 线程本地 sink 路由回任务对象，前端 800ms 轮询。
"""
import webview

from app.tasks import TaskManager
from app.ui.bridge import Bridge


def show() -> None:
    from core import paths
    from app.ui import media_server
    port = media_server.start(paths.APP_ROOT)
    tm = TaskManager()
    bridge = Bridge(tm)
    bridge.media_base = f"http://127.0.0.1:{port}"
    page = str((__import__("pathlib").Path(__file__).parent
                / "index.html").resolve())
    webview.create_window(
        "视频下载器", page, js_api=bridge,
        width=1300, height=840, min_size=(1100, 700),
        background_color="#121419")
    webview.start()
