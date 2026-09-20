#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 请求 / 响应模型（Pydantic v2）。

集中定义各端点的入参校验与出参结构，FastAPI 据此生成 /docs（OpenAPI）。
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from ingestion import _CHUNK_MAX_CHARS_DEFAULT

Subject = Literal["physics", "chemistry", "math"]


# ---- /books ----
class BookVersion(BaseModel):
    """教材的一个内容版本（pdf_id = PDF 内容哈希，改版即新 id）。"""

    pdf_id: str
    name: str = ""
    is_active: Optional[bool] = Field(
        None, description="是否被显式指定为当前版本；None=未表述（老数据）")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BookItem(BaseModel):
    """一本「逻辑书」（同名多版本聚合成一条，前端不再看到同名重复项）。"""

    logical_book_id: str = Field(..., description="逻辑书 id（同名归一化，可由 --book-id 指定）")
    name: str
    version_count: int = Field(1, description="已导入的内容版本数")
    active_pdf_id: Optional[str] = Field(
        None,
        description="当前版本；多版本且未显式指定时为 null（不猜，需 set-active 指定）",
    )
    versions: List[BookVersion] = Field(default_factory=list)


class BookList(BaseModel):
    count: int = Field(..., description="逻辑书（教材）数量")
    version_count: int = Field(0, description="登记的全部版本数（pdf_id 条数）")
    books: List[BookItem]


# ---- /ask ----
class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, description="学生提问")
    subject: Optional[Subject] = Field(
        None, description="可选：预选学科；缺省由 Agent 自动判定")
    save: bool = Field(True, description="是否同时把讲解保存为 output/answers/*.md")


class AskResult(BaseModel):
    query: str
    target_subject: Optional[str] = None
    target_concept: Optional[str] = None
    intent: Optional[str] = None
    final_answer: str
    answer_path: Optional[str] = Field(
        None, description="save=true 时写出的 Markdown 路径")


# ---- /chat ----
class CreateSessionRequest(BaseModel):
    session_id: Optional[str] = Field(
        None, description="指定新会话 id；缺省自动生成 s-xxxx")


class SessionSummary(BaseModel):
    thread_id: str
    updated_at: str = ""
    turns: int = 0
    first_question: str = ""
    chars: int = 0


class SessionList(BaseModel):
    count: int
    sessions: List[SessionSummary]


class ChatMessage(BaseModel):
    role: Literal["human", "ai"]
    content: str


class SessionDetail(BaseModel):
    thread_id: str
    history_summary: str
    messages: List[ChatMessage]


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, description="本轮提问")
    stream: bool = Field(True, description="true=SSE 逐 token；false=等完整结果(JSON)")


class ChatTurnResult(BaseModel):
    thread_id: str
    reply: str
    target_subject: Optional[str] = None
    target_concept: Optional[str] = None
    answer_path: Optional[str] = None


class ExportResult(BaseModel):
    thread_id: str
    path: str


# ---- /build ----
class EstimateRequest(BaseModel):
    pdf: str = Field(..., description="教材 PDF 路径")
    start_page: int = Field(1, ge=1, alias="startPage")
    end_page: int = Field(..., ge=1, alias="endPage")
    subject: Subject = "physics"
    max_chars: int = Field(_CHUNK_MAX_CHARS_DEFAULT, alias="maxChars")
    max_new_calls: Optional[int] = Field(
        None, alias="maxNewCalls", description="本批视觉新调用上限，同 CLI --max-new-calls")

    model_config = {"populate_by_name": True}


class EstimateResult(BaseModel):
    range_pages: int
    processed_pages: int
    cached_pages: int
    new_vision_calls: int
    skipped_pages: int
    plan_chunks: int
    cached_chunks: int
    new_chunks: int
    approx_len: int
    vision_capped: bool
    new_calls_total: int = Field(
        0, description="new_vision_calls + new_chunks*2，预估新增模型调用总量")


class BuildRequest(EstimateRequest):
    book: Optional[str] = Field(None, description="教材显示名，同 CLI --book")
    book_id: Optional[str] = Field(
        None, alias="bookId",
        description="逻辑书 id（同名多版本聚合用），同 CLI --book-id；缺省由 book 派生")
    max_chunks: Optional[int] = Field(None, alias="maxChunks")
    save_every_chunks: int = Field(10, ge=1, alias="saveEveryChunks",
                                  description="每 N 个子块保存一次图谱快照")
    confirm: bool = Field(
        True, description="false 且存在新调用时不启动，仅回 409 让调用方看预估")


class BuildAccepted(BaseModel):
    task_id: str
    status: str
    estimate: EstimateResult


class TaskStatus(BaseModel):
    task_id: str
    status: str
    params: dict
    error: Optional[str] = None
    result: Optional[dict] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_event_seq: int


class TaskList(BaseModel):
    count: int
    tasks: List[TaskStatus]
