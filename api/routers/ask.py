#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/ask —— 单轮问答（对应 CLI --stage ask）：JSON 与 SSE 两种端点。

- POST /ask        ：等完整讲解后一次性返回 JSON（外部系统最省事的接法）；
- POST /ask/stream ：SSE 逐 token 推送讲解正文，末尾一条 result 事件带元信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.deps import resolve_image_base_url, run_blocking, sse_from_producer
from api.runner import run_ask
from api.schemas import AskRequest, AskResult

router = APIRouter(tags=["ask"])


@router.post("/ask", response_model=AskResult, summary="单轮问答（完整 JSON）")
async def ask(body: AskRequest, request: Request) -> AskResult:
    rt = request.app.state.runtime
    base = resolve_image_base_url(request)

    def _produce() -> list:
        out: dict = {}
        for ev in run_ask(body.query, vector_db=rt.vector_db,
                          graph_db=rt.graph_db, save=body.save,
                          image_base_url=base):
            if ev.get("type") == "result":
                out = ev["data"]
        return [out]

    data = (await run_blocking(_produce))[0]
    return AskResult(**data)


@router.post("/ask/stream", summary="单轮问答（SSE 流式）")
async def ask_stream(body: AskRequest, request: Request) -> StreamingResponse:
    rt = request.app.state.runtime
    base = resolve_image_base_url(request)
    producer = lambda: run_ask(body.query, vector_db=rt.vector_db,  # noqa: E731
                               graph_db=rt.graph_db, save=body.save,
                               image_base_url=base)
    return StreamingResponse(sse_from_producer(producer),
                             media_type="text/event-stream")
