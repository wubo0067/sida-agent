# -*- coding: utf-8 -*-
"""一次性修复：合并「name：定义摘要」污染概念节点（零 LLM）。

背景（角元塞瓦定理事故，README 6.14）：旧版滚动上下文把已建概念拼成
"name：desc[:40]" 单串注入 prompt，模型按「逐字复用」指令把整串当成概念名
输出，图谱里落下 40+ 字的长句 Concept 节点（如
"塞瓦定理：在三角形内任取一点，……则这"），其挂载的公式（角元/边元塞瓦定理）、
题型、先修边全部与正主节点割裂——概念锚点检索永远走不到这些内容。
抽取层已根治（ingestion._gather_known_context / _restore_polluted_names），
本脚本负责修复既有图谱与向量库中的脏数据。

做法（幂等，重复执行第二次即无候选直接退出）：
1. 找出所有「冒号前缀精确等于同科另一概念名」的 Concept 节点（污染节点）；
2. 调 ScienceGraphStore.merge_concepts 把边与属性并入正主节点（越建越全）；
3. 向量库：删除污染 Concept 切片，正文（去首行旧标题）追加进正主切片后按原
   id 重写，滚动上下文检索不再可能看到长句名；
4. 对受影响学科重跑 analyze_graph 刷新 importance/community，图谱统一 save。

用法：uv run python migrate_merge_polluted_concepts.py
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple, cast

from langchain_core.documents import Document

from logger import get_logger
from storage.graph_analysis import analyze_graph
from storage.graph_store import (
    K_CONCEPT,
    ScienceGraphStore,
    bare_name,
    node_key,
)
from storage.vector_store import get_vector_store

log = get_logger()


def _find_polluted(store: ScienceGraphStore) -> List[Tuple[str, str, str]]:
    """返回污染概念清单 [(污染(别名)节点名, 正主(规范)节点名, 学科)]。

    判据与 ingestion._restore_polluted_names 完全一致：名字含冒号，且冒号前
    缀精确等于同科另一概念节点名。正常概念名（前缀不是已建概念）不会被误伤。
    """
    names_by_subject: Dict[str, set] = {}
    for nid, nd in store.graph.nodes(data=True):
        node_data = cast(Dict[str, Any], nd)
        if node_data.get("type") == K_CONCEPT:
            names_by_subject.setdefault(node_data.get("subject", ""), set()).add(bare_name(nid))
    out: List[Tuple[str, str, str]] = []
    for nid, nd in list(store.graph.nodes(data=True)):
        node_data = cast(Dict[str, Any], nd)
        if node_data.get("type") != K_CONCEPT:
            continue
        subject = node_data.get("subject", "")
        name = bare_name(nid)
        for sep in ("：", ":"):
            if sep not in name:
                continue
            prefix = name.split(sep, 1)[0].strip()
            if prefix and prefix != name and prefix in names_by_subject.get(subject, set()):
                out.append((name, prefix, subject))
                break
    return out


def _fix_vector(alias_key: str, canonical_key: str) -> None:
    """向量库：删污染 Concept 切片，把其正文追加进正主切片（按原 id 重写）。"""
    vdb = get_vector_store()
    got = vdb.get(ids=[alias_key])
    alias_docs = (got or {}).get("documents") or []
    if not alias_docs:
        log.info("[migrate] 向量库无污染切片 %s，跳过", alias_key)
        return
    # 去掉首行旧标题（"概念：长句名"），保留拆解/易错点等增量正文
    body = "\n".join((alias_docs[0] or "").splitlines()[1:]).strip()
    got_t = vdb.get(ids=[canonical_key])
    t_docs = (got_t or {}).get("documents") or []
    t_metas = (got_t or {}).get("metadatas") or []
    if t_docs:
        merged = (t_docs[0].rstrip() + "\n" + body).strip() if body else t_docs[0]
        meta = dict(t_metas[0]) if t_metas else {"id": canonical_key}
    else:
        merged = f"概念：{bare_name(canonical_key)}\n{body}"
        meta = {"id": canonical_key,
                "subject": canonical_key.split(":", 1)[0], "type": K_CONCEPT}
    vdb.delete(ids=[alias_key])
    vdb.add_documents([Document(page_content=merged, metadata=meta)],
                      ids=[canonical_key])
    log.info("[migrate] 向量切片已合并重写: %s（+%d 字符）", canonical_key, len(body))


def main() -> int:
    store = ScienceGraphStore.load()
    polluted = _find_polluted(store)
    if not polluted:
        log.info("[migrate] 未发现「name：定义摘要」污染概念节点，无需处理")
        return 0
    log.info("[migrate] 发现 %d 个污染概念节点：%s",
             len(polluted), "、".join(a for a, _c, _s in polluted))
    subjects = set()
    for alias, canonical, subject in polluted:
        if not store.merge_concepts(subject, canonical, alias):
            log.warning("[migrate] merge_concepts 未执行（节点已不存在？）: %s", alias)
            continue
        _fix_vector(node_key(subject, K_CONCEPT, alias),
                    node_key(subject, K_CONCEPT, canonical))
        subjects.add(subject)
    store.save()
    for subject in sorted(subjects):
        analyze_graph(store, subject, save=True)
    log.info("[migrate] 修复完成：合并 %d 个节点，重跑 %s 科结构分析",
             len(polluted), "、".join(sorted(subjects)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
