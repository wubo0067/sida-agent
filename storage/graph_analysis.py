#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识图谱结构分析：健康度审计（功能1）+ 重要性/社区分析（功能2）。

借鉴 ai-knowledge-graph 的图分析思路（连通分量审计、中心性加权、Louvain
社区发现），针对 sida-agent 的类型化教学知识图谱做了口径适配：

- 审计（只读不落库）：弱连通分量规模、"检索死区"实体（所在分量无任何
  Concept 节点 / 零边孤儿）、关系类型分布 —— 暴露「知识在库里、
  get_subgraph 走不到」的结构性根因，并把边数异常波动留作回归基线；
- 分析（结果写回节点属性，随 graph_db.save() 持久化）：
  * importance   —— 度/介数/特征向量三中心性加权（0~100），检索截断
                    （graph_store._capped）的同分次级排序键；
  * community    —— Louvain 主题社区 id，跨章社区 = 综合题命题区信号；
  * 桥概念       —— 高介数先修概念（学生断在此处后面整片崩），仅入报告。

计算全部为建库收尾或 CLI --stage analyze 的离线一次性动作（几百节点量级
秒级完成），不参与在线问答链路；Chapter/Subject/PdfSource 等非检索实体
不进入分析视图（章节本就无入图边，计入只会污染分量统计）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import networkx as nx

from logger import get_logger
from storage.graph_store import (
    K_CONCEPT,
    K_EXAMPLE,
    K_EXPERIMENT,
    K_FORMULA,
    K_METHOD,
    K_QUESTION_TYPE,
    ScienceGraphStore,
    bare_name,
)

log = get_logger()

# 参与结构分析的实体种类 = get_subgraph 的可达目标集（_RETRIEVABLE 同款）。
# Chapter/Subject/PdfSource 不在其中：它们不是检索锚点或聚合目标。
_AUDIT_KINDS = (K_CONCEPT, K_FORMULA, K_EXPERIMENT, K_QUESTION_TYPE,
                K_METHOD, K_EXAMPLE)

# importance 三指标权重（与 ai-knowledge-graph 的节点大小公式同源，可按教学效果调权）
_W_DEGREE, _W_BETWEENNESS, _W_EIGENVECTOR = 0.5, 0.3, 0.2

# 报告中「桥概念」与「跨章社区」的展示条数上限
_BRIDGE_TOP_N = 8
_CROSS_CHAPTER_TOP_N = 10


def _subject_view(store: ScienceGraphStore, subject: str) -> nx.Graph:
    """该学科可检索实体构成的无向视图（平行/反向边合并，供中心性与社区计算）。"""
    g = nx.Graph()
    for nid, nd in store.graph.nodes(data=True):
        if nd.get("subject") == subject and nd.get("type") in _AUDIT_KINDS:
            g.add_node(nid)
    for u, v, ed in store.graph.edges(data=True):
        if u in g and v in g:
            g.add_edge(u, v, relation=ed.get("relation"))
    return g


def _node_type(store: ScienceGraphStore, nid: str) -> str:
    return str(store.graph.nodes[nid].get("type") or "?")


# ------------------------------------------------------------------ 功能 1
def audit_structure(store: ScienceGraphStore, subject: str) -> Dict[str, Any]:
    """图健康度审计（只读）：连通分量 / 检索死区 / 关系类型分布。

    返回报告 dict（同时以日志打印）：
    - components：弱连通分量数与规模分布（降序，最多列 15 项）；
    - isolated：零边孤儿节点清单（截断展示）；
    - dead_zone：所在分量不含任何 Concept 的可检索实体——概念锚点检索
      （get_subgraph 从 Concept 出发 1~2 跳）永远走不到它们，即「知识在
      库里却答不出」事故的直接证据；
    - relations：按 relation 计数的边分布（增量建库的回归基线，
      如 PREREQUISITE_OF 骤降 / EXTRA 暴涨即结构劣化信号）。
    """
    g = _subject_view(store, subject)
    report: Dict[str, Any] = {
        "subject": subject,
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
    }
    if g.number_of_nodes() == 0:
        log.info("[graph_analysis] %s 科无可分析实体，跳过审计", subject)
        return report

    # ---- 弱连通分量 ----
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    report["components"] = len(comps)
    report["component_sizes"] = [len(c) for c in comps[:15]]
    log.info("[graph_analysis] %s 科结构审计：实体节点 %d（可检索）、边 %d、"
             "弱连通分量 %d 个（规模 Top: %s）",
             subject, g.number_of_nodes(), g.number_of_edges(), len(comps),
             ", ".join(str(n) for n in report["component_sizes"][:8]))

    # ---- 零边孤儿 ----
    isolated = sorted((bare_name(n) for n in g if g.degree(n) == 0))
    report["isolated"] = isolated[:20]
    report["isolated_count"] = len(isolated)
    if isolated:
        log.warning("[graph_analysis] 审计: %s 科存在 %d 个零边孤儿实体"
                    "（无任何关系，检索必然不可达）: %s",
                    subject, len(isolated), "、".join(isolated[:15]))

    # ---- 检索死区：所在分量没有任何 Concept ----
    # get_subgraph / get_entity_subgraph 均从概念或实体锚点沿边游走；
    # 一个不含 Concept 的分量意味着其中的公式/例题只能靠「实体锚点精确
    # 命中」侥幸可达，概念路径检索（主链路）永远走不到。
    dead_zone: List[Dict[str, str]] = []
    for ci, comp in enumerate(comps, start=1):
        if any(_node_type(store, n) == K_CONCEPT for n in comp):
            continue
        for n in sorted(comp):
            dead_zone.append({"name": bare_name(n), "type": _node_type(store, n),
                              "component": str(ci)})
    report["dead_zone_count"] = len(dead_zone)
    report["dead_zone"] = dead_zone[:20]
    if dead_zone:
        shown = "、".join(f"{d['name']}({d['type']})" for d in dead_zone[:15])
        log.warning("[graph_analysis] 审计: %s 科存在 %d 个「检索死区」实体"
                    "（所在分量无任何概念节点，概念锚点链路不可达）: %s",
                    subject, len(dead_zone), shown)
    else:
        log.info("[graph_analysis] 审计: %s 科所有连通分量均含概念节点，"
                 "无检索死区", subject)

    # ---- 关系类型分布（有向边口径，覆盖学科全部实体间关系）----
    rel_counts: Dict[str, int] = {}
    for _u, _v, ed in store.graph.edges(data=True):
        if store.graph.nodes[_u].get("subject") != subject:
            continue
        rel = str(ed.get("relation") or "?")
        rel_counts[rel] = rel_counts.get(rel, 0) + 1
    report["relations"] = rel_counts
    log.info("[graph_analysis] %s 科关系类型分布: %s", subject,
             ", ".join(f"{k}={v}" for k, v in
                       sorted(rel_counts.items(), key=lambda x: -x[1])))
    return report


# ------------------------------------------------------------------ 功能 2
def analyze_structure(store: ScienceGraphStore, subject: str) -> Dict[str, Any]:
    """中心性 + Louvain 社区分析，结果写入节点属性（importance / community）。

    - importance（0~100）= 100 * (0.5×度 + 0.3×介数 + 0.2×特征向量)，各指标
      min-max 归一。语义：被多少公式/实验/题型指向（考点覆盖广度）、
      是否为知识链路上的桥（前置重要性）、邻居是否也是枢纽。
      消费方：graph_store._capped 在锚点 token 同分时按 importance 降序，
      枢纽概念截断 top-N 保住「被考最多的」而非「最先入库的」；
    - community：Louvain 社区 id（seed 固定保证可复现）。社区与章节的错位
      （一个社区横跨多章）= 跨章综合题命题区，报告列出供教研核对；
    - 桥概念：介数中心性最高的概念（PREREQUISITE_OF 依赖链的咽喉），
      供「学习路径诊断」类回答参考，仅入报告不落属性。
    返回报告 dict。特征向量中心性在个别图上可能不收敛，失败时该项计 0，
    importance 退化为度+介数两指标加权，不影响其余分析。
    """
    g = _subject_view(store, subject)
    report: Dict[str, Any] = {"subject": subject}
    if g.number_of_nodes() == 0:
        log.info("[graph_analysis] %s 科无可分析实体，跳过结构分析", subject)
        return report

    # ---- 三种中心性 ----
    degree = dict(g.degree())
    betweenness = nx.betweenness_centrality(g)
    try:
        eigenvector = nx.eigenvector_centrality(g, max_iter=1000)
    except Exception:  # 不收敛（含全零向量图）等：降级为不计入
        log.warning("[graph_analysis] 特征向量中心性计算失败，importance "
                    "退化为度+介数两指标", exc_info=True)
        eigenvector = {n: 0.0 for n in g}

    def _norm(mapping: Dict[str, float]) -> Dict[str, float]:
        hi = max(mapping.values(), default=0.0)
        if hi <= 0:
            return {k: 0.0 for k in mapping}
        return {k: v / hi for k, v in mapping.items()}

    nd_deg, nd_btw, nd_eig = _norm(degree), _norm(betweenness), _norm(eigenvector)
    importance = {
        n: round(100 * (_W_DEGREE * nd_deg[n] + _W_BETWEENNESS * nd_btw[n]
                        + _W_EIGENVECTOR * nd_eig[n]), 1)
        for n in g
    }

    # ---- Louvain 社区（networkx 内置，seed 固定可复现；失败回退连通分量）----
    try:
        communities = sorted(nx.community.louvain_communities(g, seed=42),
                             key=len, reverse=True)
    except Exception:
        log.warning("[graph_analysis] Louvain 社区检测失败，回退连通分量口径",
                    exc_info=True)
        communities = [c for c in
                       sorted(nx.connected_components(g), key=len, reverse=True)]
    community_of: Dict[str, int] = {}
    for cid, comp in enumerate(communities):
        for n in comp:
            community_of[n] = cid

    # ---- 写回节点属性（随调用方 graph_db.save() 持久化）----
    for nid in g:
        nd = store.graph.nodes[nid]
        nd["importance"] = importance[nid]
        nd["community"] = community_of[nid]
    report["communities"] = len(communities)
    log.info("[graph_analysis] %s 科结构分析完成：%d 个实体已写入 importance/"
             "community 属性（Louvain 社区 %d 个）",
             subject, g.number_of_nodes(), len(communities))

    # ---- 枢纽概念（importance Top）----
    concepts = [n for n in g if _node_type(store, n) == K_CONCEPT]
    top_hub = sorted(concepts, key=lambda n: -importance[n])[:5]
    log.info("[graph_analysis] %s 科核心考点（importance Top5）: %s", subject,
             "、".join(f"{bare_name(n)}({importance[n]:.0f})" for n in top_hub))
    report["top_concepts"] = [
        {"name": bare_name(n), "importance": importance[n]} for n in top_hub]

    # ---- 桥概念（介数 Top）：先修链咽喉，学习路径诊断信号 ----
    bridges = sorted(concepts, key=lambda n: -betweenness[n])[:_BRIDGE_TOP_N]
    bridges = [n for n in bridges if betweenness[n] > 0]
    if bridges:
        log.info("[graph_analysis] %s 科桥概念（介数 Top，学生断链高危前置）: %s",
                 subject, "、".join(f"{bare_name(n)}({betweenness[n]:.3f})"
                                    for n in bridges))
    report["bridges"] = [
        {"name": bare_name(n), "betweenness": round(betweenness[n], 4)}
        for n in bridges]

    # ---- 跨章社区：社区内概念覆盖 ≥2 个章节 = 跨章综合题温床 ----
    cross: List[Dict[str, Any]] = []
    for cid, comp in enumerate(communities):
        chapters: Dict[str, int] = {}
        for n in comp:
            if _node_type(store, n) != K_CONCEPT:
                continue
            ch = str(store.graph.nodes[n].get("chapter") or "").strip()
            if ch:
                chapters[ch] = chapters.get(ch, 0) + 1
        if len(chapters) >= 2:
            cross.append({"community": cid, "size": len(comp),
                          "chapters": sorted(chapters)})
    cross.sort(key=lambda c: -c["size"])
    report["cross_chapter_communities"] = cross[:_CROSS_CHAPTER_TOP_N]
    if cross:
        log.info("[graph_analysis] %s 科发现 %d 个跨章社区（跨章综合题命题区，"
                 "展示前 %d）:", subject, len(cross), _CROSS_CHAPTER_TOP_N)
        for c in cross[:_CROSS_CHAPTER_TOP_N]:
            log.info("  社区#%d（%d 实体）跨章节: %s",
                     c["community"], c["size"], " | ".join(c["chapters"][:6]))
    else:
        log.info("[graph_analysis] %s 科社区划分与章节基本一致，无跨章社区", subject)
    return report


# ------------------------------------------------------------- 统一入口
def analyze_graph(store: ScienceGraphStore, subject: str, *,
                  save: bool = False) -> Dict[str, Any]:
    """功能1+2 统一入口：健康度审计（只读）→ 中心性/社区分析（写属性）。

    save=True 时分析完成后立即 graph_db.save() 持久化新属性；
    建库收尾调用传 False（由建库流程在锁内统一落盘）。
    返回 {"audit": ..., "analysis": ...} 合并报告。
    """
    log.info("[graph_analysis] ===== %s 科图谱结构分析开始 =====", subject)
    report: Dict[str, Any] = {"audit": audit_structure(store, subject)}
    report["analysis"] = analyze_structure(store, subject)
    if save:
        store.save()
    log.info("[graph_analysis] ===== %s 科图谱结构分析结束 =====", subject)
    return report
