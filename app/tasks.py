#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.tasks — 任务管理器（UI 的任务调度与进度路由）。

调度规则（设计定稿）：平台=浏览器实例、同平台任务排队、跨平台并行。
当前仅抖音平台 → 同平台排队保证任意时刻至多一个业务线程在跑，
进度 sink 直接路由到活跃任务（多平台并行时升级为每线程独立 sink）。

取消实现：TaskCancelled 继承 BaseException（同 KeyboardInterrupt），
借日志/事件发射点抛出——穿透业务层的 except Exception，各层 finally
的监听器摘除/半成品清理照常执行。
"""
import threading
import time
from dataclasses import dataclass, field

from core import reporting


class TaskCancelled(BaseException):
    """UI 请求取消（借发射点抛出，见模块 docstring）。"""


_STATUS_TEXT = {"queued": "◌排队中", "running": "●运行中", "wait": "⏳等待扫码/验证",
                "cancelling": "◌取消中…",
                "done": "✔完成", "error": "✗出错", "cancelled": "已取消"}


@dataclass
class Task:
    id: int
    mode: str                    # jx | clips
    platform: str = "douyin"
    keyword: str = ""
    done: int = 0
    total: int = 0
    unit: str = "部"
    now: str = "排队中…"
    status: str = "queued"       # queued|running|wait|done|error|cancelled
    error: str = ""
    logs: list = field(default_factory=list)

    def snapshot(self) -> dict:
        with_logs = self.logs[-8:]
        return {"id": self.id, "mode": self.mode, "platform": self.platform,
                "keyword": self.keyword, "done": self.done,
                "total": self.total, "unit": self.unit, "now": self.now,
                "status": self.status, "statusText": _STATUS_TEXT.get(
                    self.status, self.status),
                "error": self.error, "logs": with_logs}


class TaskManager:
    def __init__(self):
        self._tasks: list[Task] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._cancel: set[int] = set()

    # ---------- 查询 ----------

    def list(self) -> list:
        with self._lock:
            return [t.snapshot() for t in self._tasks]

    # ---------- 发起 ----------

    def start(self, mode: str, cfg, keyword: str) -> int:
        with self._lock:
            self._seq += 1
            t = Task(id=self._seq, mode=mode, keyword=keyword,
                     total=cfg.limit, unit="部" if mode == "jx" else "条")
            self._tasks.append(t)
        threading.Thread(target=self._run, args=(t, cfg), daemon=True,
                         name=f"task-{self._seq}-{mode}").start()
        return t.id

    def cancel(self, tid: int) -> None:
        """请求取消。运行中任务在下一个检查点(≤2s)退出; 排队任务立即出队。"""
        self._cancel.add(tid)
        with self._lock:
            for t in self._tasks:
                if t.id == tid and t.status == "queued":
                    t.status = "cancelled"
                    t.now = "已取消"
                elif t.id == tid and t.status in ("running", "wait"):
                    t.status = "cancelling"
                    t.now = "取消中(等待当前操作退出)…"

    # ---------- 内部 ----------

    def _busy(self, platform: str) -> bool:
        # cancelling 仍占浏览器(线程未退出), 下一个任务必须继续等
        return any(t.platform == platform
                   and t.status in ("running", "wait", "cancelling")
                   for t in self._tasks)

    def _cancelled(self, tid: int) -> bool:
        return tid in self._cancel

    def _push_log(self, task: Task, msg: str, err: bool = False) -> None:
        with self._lock:
            task.logs.append(("ERR " if err else "") + msg)
            if len(task.logs) > 300:
                del task.logs[:100]

    def _run(self, task: Task, cfg) -> None:
        # 同平台排队（profile 锁 + 风控保护，见模块 docstring）
        while self._busy(task.platform):
            if self._cancelled(task.id):
                task.status = "cancelled"
                task.now = "已取消"
                return
            time.sleep(1.0)
        task.status = "running"
        task.now = "启动搜索浏览器…"

        def on_log(msg, err=False):
            if self._cancelled(task.id):
                raise TaskCancelled()
            if task.status == "wait":       # urgent 后有普通日志 → 验证已过
                task.status = "running"
            self._push_log(task, msg, err)

        def on_urgent(msg):
            task.status = "wait"            # 滑块/扫码，等人在场
            self._push_log(task, msg)

        def on_event(ev):
            if self._cancelled(task.id):
                raise TaskCancelled()
            if ev.get("type") == "progress":
                task.done = ev.get("done", task.done)
                task.total = ev.get("total", task.total)
                task.unit = ev.get("unit", task.unit)
                task.now = ev.get("now", task.now)
            elif ev.get("type") == "done":
                task.status = "done"
                task.now = f"已完成 ✔ ({task.done}/{task.total} {task.unit})"
            print(f"[task-{task.id}] {ev}", flush=True)  # 事件留痕

        def on_checkpoint():
            """取消检查点: 长等待循环每圈调用, 请求取消即抛出中断。"""
            if self._cancelled(task.id):
                raise TaskCancelled()

        reporting.set_sink(on_log)
        reporting.set_urgent_sink(on_urgent)
        reporting.set_event_sink(on_event)
        reporting.set_checkpoint_sink(on_checkpoint)
        try:
            from app import service
            if task.mode == "jx":
                service.run_jx(cfg)
            else:
                service.run_clips(cfg)
            if task.status != "done":
                task.status = "done"
                task.now = f"已结束 ({task.done}/{task.total} {task.unit})"
        except TaskCancelled:
            task.status = "cancelled"
            task.now = "已取消"
        except KeyboardInterrupt:
            task.status = "cancelled"
            task.now = "已中断"
        except Exception as e:  # noqa: BLE001 - 任务失败落到状态展示
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            task.now = f"出错: {e}"
            # 落盘留痕(UI 任务的 sink 输出只在内存, 崩溃必须有文件痕迹)
            import traceback
            print(f"[task-{task.id} 出错] {task.error}\n"
                  f"{traceback.format_exc()}", flush=True)
        finally:
            reporting.set_sink(None)
            reporting.set_urgent_sink(None)
            reporting.set_event_sink(None)
            reporting.set_checkpoint_sink(None)
