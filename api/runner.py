#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问答 / 对话的同步流式执行器：把 agent.stream 转成「事件 dict 生成器」。

与 CLI（main.py）共用同一套业务函数与落盘逻辑（_save_answer_markdown /
_normalize_math_delims / _CHAT_STREAM_NODES），只是把「打印到终端」换成
「yield 事件 dict」，交给 SSE 桥接（deps.sse_from_producer）推送。

生成器全部是同步的，运行在 runtime 线程池里（见 deps），因此可安全做
Chroma / SqliteSaver / LLM 等阻塞调用。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from langchain_core.messages import HumanMessage

from chat_session import open_saver, session_snapshot
from logger import get_logger
from main import (_CHAT_STREAM_NODES, _new_thread_id, _normalize_math_delims,
                  _save_answer_markdown)

log = get_logger()


def _stream_agent(agent: Any, inputs: Dict[str, Any], config: Optional[Dict],
                  stream_nodes: tuple) -> Iterator[Dict[str, Any]]:
    """跑一轮 agent.stream，逐 token 产出 token 事件，收尾产出 result 事件。

    messages 模式按节点过滤只保留生成节点正文增量（delta 去重，兼容后端
    「先增量块再完整块」的重复推送）；values 模式取最后一份状态快照作最终结果。
    思考模式下生成节点会先推 reasoning_content 增量（由
    config.ChatOpenAIWithReasoning 保留在 additional_kwargs），转成
    {"type": "reasoning"} 事件先行推送，客户端可折叠展示思考过程。
    """
    printed = ""
    result: Dict[str, Any] = {}
    stream = (agent.stream(inputs, config=config,
                           stream_mode=["messages", "values"])
              if config else
              agent.stream(inputs, stream_mode=["messages", "values"]))
    for mode, chunk in stream:
        if mode == "messages":
            msg, meta = chunk
            if meta.get("langgraph_node") not in stream_nodes:
                continue
            ak = getattr(msg, "additional_kwargs", None) or {}
            reasoning = ak.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield {"type": "reasoning", "text": reasoning}
                continue
            text = msg.content if isinstance(msg.content, str) else ""
            if text and len(text) > len(printed) and text.startswith(printed):
                yield {"type": "token", "text": text[len(printed):]}
                printed = text
        else:
            result = chunk
    yield {"type": "_final_state", "result": result, "printed": printed}


def run_ask(query: str, *, vector_db: Any, graph_db: Any,
            save: bool = True) -> Iterator[Dict[str, Any]]:
    """单轮问答事件流：token… + 末尾 result（含 final_answer / 元信息 / 落盘路径）。"""
    from agent.workflow import create_circuit_agent  # 延迟导入，避免启动即建图

    agent = create_circuit_agent(vector_db=vector_db, graph_db=graph_db)
    nodes = tuple(n for n in _CHAT_STREAM_NODES
                  if n in ("generate_response", "generate_problem_response"))
    final: Dict[str, Any] = {}
    for ev in _stream_agent(agent, {"query": query}, None, nodes):
        if ev["type"] == "_final_state":
            final = ev["result"]
            continue
        yield ev
    final_answer = _normalize_math_delims(final.get("final_answer", ""))
    out_path = None
    if save and final.get("final_answer"):
        final["query"] = query
        saved = dict(final)
        saved["final_answer"] = final_answer
        try:
            out_path = str(_save_answer_markdown(saved, "physics").resolve())
        except Exception:  # noqa: BLE001 - 落盘失败不影响返回内容
            log.exception("[api] ask 结果落盘失败")
    yield {"type": "result", "data": {
        "query": query,
        "target_subject": final.get("target_subject"),
        "target_concept": final.get("target_concept"),
        "intent": final.get("intent"),
        "final_answer": final_answer,
        "answer_path": out_path,
    }}


def run_chat_turn(thread_id: str, message: str, *, vector_db: Any,
                  graph_db: Any, save: bool = True) -> Iterator[Dict[str, Any]]:
    """多轮对话的一轮事件流：token… + 末尾 result（reply + 元信息 + 落盘路径）。

    会话历史经 SqliteSaver（checkpointer）按 thread_id 持久化，与 CLI chat
    共用同一份 output/chat/checkpoints.sqlite。每轮独立开连接（with open_saver），
    避免跨请求共享非线程安全的单连接。
    """
    from agent.workflow import create_circuit_agent

    with open_saver() as saver:
        agent = create_circuit_agent(vector_db=vector_db, graph_db=graph_db,
                                     checkpointer=saver)
        inputs = {"query": message, "messages": [HumanMessage(content=message)]}
        config = {"configurable": {"thread_id": thread_id}}
        final: Dict[str, Any] = {}
        for ev in _stream_agent(agent, inputs, config, _CHAT_STREAM_NODES):
            if ev["type"] == "_final_state":
                final = ev["result"]
                continue
            yield ev
    reply = _normalize_math_delims(final.get("final_answer", ""))
    out_path = None
    if save and final.get("final_answer"):
        final["query"] = message
        saved = dict(final)
        saved["final_answer"] = reply
        try:
            out_path = str(_save_answer_markdown(saved, "physics").resolve())
        except Exception:  # noqa: BLE001
            log.exception("[api] chat 结果落盘失败")
    yield {"type": "result", "data": {
        "thread_id": thread_id,
        "reply": reply,
        "target_subject": final.get("target_subject"),
        "target_concept": final.get("target_concept"),
        "answer_path": out_path,
    }}


def ensure_session(thread_id: Optional[str]) -> str:
    """缺省时生成新会话 id；给了就原样返回（续聊既有 thread）。"""
    return thread_id or _new_thread_id()


def session_messages(thread_id: str) -> Dict[str, Any]:
    """读取某会话最新快照，转成 {history_summary, messages:[{role,content}]}。"""
    with open_saver() as saver:
        summary, msgs = session_snapshot(saver, thread_id)
    out: List[Dict[str, str]] = []
    for m in msgs:
        cls = type(m).__name__
        if cls == "HumanMessage":
            role = "human"
        elif cls == "AIMessage":
            role = "ai"
        else:
            continue
        content = m.content if isinstance(m.content, str) else "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in m.content)
        if content.strip():
            out.append({"role": role, "content": content})
    return {"thread_id": thread_id, "history_summary": summary, "messages": out}
