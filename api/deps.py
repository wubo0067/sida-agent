#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 运行时共享资源：双库单例、写锁、线程池、SSE 桥接。

FastAPI 端点全部是 async，但业务内核（Chroma / NetworkX / SqliteSaver /
LLM 调用）是同步阻塞的，因此：
- 阻塞调用一律丢进 ``runtime.executor`` 线程池执行，event loop 不被卡住；
- 双库（vector_db / graph_db）在进程启动时各加载一份，跨请求共享，与 CLI
  「同一份双库累积全科知识」的语义一致；
- graph_db 是单份内存图 + 整体落盘，写侧（build）用 ``graph_lock`` 串行化，
  避免并发写互相覆盖（并发提交 build 在路由层直接 409 拒绝）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Iterator, Optional

from logger import get_logger
from storage.graph_store import ScienceGraphStore
from storage.vector_store import get_vector_store

log = get_logger()


class Runtime:
    """进程级共享运行时：双库单例 + 写锁 + 线程池。

    由 lifespan 在应用启动时构造并挂到 ``app.state.runtime``；模块级
    ``runtime`` 同时保存一份引用，方便后台任务/工具函数不经 Request 直接取用。
    """

    def __init__(self) -> None:
        # 双库：与 CLI 同源，跨请求共享同一份持久化知识库
        self.vector_db = get_vector_store()
        self.graph_db = ScienceGraphStore.load()
        # 图谱写锁：串行化「写图 + save」临界区（build 侧）
        self.graph_lock = threading.RLock()
        # 阻塞任务线程池：ask/chat/build 全部在此执行，不占用 event loop
        self.executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="sida-api")
        # build 任务注册表（延迟导入避免循环依赖，见 tasks.BuildRegistry）
        self.builds: Optional[Any] = None
        self._shutdown = False

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        # 不等待在跑的 build（其内部逐子块已落盘缓存，重启后重发即续跑）
        self.executor.shutdown(wait=False, cancel_futures=True)


# 模块级引用：lifespan 里赋值，供不便传 Request 的地方（后台线程）取用
runtime: Optional[Runtime] = None


def set_runtime(rt: Optional[Runtime]) -> None:
    """设置/清空模块级 runtime 引用（lifespan 调用）。"""
    global runtime
    runtime = rt


def require_runtime() -> Runtime:
    """取当前 runtime；未初始化（应用未启动）时抛错，避免静默 None。"""
    if runtime is None:
        raise RuntimeError("API runtime 未初始化：请通过 uvicorn/main.py serve 启动应用")
    return runtime


# ---- 同步 → 线程 / SSE 桥接工具 -------------------------------------------

_SENTINEL = object()


async def run_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把同步阻塞函数丢进 runtime 线程池执行并等待结果（JSON 端点用）。"""
    rt = require_runtime()
    loop = asyncio.get_running_loop()
    if kwargs:
        call: Callable[[], Any] = lambda: fn(*args, **kwargs)  # noqa: E731
    else:
        call = lambda: fn(*args)  # noqa: E731
    return await loop.run_in_executor(rt.executor, call)


def _drain_producer(producer: Callable[[], Iterator[dict]],
                    loop: asyncio.AbstractEventLoop,
                    aq: "asyncio.Queue[Any]") -> None:
    """在工作线程里跑同步生成器，逐事件经 call_soon_threadsafe 投回 event loop。

    用 asyncio.Queue + call_soon_threadsafe 而非阻塞 queue.get，避免每条流
    额外占用一个线程池 worker（只有 producer 本身占一个）。producer 抛出的
    异常转成 error 事件，最后放哨兵，保证消费端总能收尾。
    """
    def _put(item: Any) -> None:
        loop.call_soon_threadsafe(aq.put_nowait, item)

    try:
        for event in producer():
            _put(event)
    except Exception as exc:  # noqa: BLE001 - 流式端点兜底：异常转事件
        log.exception("[api] 流式生成器异常")
        _put({"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        _put(_SENTINEL)


async def sse_from_producer(
    producer: Callable[[], Iterator[dict]],
) -> AsyncIterator[str]:
    """把「同步生成器产出 dict 事件」桥接成 SSE 文本流（async 生成器）。

    生成器在 runtime 线程池里运行，逐事件经 asyncio.Queue 回传给 event loop，
    每条编码成 ``data: <json>\\n\\n``；生成器结束后收到哨兵再补一条
    ``event: end`` 收尾。客户端断开时 async 生成器被取消，producer 线程会
    在下一个事件投递时发现 loop 状态——为不阻塞，producer 结束后自然退出。
    """
    rt = require_runtime()
    loop = asyncio.get_running_loop()
    aq: "asyncio.Queue[Any]" = asyncio.Queue()
    loop.run_in_executor(rt.executor, _drain_producer, producer, loop, aq)
    while True:
        item = await aq.get()
        if item is _SENTINEL:
            break
        yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
    yield "event: end\ndata: {}\n\n"
