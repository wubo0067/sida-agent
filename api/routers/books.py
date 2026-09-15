#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GET /books —— 对应 CLI --list-books：列出已登记进图谱的教材书名。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.deps import run_blocking
from api.schemas import BookItem, BookList

router = APIRouter(tags=["books"])


@router.get("/books", response_model=BookList, summary="已导入教材清单")
async def list_books(request: Request) -> BookList:
    rt = request.app.state.runtime

    def _read() -> BookList:
        # 与 build 写侧共用 graph_lock：避免读到「写图未落盘」的中间态
        with rt.graph_lock:
            names = rt.graph_db.pdf_names()  # {pdf_id: 显示名}
        books = [BookItem(pdf_id=pid, name=(name or "（未命名）"))
                 for pid, name in sorted(names.items(), key=lambda kv: (kv[1], kv[0]))]
        return BookList(count=len(books), books=books)

    return await run_blocking(_read)
