#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.reporting — 输出通道：log/urgent/event 三通道，为 UI 预留 sink 接缝。

- log(): 普通进度输出（print 替代品）
- urgent(): 必须实时人看的提示（滑块/扫码）——独立通道不进缓冲
- event(): 结构化进度事件（{type,done,total,...}），UI 任务列表的数据源

sink 存储分两层：线程本地（任务线程 set_sink 覆盖，多任务并行互不串）
> 全局默认（UI/CLI 用 set_default_sink 安装）。都没有时回落直 print。

CLI 态不安装任何 sink，行为与 print 一致。
"""
import sys
import threading

_tls = threading.local()                 # 任务线程覆盖层
_default = {"log": None, "urgent": None, "event": None,
            "checkpoint": None}  # 全局默认层


# ---------- 安装接口 ----------

def set_sink(fn) -> None:
    """当前线程的 log sink（任务线程用；None 回落全局默认）。"""
    _tls.log = fn


def set_urgent_sink(fn) -> None:
    """当前线程的 urgent sink。"""
    _tls.urgent = fn


def set_event_sink(fn) -> None:
    """当前线程的结构化事件 sink。"""
    _tls.event = fn


def set_checkpoint_sink(fn) -> None:
    """当前线程的取消检查点（长等待循环每圈调用，UI 取消即时生效）。"""
    _tls.checkpoint = fn


def set_default_sink(fn) -> None:
    """全局默认 log sink（UI/CLI 客户端安装用）。"""
    _default["log"] = fn


def set_default_urgent(fn) -> None:
    _default["urgent"] = fn


def set_default_event(fn) -> None:
    _default["event"] = fn


def _pick(kind: str):
    return getattr(_tls, kind, None) or _default[kind]


# ---------- 输出接口 ----------

def log(msg="", *, err=False, flush=False):
    """普通进度输出。err=True 走 stderr 语义（收集器自行决定呈现）。"""
    fn = _pick("log")
    if fn is not None:
        fn(msg, err)
        return
    print(msg, file=sys.stderr if err else sys.stdout, flush=flush)


def urgent(msg=""):
    """紧急人机交互提示（>>> 请完成滑块验证 <<< 这类），保证实时可见。"""
    fn = _pick("urgent")
    if fn is not None:
        fn(msg)
        return
    print(msg, flush=True)


def event(ev: dict):
    """结构化进度事件。业务代码在关键节点发，UI 消费；CLI 无 sink 时忽略。

    约定字段: {"type": "progress"|"status"|"done", ...}
    progress: {"done": int, "total": int, "unit": str, "now": str}
    """
    fn = _pick("event")
    if fn is not None:
        fn(ev)


def checkpoint():
    """取消检查点：长等待循环每圈调用一次（约 2s 粒度）。

    无 sink 时零开销直过；UI 注册 sink 后，请求取消时在此抛出
    TaskCancelled（BaseException，穿透业务层 except Exception）。
    """
    fn = _pick("checkpoint")
    if fn is not None:
        fn()
