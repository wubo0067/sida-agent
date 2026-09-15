#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sida-agent HTTP 服务层（FastAPI）。

把 CLI 的四类能力（list-books / ask / chat / build）暴露为 REST + SSE 接口，
供外部系统对接。设计要点：

- 复用 CLI 同一套业务内核（ingestion / pdf_processor / agent.workflow /
  chat_session / storage），不复制业务逻辑；
- 全栈业务代码是同步的（Chroma / NetworkX / SqliteSaver / LLM 调用），
  因此所有端点经 run_in_threadpool 或后台工作线程执行，绝不在 event loop
  里直接跑阻塞代码；
- 流式统一用 SSE（Server-Sent Events）：问答逐 token、build 逐页/逐子块进度；
- build 是长任务，采用「提交即返回 task_id + 轮询状态 / 订阅进度流」的异步任务
  模型；同一时刻仅允许一个 build 写双库（graph_db 单内存图整体落盘），
  并发提交直接 409。

对外入口：``api.app:app``（uvicorn ASGI 应用），或
``uv run python main.py --stage serve``。
"""
