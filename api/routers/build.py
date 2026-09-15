#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/build —— 建库（对应 CLI --stage build）：预估 / 提交 / 状态 / 进度流。

build 是分钟~小时级长任务，不能做成同步 HTTP 请求，采用异步任务模型：

- POST /build/estimate ：只读缓存做规模预估（0 模型调用），返回将新增的视觉/推理
  调用量，供调用方决策（等价 CLI 执行前的预估打印）；
- POST /build          ：提交任务，立即返回 task_id。confirm=false 且预估有新调用
  时不启动、直接 409（把预估亮回去）；已有未终态任务时 409（graph_db 单内存图，
  并发写会互相覆盖）；
- GET  /build/tasks            ：任务列表（含状态）；
- GET  /build/tasks/{id}       ：单任务状态/结果/错误；
- GET  /build/tasks/{id}/events：SSE 订阅进度（逐页/逐子块/终态），支持 ?since=
  游标断线续传。

任务在专属单线程执行器里串行跑（见 api.tasks），progress 回调把 ingestion /
pdf_processor 的里程碑事件写入任务缓冲，SSE 端点轮询缓冲推送。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.deps import run_blocking, sse_from_producer
from api.schemas import (BuildAccepted, BuildRequest, EstimateRequest,
                         EstimateResult, TaskList, TaskStatus)
from api.tasks import BuildTask
from logger import get_logger
from main import TokenMeter, _estimate_build
from pdf_processor import _pdf_id, extract_pdf_pages_as_markdown
from ingestion import build_knowledge_bases

log = get_logger()
router = APIRouter(tags=["build"])


def _to_estimate(est: Dict[str, Any]) -> EstimateResult:
    est = dict(est)
    est["new_calls_total"] = est["new_vision_calls"] + est["new_chunks"] * 2
    return EstimateResult(**est)


@router.post("/build/estimate", response_model=EstimateResult,
             summary="建库规模预估（0 模型调用）")
async def estimate(body: EstimateRequest) -> EstimateResult:
    try:
        est = await run_blocking(
            _estimate_build, body.pdf, body.start_page, body.end_page,
            body.subject, body.max_chars, body.max_new_calls)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_estimate(est)


@router.post("/build", response_model=BuildAccepted, summary="提交建库任务")
async def submit_build(body: BuildRequest, request: Request):
    rt = request.app.state.runtime
    # 预估（同步、只读缓存）：校验 PDF 有效性 + 决定是否需确认
    try:
        est = await run_blocking(
            _estimate_build, body.pdf, body.start_page, body.end_page,
            body.subject, body.max_chars, body.max_new_calls)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_calls = est["new_vision_calls"] + est["new_chunks"]
    if not body.confirm and new_calls > 0:
        raise HTTPException(
            status_code=409,
            detail={"message": "本次将产生新模型调用，需 confirm=true 放行",
                    "estimate": _to_estimate(est).model_dump()})

    active = rt.builds.active_task()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "已有建库任务在执行，双库写入需串行，请稍后重试",
                    "active_task_id": active.id, "active_status": active.status})

    params: Dict[str, Any] = {
        "pdf": body.pdf, "start_page": body.start_page, "end_page": body.end_page,
        "subject": body.subject, "max_chars": body.max_chars,
        "max_new_calls": body.max_new_calls, "max_chunks": body.max_chunks,
        "book": body.book,
    }
    task = rt.builds.submit(params, _make_work(rt, params))
    return BuildAccepted(task_id=task.id, status=task.status,
                         estimate=_to_estimate(est))


def _make_work(rt: Any, params: Dict[str, Any]):
    """构造在 build 线程里执行的任务体：闭包捕获 runtime 与参数。"""

    def work(task: BuildTask) -> None:
        def emit(ev: Dict[str, Any]) -> None:
            task.add_event(ev)

        vision_meter = TokenMeter()
        reasoning_meter = TokenMeter()
        pdf_id = _pdf_id(Path(params["pdf"]))
        book_name = (params.get("book") or "").strip() or Path(params["pdf"]).stem

        # 1) 视觉提取：逐页进度经 progress 回调进任务缓冲
        pages_data = extract_pdf_pages_as_markdown(
            pdf_path=params["pdf"],
            start_page=params["start_page"],
            end_page=params["end_page"],
            meter=vision_meter,
            max_new_calls=params["max_new_calls"],
            progress=emit,
        )
        # 2) 知识抽取入库：逐子块进度 + graph_lock 串行化写库
        build_knowledge_bases(
            pages_data=pages_data,
            subject=params["subject"],
            vector_db=rt.vector_db,
            graph_db=rt.graph_db,
            max_chars=params["max_chars"],
            max_chunks=params["max_chunks"],
            meter=reasoning_meter,
            pdf_id=pdf_id,
            book_name=book_name,
            progress=emit,
            graph_lock=rt.graph_lock,
        )
        task.result = {
            "pages": len(pages_data),
            "nodes": rt.graph_db.graph.number_of_nodes(),
            "vision_calls": vision_meter.calls,
            "vision_tokens": [vision_meter.prompt_tokens, vision_meter.completion_tokens],
            "reasoning_calls": reasoning_meter.calls,
            "reasoning_tokens": [reasoning_meter.prompt_tokens,
                                 reasoning_meter.completion_tokens],
        }
        emit({"stage": "task", "event": "summary", "result": task.result})

    return work


@router.get("/build/tasks", response_model=TaskList, summary="建库任务列表")
async def list_builds(request: Request) -> TaskList:
    rt = request.app.state.runtime
    rows = rt.builds.list_tasks()
    return TaskList(count=len(rows), tasks=[TaskStatus(**r) for r in rows])


@router.get("/build/tasks/{task_id}", response_model=TaskStatus,
            summary="建库任务状态")
async def get_build(task_id: str, request: Request) -> TaskStatus:
    rt = request.app.state.runtime
    task = rt.builds.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return TaskStatus(**task.snapshot())


@router.get("/build/tasks/{task_id}/events", summary="建库进度（SSE）")
async def build_events(task_id: str, request: Request,
                       since: int = Query(0, ge=0, description="续传游标=已收到的最大 seq")):
    rt = request.app.state.runtime
    if rt.builds.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    def _producer() -> Iterator[Dict[str, Any]]:
        cursor = since
        while True:
            task = rt.builds.get(task_id)
            if task is None:
                return
            for ev in task.events_since(cursor):
                cursor = ev["seq"]
                yield {"type": "progress", **ev}
            if task.terminal():
                yield {"type": "task_status", **task.snapshot()}
                return
            # 无新事件：等待唤醒或超时轮询（1s），避免忙等
            task.wakeup.wait(timeout=1.0)
            task.wakeup.clear()

    return StreamingResponse(sse_from_producer(_producer),
                             media_type="text/event-stream")
