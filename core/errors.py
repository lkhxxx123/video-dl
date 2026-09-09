#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.errors — 跨层异常定义。

core 禁止反向依赖 platforms/modes，因此被多层共用的异常先下沉到此
（原 douyin_search.SearchError：浏览器层抛出、三个模式层捕获）。
"""


class SearchError(Exception):
    """搜索/浏览器流程失败（登录超时、验证码未通过、接口未拦截到等）。"""
