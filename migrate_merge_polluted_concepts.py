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
3. 向量库：先将污染正文（去首行旧标题）合并写入并校验正主切片，再删除污染
    Concept 切片；滚动上下文检索不再可能看到长句名；
4. 对受影响学科重跑 analyze_graph 刷新 importance/community，图谱统一 save。

用法：uv run python migrate_merge_polluted_concepts.py [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path
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
    for nid, nd in store.graph.nodes(data=True):
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
    """安全合并向量切片，先写正主并校验，再删除污染切片。

    Chroma 没有和图谱 JSON 共享的跨库事务，因此这里采用可补偿的顺序：
    先保留原始文档，更新/插入 canonical 并验证，最后才删除 alias；任一步失败
    都不应主动删除已有数据。
    """
    vdb = get_vector_store()
    got = vdb.get(ids=[alias_key])
    alias_docs = (got or {}).get("documents") or []
    if not alias_docs:
        log.info("[migrate] 向量库无污染切片 %s，跳过", alias_key)
        return
    # 去掉首行旧标题（"概念：长句名"），保留拆解/易错点等增量正文
    body = "\n".join(
        "\n".join((doc or "").splitlines()[1:]).strip()
        for doc in alias_docs
        if doc
    ).strip()
    got_t = vdb.get(ids=[canonical_key])
    t_docs = (got_t or {}).get("documents") or []
    t_metas = (got_t or {}).get("metadatas") or []
    if t_docs:
        canonical_content = "\n".join(doc for doc in t_docs if doc).strip()
        if body and body not in canonical_content:
            merged = (canonical_content + "\n" + body).strip()
        else:
            merged = canonical_content
        meta = dict(t_metas[0]) if t_metas else {"id": canonical_key}
    else:
        merged = f"概念：{bare_name(canonical_key)}\n{body}"
        meta = {"id": canonical_key,
                "subject": canonical_key.split(":", 1)[0], "type": K_CONCEPT}

    document = Document(page_content=merged, metadata=meta)
    if t_docs:
        vdb.update_documents(ids=[canonical_key], documents=[document])
    else:
        vdb.add_documents([document], ids=[canonical_key])
    verified = vdb.get(ids=[canonical_key]).get("documents") or []
    if not verified or verified[0] != merged:
        raise RuntimeError(f"向量库写入校验失败: {canonical_key}")

    try:
        vdb.delete(ids=[alias_key])
        if (vdb.get(ids=[alias_key]).get("documents") or []):
            raise RuntimeError(f"向量库删除校验失败: {alias_key}")
    except Exception:
        # canonical 已经有完整内容，恢复 alias 可使本次操作可重试，避免信息丢失。
        try:
            vdb.add_documents(
                [Document(page_content=alias_docs[0], metadata=(got.get("metadatas") or [{}])[0])],
                ids=[alias_key],
            )
        except Exception:
            log.exception("[migrate] alias 恢复失败，请从备份恢复: %s", alias_key)
        raise
    log.info("[migrate] 向量切片已合并重写: %s（+%d 字符）", canonical_key, len(body))


def _backup_graph() -> Path | None:
    """在首次写入前保留一份带时间戳的图谱备份。"""
    source = Path("output/knowledge_graph.json")
    if not source.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = source.with_name(f"{source.stem}.pre_migrate_{stamp}{source.suffix}")
    shutil.copy2(source, backup)
    log.info("[migrate] 已备份图谱: %s", backup)
    return backup


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并污染 Concept 节点及其向量切片")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只扫描并打印候选，不修改图谱或向量库",
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=1,
        help="每处理多少个节点保存一次图谱（默认 1）",
    )
    args = parser.parse_args()
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every 必须大于 0")
    return args


def main() -> int:
    args = _parse_args()
    store = ScienceGraphStore.load()
    polluted = _find_polluted(store)
    if not polluted:
        log.info("[migrate] 未发现「name：定义摘要」污染概念节点，无需处理")
        return 0
    log.info("[migrate] 发现 %d 个污染概念节点：%s",
             len(polluted), "、".join(a for a, _c, _s in polluted))
    for alias, canonical, subject in polluted:
        log.info("[migrate] 候选: %s:%s -> %s", subject, alias, canonical)
    if args.dry_run:
        log.info("[migrate] dry-run 结束，未修改任何数据")
        return 0

    _backup_graph()
    subjects = set()
    merged_count = 0
    for index, (alias, canonical, subject) in enumerate(polluted, start=1):
        alias_key = node_key(subject, K_CONCEPT, alias)
        canonical_key = node_key(subject, K_CONCEPT, canonical)
        try:
            # 先更新并校验向量，再改图谱；向量失败时不先删除图节点。
            _fix_vector(alias_key, canonical_key)
            if not store.merge_concepts(subject, canonical, alias):
                log.warning("[migrate] merge_concepts 未执行（节点已不存在？）: %s", alias)
                continue
        except Exception:
            log.exception("[migrate] 合并失败，已停止于 %s", alias)
            return 1
        subjects.add(subject)
        merged_count += 1
        if index % args.checkpoint_every == 0:
            store.save()
    for subject in sorted(subjects):
        analyze_graph(store, subject, save=True)
    store.save()
    log.info("[migrate] 修复完成：合并 %d 个节点，重跑 %s 科结构分析",
             merged_count, "、".join(sorted(subjects)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
