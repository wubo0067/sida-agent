#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI 应用装配：lifespan 管理双库/线程池/build 执行器，挂载各路由。

启动方式：
    uv run python main.py --stage serve [--host 127.0.0.1 --port 8000]
    或 uv run uvicorn api.app:app --host 127.0.0.1 --port 8000
交互式文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import deps
from api.deps import Runtime
from api.routers import ask, books, build, chat
from logger import get_logger
from storage.image_store import IMAGE_ROOT, IMAGE_URL_MOUNT

log = get_logger()

_DESC = """初中理科全科知识库 Agent 的 HTTP 接口。

- **books**：已导入教材清单（对应 CLI `--list-books`）。
- **ask**：单轮问答，支持完整 JSON 或 SSE 逐 token 流式。
- **chat**：多轮会话，历史持久化，与 CLI chat 共用同一份 checkpoints.sqlite。
- **build**：PDF 建库，异步任务模型（提交返回 task_id，轮询状态或订阅 SSE 进度）。

流式端点均为 **SSE**（`text/event-stream`），每帧 `data: <json>`，末尾 `event: end`。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = Runtime()
    from api.tasks import BuildRegistry  # 延迟导入避免循环依赖
    rt.builds = BuildRegistry()
    deps.set_runtime(rt)
    app.state.runtime = rt
    log.info("[api] sida-agent HTTP 服务已启动（双库 + build 执行器就绪）")
    try:
        yield
    finally:
        rt.builds.shutdown()
        rt.shutdown()
        deps.set_runtime(None)
        log.info("[api] sida-agent HTTP 服务已停止")


def create_app() -> FastAPI:
    app = FastAPI(
        title="sida-agent API",
        description=_DESC,
        version="0.1.0",
        lifespan=lifespan,
    )
    # 外部系统对接常为浏览器跨域调用；默认放开跨域（本机/内网自用）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(books.router)
    app.include_router(ask.router)
    app.include_router(chat.router)
    app.include_router(build.router)

    # 教材原图静态挂载：把 output/pdf_images 目录暴露为 /pdf_images/*，供外部
    # 系统直接以 HTTP URL 加载回答末尾的「教材原图」PNG（PNG 带正确 MIME、
    # 支持浏览器缓存）。API 出参里的图片链接由 image_store.
    # rewrite_image_paths_to_urls 重写为 {base}/pdf_images/{pdf_id}/p页.png。
    # StaticFiles 在目录不存在时构造即抛错，故先 mkdir 兜底（只问答未补图的
    # 环境里目录可能还没建）；空目录挂载后访问任意图片返回 404，与
    # render_image_section「只渲染已落盘图片」的防死链语义一致。
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount(IMAGE_URL_MOUNT, StaticFiles(directory=IMAGE_ROOT),
              name="pdf_images")

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict:
        rt = app.state.runtime
        return {
            "status": "ok",
            "graph_nodes": rt.graph_db.graph.number_of_nodes(),
            "vector_count": rt.vector_db._collection.count(),
        }

    return app


app = create_app()
