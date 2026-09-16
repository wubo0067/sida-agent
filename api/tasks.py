#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build 后台任务注册表：提交即返回 task_id，进度事件可轮询 / SSE 订阅。

模型（与确认过的方案一致）：
- ``POST /build`` 校验通过后登记任务并在专属单线程执行器里开跑，立即返回 task_id；
- 同一时刻只允许一个 running 任务：graph_db 是单份内存图整体落盘，并发写会
  互相覆盖 —— 第二个提交直接 409（由路由层调用 ``active_task()`` 判断）；
- 进度事件来自 ingestion / pdf_processor 的 progress 回调（线程安全地写入
  任务缓冲）：每个任务保留最近 _EVENT_CAP 条，SSE 订阅者从上次位置继续拉，
  断线重连不丢大事件（环形缓冲足够覆盖一次 build 的关键节点事件量）；
- 任务状态为内存版：服务重启即丢历史，但 build 的逐页/逐子块缓存都在磁盘，
  重发同参数请求即秒级续跑（0 成本命中缓存）。
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from logger import get_logger

log = get_logger()

# 每任务保留的最近进度事件条数（终态 + 里程碑事件远小于此值）
_EVENT_CAP = 2000

Status = str  # queued / running / done / failed / cancelled


class BuildTask:
    """单个 build 任务：参数快照 + 状态 + 进度事件环形缓冲。"""

    def __init__(self, params: Dict[str, Any]) -> None:
        self.id = "b-" + uuid.uuid4().hex[:12]
        self.params = params
        self.status: Status = "queued"
        self.error: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None   # 终态统计（页数/块数/token）
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=_EVENT_CAP)
        self._seq = 0
        # 有新事件 / 状态变化时 set，唤醒 SSE 订阅者（自动重置的 Event）
        self.wakeup = threading.Event()
        self.lock = threading.Lock()

    def add_event(self, ev: Dict[str, Any]) -> int:
        """追加进度事件并唤醒订阅者，返回事件序号（SSE 续传游标）。"""
        with self.lock:
            self._seq += 1
            ev = {"seq": self._seq,
                  "ts": datetime.now().isoformat(timespec="seconds"), **ev}
            self.events.append(ev)
        self.wakeup.set()
        return self._seq

    def snapshot(self) -> Dict[str, Any]:
        """任务概要（不含事件流），供 JSON 端点。"""
        with self.lock:
            return {
                "task_id": self.id,
                "status": self.status,
                "params": self.params,
                "error": self.error,
                "result": self.result,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_event_seq": self._seq,
            }

    def events_since(self, seq: int) -> List[Dict[str, Any]]:
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def terminal(self) -> bool:
        return self.status in ("done", "failed", "cancelled")


class BuildRegistry:
    """任务注册表 + 单执行线程（同时最多 1 个 running，后来者 409 由路由拒绝）。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, BuildTask] = {}
        self._order: List[str] = []          # 提交顺序
        self._lock = threading.Lock()
        # 专属单线程执行器：build 彼此串行；与请求线程池隔离，避免互相挤占
        self._runner = threading.Thread(target=self._queue_loop,
                                        name="sida-build", daemon=True)
        self._pending: List[BuildTask] = []
        self._wake = threading.Event()
        self._stop = False
        self._current: Optional[BuildTask] = None
        self._runner.start()

    # ---- 查询 ----
    def get(self, task_id: str) -> Optional[BuildTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._tasks[t].snapshot() for t in self._order]

    def active_task(self) -> Optional[BuildTask]:
        """当前占用双库写位的任务（running，或排队中尚未跑完的）。

        路由层据此对并发提交返回 409。
        """
        with self._lock:
            if self._current is not None and not self._current.terminal():
                return self._current
            for t in self._pending:
                if not t.terminal():
                    return t
        return None

    # ---- 提交 / 执行 ----
    def submit(self, params: Dict[str, Any],
               work: Any) -> BuildTask:
        """登记任务并入队执行。work(task) 在 build 线程里跑，负责推进状态。"""
        task = BuildTask(params)
        task._work = work  # type: ignore[attr-defined]
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._pending.append(task)
        self._wake.set()
        log.info("[api] build 任务已登记: %s (%s)", task.id, params.get("pdf"))
        return task

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    def _queue_loop(self) -> None:
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            while self._pending and not self._stop:
                task = self._pending[0]
                with self._lock:
                    self._current = task
                task.status = "running"
                task.started_at = datetime.now().isoformat(timespec="seconds")
                task.add_event({"stage": "task", "event": "task_started"})
                try:
                    task._work(task)  # type: ignore[attr-defined]
                    if not task.terminal():
                        task.status = "done"
                except Exception as exc:  # noqa: BLE001 - 任务异常转终态
                    log.exception("[api] build 任务 %s 失败", task.id)
                    task.status = "failed"
                    task.error = f"{type(exc).__name__}: {exc}"
                    task.add_event({"stage": "task", "event": "task_failed",
                                    "error": task.error})
                finally:
                    task.finished_at = datetime.now().isoformat(timespec="seconds")
                    task.add_event({"stage": "task", "event": "task_finished",
                                    "status": task.status})
                    task.wakeup.set()
                    with self._lock:
                        self._pending.pop(0)
                        self._current = None
