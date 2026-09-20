#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GET /books —— 对应 CLI --list-books：列出已登记进图谱的教材。

一条 = 一本「逻辑书」（同名多版本折叠）：pdf_id 是 PDF 内容哈希，同一本教材
改版即新 id，逐条返回会让前端显示成好几本同名教材，因此这里按逻辑书聚合，
并把全部版本号放进 versions 供前端做版本切换/展开。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.deps import run_blocking
from api.schemas import BookItem, BookList, BookVersion

router = APIRouter(tags=["books"])


@router.get("/books", response_model=BookList, summary="已导入教材清单（按逻辑书聚合）")
async def list_books(request: Request) -> BookList:
    rt = request.app.state.runtime

    def _read() -> BookList:
        # 与 build 写侧共用 graph_lock：避免读到「写图未落盘」的中间态
        with rt.graph_lock:
            books = rt.graph_db.logical_books()
        items = [
            BookItem(
                logical_book_id=b["logical_book_id"],
                name=b["name"] or "（未命名）",
                version_count=b["version_count"],
                active_pdf_id=b["active_pdf_id"],
                versions=[BookVersion(**v) for v in b["versions"]],
            )
            for b in sorted(books.values(),
                            key=lambda b: (b["name"], b["logical_book_id"]))
        ]
        return BookList(count=len(items),
                        version_count=sum(i.version_count for i in items),
                        books=items)

    return await run_blocking(_read)
