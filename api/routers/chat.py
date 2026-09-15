#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/chat —— 多轮会话（对应 CLI --stage chat）：会话增删查 + 逐轮流式对话。

会话历史经 SqliteSaver 按 thread_id 持久化到 output/chat/checkpoints.sqlite，
与 CLI chat 完全共用：CLI ``--session`` 能续聊 API 创建的会话，反之亦然。

- GET  /chat/sessions                     会话清单（同 chat --list）
- POST /chat/sessions                     新建会话（返回 thread_id）
- GET  /chat/sessions/{id}                某会话完整消息快照
- POST /chat/sessions/{id}/messages       发一轮提问：stream=true 走 SSE 逐 token，
                                          stream=false 等完整结果返回 JSON
- POST /chat/sessions/{id}/export         导出为 Markdown（同 chat --export）
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from api.deps import run_blocking, sse_from_producer
from api.runner import ensure_session, run_chat_turn, session_messages
from api.schemas import (ChatMessageRequest, ChatTurnResult,
                        CreateSessionRequest, ExportResult, SessionDetail,
                        SessionList)
from chat_session import export_session_md, list_sessions

router = APIRouter(tags=["chat"])

# 会话 id 白名单：CLI 生成的是 s-<12hex>，也允许外部自定义同类安全 id。
# 限制字符集避免把 id 当路径用（export 落盘文件名）时出问题。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


def _check_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(
            status_code=422,
            detail="会话 id 只允许字母/数字/下划线/点/短横线，长度 1-64")
    return session_id


@router.get("/chat/sessions", response_model=SessionList, summary="会话清单")
async def get_sessions() -> SessionList:
    rows = await run_blocking(list_sessions)
    return SessionList(count=len(rows), sessions=rows)


@router.post("/chat/sessions", response_model=SessionDetail, summary="新建会话")
async def create_session(body: CreateSessionRequest) -> SessionDetail:
    sid = _check_id(body.session_id) if body.session_id else ensure_session(None)
    # 此处只返回 id（真实 thread 在该会话首次发消息时由 checkpointer 建立）
    return SessionDetail(thread_id=sid, history_summary="", messages=[])


@router.get("/chat/sessions/{session_id}", response_model=SessionDetail,
            summary="会话历史快照")
async def get_session(session_id: str) -> SessionDetail:
    _check_id(session_id)
    data = await run_blocking(session_messages, session_id)
    if not data["messages"]:
        raise HTTPException(status_code=404, detail=f"会话不存在或为空: {session_id}")
    return SessionDetail(**data)


@router.post("/chat/sessions/{session_id}/messages",
             summary="发一轮提问（默认 SSE 流式）")
async def post_message(session_id: str, body: ChatMessageRequest,
                       request: Request):
    rt = request.app.state.runtime
    sid = _check_id(session_id)

    def _producer() -> object:
        return run_chat_turn(sid, body.message, vector_db=rt.vector_db,
                             graph_db=rt.graph_db)

    if body.stream:
        return StreamingResponse(sse_from_producer(_producer),
                                 media_type="text/event-stream")

    # 非流式：收集 token 事件拼出完整回复，取 result 事件作响应体
    def _collect() -> dict:
        out: Optional[dict] = None
        for ev in run_chat_turn(sid, body.message, vector_db=rt.vector_db,
                                graph_db=rt.graph_db):
            if ev.get("type") == "result":
                out = ev["data"]
        if out is None:
            raise RuntimeError("该轮未生成有效讲解内容")
        return out

    data = await run_blocking(_collect)
    return ChatTurnResult(**data)


@router.post("/chat/sessions/{session_id}/export", response_model=ExportResult,
             summary="导出会话为 Markdown")
async def export_session(session_id: str, download: bool = False):
    _check_id(session_id)
    path = await run_blocking(export_session_md, session_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"会话不存在或为空: {session_id}")
    if download:
        return FileResponse(str(path), media_type="text/markdown",
                            filename=path.name)
    return ExportResult(thread_id=session_id, path=str(path.resolve()))
