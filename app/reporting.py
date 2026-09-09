#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.reporting — 报告器安装（CLI 与 UI 共用的输出接缝）。

core.reporting 定义了 log/urgent 双通道与 sink 机制；本模块负责把
"具体界面的报告器"装进去。CLI 用 ConsoleReporter（行为=直通控制台），
未来 UI 换实现即可，业务代码零改动。
"""
import sys

from core import reporting


class ConsoleReporter:
    """控制台报告器（CLI 默认）。"""

    def log(self, msg: str, err: bool = False) -> None:
        print(msg, file=sys.stderr if err else sys.stdout)

    def urgent(self, msg: str) -> None:
        # 人必须实时看的提示（验证码/扫码），强制 flush
        print(msg, flush=True)


def install(reporter=None) -> None:
    """把报告器装为全局默认；None = 恢复默认直 print。

    任务线程可用 core.reporting.set_sink 系列做线程本地覆盖（多任务
    并行时各自路由，互不串线）。
    """
    if reporter is None:
        reporting.set_default_sink(None)
        reporting.set_default_urgent(None)
        reporting.set_default_event(None)
        return
    reporting.set_default_sink(reporter.log)
    reporting.set_default_urgent(reporter.urgent)
    if hasattr(reporter, "event"):
        reporting.set_default_event(reporter.event)
