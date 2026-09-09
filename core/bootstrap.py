#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.bootstrap — 进程启动统一前置（原 8 处 __main__ 同构代码合一）。"""
import os
import sys

from . import paths


def setup_stdio() -> None:
    """Windows 控制台 UTF-8 输出（中文不乱码）。

    无控制台模式（exe windowed）：sys.stdout/stderr 为 None，直接 print
    会崩——重定向到 logs/运行日志.txt 兜底（print 类输出全部落盘留痕）。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if sys.stdout is None or sys.stderr is None:
        log_dir = paths.APP_ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            lf = open(log_dir / "运行日志.txt", "a", encoding="utf-8",
                      buffering=1)
            sys.stdout = sys.stderr = lf
        except Exception:  # noqa: BLE001 - 连日志都开不了就静默丢弃
            sys.stdout = sys.stderr = open(os.devnull, "w",
                                           encoding="utf-8")


def chdir_app_root() -> None:
    """工作目录切到应用根（相对 out-dir、日志、子进程等依赖 cwd 的行为保持
    与原 run.py os.chdir(SCRIPT_DIR) 一致）。"""
    os.chdir(paths.APP_ROOT)


def setup_runtime_env() -> None:
    """冻结态(exe)：PLAYWRIGHT_BROWSERS_PATH 指向包内 Chromium。

    目录不存在时绝不设置——否则 playwright 会到不存在的路径找浏览器
    直接报错；开发态（未冻结）本函数不产生任何效果。
    """
    browsers = paths.APP_ROOT / "runtime" / "browsers"
    if getattr(sys, "frozen", False) and browsers.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
