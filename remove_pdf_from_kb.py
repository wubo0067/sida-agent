#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 pdf_id 把一本教材从双库（知识图谱 + 向量库）中整体撤除。

场景：教材导入时选错学科（如化学 PDF 建成 math），其全部实体以错误学科前缀
入图，正确学科检索不到；需要先清理再按正确学科重建。

清理范围（三处，缺一都会留下脏数据）：
1. 图谱实体节点：sources 含该 pdf_id 且为**单来源**的节点（含 Chapter），
   连带其所有边一起删除；多来源节点（跨书合并的公共概念）只从 sources /
   page_refs 中剔除该 pdf_id，节点保留。
2. 注册表节点 meta:PdfSource:{pdf_id}。
3. 向量库文档：与删除节点同 id 的实体切片 + metadata.pdf_id 匹配的讲义页
   切片；多来源节点保留其向量文档。
4. output/build_state.json 中 "{subject}|{pdf_id}|*" 的 chunk 状态（否则
   重建时误判"已建过"而跳过）。

视觉提取缓存 output/pdf_extract/{pdf_id}/ 不动：重建时直接复用，零视觉调用。

用法（默认干跑，仅打印将删除的内容；--apply 才真正写库）：
    python remove_pdf_from_kb.py --pdf-id 613175a1cf29546a
    python remove_pdf_from_kb.py --pdf-id 613175a1cf29546a --apply

安全：执行前自动备份图谱为 knowledge_graph.prepurge.bak.json。
务必先停掉 serve 进程再清理，否则服务内存中的旧图会在下次保存时覆盖清理结果。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from storage.graph_store import K_PDF_SOURCE, ScienceGraphStore  # noqa: E402

_GRAPH_PATH = ROOT / "output" / "knowledge_graph.json"
_VECTOR_DIR = ROOT / "output" / "vector_db"
_BUILD_STATE_PATH = ROOT / "output" / "build_state.json"
_COLLECTION = "science_kb"


def _plan(store: ScienceGraphStore, pdf_id: str):
    """返回 (待删节点集合, 待改节点列表[(node, new_attrs)])，不修改图。"""
    delete: set[str] = set()
    modify: list[tuple[str, dict]] = []
    for nid, nd in store.graph.nodes(data=True):
        if nd.get("type") == K_PDF_SOURCE and str(nd.get("pdf_id")) == pdf_id:
            delete.add(nid)
            continue
        srcs = [str(s) for s in (nd.get("sources") or [])]
        if pdf_id not in srcs:
            continue
        others = [s for s in srcs if s != pdf_id]
        if not others:
            delete.add(nid)
        else:  # 跨书公共实体：只摘除本书来源，节点保留
            refs = [r for r in (nd.get("page_refs") or [])
                    if not str(r).startswith(f"{pdf_id}:")]
            modify.append((nid, {"sources": others, "page_refs": refs}))
    return delete, modify


def main() -> int:
    doc_str = __doc__ or ""
    doc_lines = doc_str.splitlines()
    ap = argparse.ArgumentParser(description=doc_lines[0] if doc_lines else "")
    ap.add_argument("--pdf-id", required=True, help="PDF 内容哈希前 16 位")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除（缺省仅干跑预览）")
    args = ap.parse_args()
    pdf_id = args.pdf_id.strip()

    store = ScienceGraphStore.load(_GRAPH_PATH)
    names = store.pdf_names()
    print(f"教材注册名: {pdf_id} -> {names.get(pdf_id, '（未登记）')}")
    delete, modify = _plan(store, pdf_id)
    print(f"将删除节点 {len(delete)} 个（含其全部边）；将保留并改 sources 的节点 {len(modify)} 个")
    for nid in sorted(delete):
        print("  -", nid)
    for nid, attrs in modify:
        print("  ~", nid, "->", attrs)

    # 向量库中属于该 pdf 的文档 id：删除节点同名 id + 页切片（metadata.pdf_id）
    import chromadb
    client = chromadb.PersistentClient(path=str(_VECTOR_DIR))
    col = client.get_collection(_COLLECTION)
    vec_ids = sorted(set(delete) & {i for i in col.get(ids=list(delete) or ["-"],
                                                       include=[])["ids"]})
    page_docs = col.get(where={"pdf_id": pdf_id}, include=[])["ids"]
    vec_ids = sorted(set(vec_ids) | set(page_docs))
    print(f"将删除向量文档 {len(vec_ids)} 条")

    # build_state 中该 pdf 的 chunk 状态
    state = {}
    if _BUILD_STATE_PATH.exists():
        state = json.loads(_BUILD_STATE_PATH.read_text(encoding="utf-8"))
    stale_keys = [k for k in state.get("chunks", {}) if f"|{pdf_id}|" in k]
    print(f"将清理 build_state 条目 {len(stale_keys)} 条")
    if not args.apply:
        print("\n（干跑模式，未做任何修改。确认后加 --apply 执行）")
        return 0

    if not delete and not modify and not vec_ids and not stale_keys:
        print("没有需要清理的内容，直接退出。")
        return 0

    # 1) 备份并写图谱
    bak = _GRAPH_PATH.with_name("knowledge_graph.prepurge.bak.json")
    shutil.copy2(_GRAPH_PATH, bak)
    print(f"图谱已备份: {bak}")
    for nid, attrs in modify:
        store.graph.nodes[nid].update(attrs)
    store.graph.remove_nodes_from(delete)
    store.save(_GRAPH_PATH)

    # 2) 向量库
    if vec_ids:
        col.delete(ids=vec_ids)
        print(f"向量库已删除 {len(vec_ids)} 条，剩余 {col.count()} 条")

    # 3) build_state
    for k in stale_keys:
        state["chunks"].pop(k, None)
    if stale_keys:
        _BUILD_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"build_state 已清理 {len(stale_keys)} 条")
    print("完成。可按正确学科重新执行 build。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
