#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教材页面图片存储：为回答提供「教材原图」旁路（页图落盘 + 路径规则 + 区块渲染）。

为什么要单独一层
----------------
PDF 提取链路（pdf_processor）把每页整页渲染成 PNG 交给视觉模型，模型转写成
Markdown 后图就被丢弃了（仅内存、不落盘）；存储层（Chroma / NetworkX）与检索、
生成三层因此都不存在任何图片实体，回答里只能出现「（见《X》第 N 页）」这类
纯文字出处标注，学生看不到原图。

本模块在不改动任何既有链路语义的前提下补一条**图片旁路**：

- 提取阶段（pdf_processor）按显示尺寸额外渲染一份 PNG 落盘到
  ``output/pdf_images/{pdf_id}/p{页码:04d}.png``；
- 抽取阶段（ingestion）把每个知识实体出现过的页码写进图谱节点的 ``page_refs``；
- 生成阶段（agent/workflow）按本轮命中的 (pdf_id, 页码) 直接拼出图片区块
  追加到回答末尾，排版由本模块的 :func:`render_image_section` 统一负责。

页码来源有两条，覆盖两类提问：**搜题路径**取向量库命中页的 metadata；
**概念/公式/实验讲解路径**取图谱实体的 page_refs（见
:func:`refs_from_graph_context`，各实体按优先级占用配额）。

两条硬约束（改动时务必保持）
--------------------------
1. **图片落盘必须与内容版本号解耦**。判重只看文件是否存在，绝不能让补图影响
   ``pdf_processor._EXTRACT_VERSION`` 或 ``ingestion._EXTRACT_SCHEMA_VERSION``。
   否则补图会使 500+ 页视觉提取缓存、乃至全部子块抽取缓存失效，迫使最贵的
   两类模型调用全量重跑（视觉模型多模态输入尤其贵）。
2. **页 Markdown 文本不得写入图片路径**。``ingestion._cache_key`` 直接对页文本
   取 sha256，插入任何图片链接都会改变缓存键，触发约束 1 描述的后果。图片只在
   「回答输出层」拼接，不回流到页文本、不进入向量库。

注意区分两种「动版本号」的情形，别把它当成本约束的反例：

- **纯旁路变更**（改渲染排版、改路径规则、改去重口径）——不得动任何版本号，
  因为落盘的图和页文本都没变，没有任何重建的必要；
- **抽取 schema 变更**（如给实体新增 ``source_pages`` 字段，见
  ``ingestion._EXTRACT_SCHEMA_VERSION`` 的 v4 注释）——**必须**递增抽取版本号，
  否则旧缓存缺字段、功能静默失效。此时抽取层需重建（页/视觉缓存不受影响，
  这正是把图片旁路做在前面的价值）。

图片链接由**应用层确定性生成**，不让 LLM 写：模型输出图片链接的可靠性很低
（同本仓库里公式定界符 ``\\[ \\]`` 需要提示词硬约束 + 后处理兜底才压住），
而按 (pdf_id, 页码) 拼路径是可测试、零幻觉、零 token 成本的确定性操作。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from logger import get_logger

log = get_logger()

# sida-agent 项目根（storage/ 的上一级目录），与 pdf_processor.BASE_DIR 同根
BASE_DIR = Path(__file__).resolve().parent.parent

# 教材原图落盘根目录（相对项目根为 output/pdf_images）
IMAGE_ROOT = BASE_DIR / "output" / "pdf_images"

# 供人查看的显示图长边像素。与 pdf_processor.MAX_LONG_EDGE(3000) 刻意分离：
# 3000px 是为了让视觉模型看清手写小字（仅内存、不落盘），这里 1600px 兼顾清晰度
# 与体积——整本书 500+ 页若按 3000px 全量落盘可达 0.5~1.5GB。
DISPLAY_LONG_EDGE = 1600

# 回答 Markdown 中图片路径的书写前缀（以项目根为基准）。落盘时再按输出文件
# 实际位置重写为相对路径（见 relativize_image_paths）。
IMAGE_REL_PREFIX = "output/pdf_images/"

# 匹配 Markdown 图片/链接目标中的项目根相对图片路径，如 ![...](output/pdf_images/ab/p0034.png)
_ROOT_REL_IMAGE_RE = re.compile(r"\((" + re.escape(IMAGE_REL_PREFIX) + r"[^)\s]+)\)")

# 图片引用：规范化后统一为 (pdf_id, 页码) 二元组
ImageRef = Tuple[str, int]

# 单条回答最多附几张教材原图。上限是必需的：一个枢纽概念（如"电功率"）可聚合出
# 几十条公式/实验/题型，每条都带若干页码，不限量会挂出几十张整页图——单张显示图
# 300KB 量级，回答体积与阅读负担都会失控。截断在「确实已落盘」的引用上执行
# （见 render_image_section），因此没补图的引用不会白占配额。
MAX_IMAGES_PER_ANSWER = 6


def page_image_path(pdf_id: str, page_no: int) -> Path:
    """某页显示图的绝对路径：output/pdf_images/{pdf_id}/p{页码:04d}.png。"""
    return IMAGE_ROOT / str(pdf_id) / f"p{int(page_no):04d}.png"


def page_image_relpath(pdf_id: str, page_no: int) -> str:
    """某页显示图相对项目根的路径（写入回答 Markdown 的书写形态）。"""
    return f"{IMAGE_REL_PREFIX}{pdf_id}/p{int(page_no):04d}.png"


def has_page_image(pdf_id: Any, page_no: Any) -> bool:
    """该页显示图是否已落盘（未跑过提取侧补图时恒为 False）。"""
    if not pdf_id or page_no is None:
        return False
    try:
        return page_image_path(str(pdf_id), int(page_no)).is_file()
    except (TypeError, ValueError):
        return False


def write_page_image(pdf_id: str, page_no: int, png: bytes) -> Path:
    """把已渲染好的 PNG 字节落盘（目标已存在则原样返回，不覆盖、不重渲染）。

    「存在即跳过」是本模块唯一的判重口径，刻意不使用任何版本号：补图属于
    新增旁路产物，不应使既有文本缓存失效（见模块 docstring 约束 1）。
    """
    path = page_image_path(pdf_id, page_no)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def normalize_refs(refs: Iterable[Any]) -> List[ImageRef]:
    """把任意形态的图片引用规范化为 (pdf_id, 页码) 列表：去重保序、丢弃非法项。

    兼容三种输入形态：``{"pdf_id": ..., "page": ...}`` 字典、``(pdf_id, page)``
    二元组/列表，以及图谱节点上的 ``"pdf_id:页码"`` 复合串（见
    ingestion._write_graph）。之所以要兼容，是因为 state 经 checkpointer 的
    JsonPlusSerializer 落盘后，tuple 会被序列化成 list 取回；复合串则可能被
    上层原样透传（如直接把节点的 page_refs 丢进来），不做解析就会静默丢图。
    """
    out: List[ImageRef] = []
    for ref in refs or []:
        pdf_id: Any = ""
        page: Any = None
        if isinstance(ref, dict):
            pdf_id, page = ref.get("pdf_id"), ref.get("page")
        elif isinstance(ref, str):
            # 只认最后一个「:」作分隔符：pdf_id 用不到「:」，但这样更耐脏数据
            pdf_id, _, page = ref.rpartition(":")
        elif isinstance(ref, (tuple, list)) and len(ref) == 2:
            pdf_id, page = ref
        if not pdf_id or page is None:
            continue
        try:
            item: ImageRef = (str(pdf_id), int(page))
        except (TypeError, ValueError):
            continue
        if item not in out:
            out.append(item)
    return out


def refs_from_metadatas(metadatas: Iterable[Any]) -> List[ImageRef]:
    """从向量库 metadata 批量提取 (pdf_id, 页码) 图片引用（去重保序）。

    页切片的 metadata 由 ``ingestion._build_page_docs`` 写入（含 ``pdf_id`` /
    ``page``）；缺 ``pdf_id`` 的是旧版裸页码键切片，定位不到具体教材，
    无法拼出图片路径，直接跳过。
    """
    refs: List[ImageRef] = []
    for meta in metadatas or []:
        if not isinstance(meta, dict):
            continue
        pdf_id, page = meta.get("pdf_id"), meta.get("page")
        if not pdf_id or page is None:
            continue
        try:
            item: ImageRef = (str(pdf_id), int(page))
        except (TypeError, ValueError):
            continue
        if item not in refs:
            refs.append(item)
    return refs


def refs_from_examples(examples: Iterable[Any]) -> List[ImageRef]:
    """从图谱例题节点批量提取 (pdf_id, 页码) 图片引用（去重保序）。

    例题节点的 ``pdf_id`` 在顶层，页码在 ``source.page``（见 ingestion._write_graph）。
    """
    refs: List[ImageRef] = []
    for ex in examples or []:
        if not isinstance(ex, dict):
            continue
        pdf_id = ex.get("pdf_id")
        page = (ex.get("source") or {}).get("page")
        if not pdf_id or page is None:
            continue
        try:
            item: ImageRef = (str(pdf_id), int(page))
        except (TypeError, ValueError):
            continue
        if item not in refs:
            refs.append(item)
    return refs


def refs_from_entities(entities: Iterable[Any]) -> List[ImageRef]:
    """从图谱知识实体提取 (pdf_id, 页码) 图片引用（去重保序）。

    读的是节点 ``page_refs`` 属性，形态为 ``"pdf_id:页码"`` 复合串列表
    （见 ingestion._write_graph）。之所以用复合串而不是 ``{pdf_id: [页码]}``：
    图谱合并（ingestion._ensure_entity）对 dict 只补缺失键、不并集内层列表，
    同一实体跨子块出现时后一子块的页码会被整段丢弃，而列表走 union 合并。

    单条实体内部按页码升序输出：页码小的通常就是该知识点的引入页/图示页，
    在 :data:`MAX_IMAGES_PER_ANSWER` 截断时更值得优先展示。
    """
    refs: List[ImageRef] = []
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        found: List[ImageRef] = []
        for token in ent.get("page_refs") or []:
            pdf_id, _, page = str(token).rpartition(":")
            if not pdf_id or not page:
                continue
            try:
                item: ImageRef = (pdf_id, int(page))
            except ValueError:
                continue
            if item not in found:
                found.append(item)
        for item in sorted(found, key=lambda r: r[1]):
            if item not in refs:
                refs.append(item)
    return refs


def refs_from_graph_context(graph_ctx: Any) -> List[ImageRef]:
    """按优先级汇总一轮图谱检索结果里所有可配图的页码引用（去重保序）。

    覆盖图谱上下文的全部来源：例题（``pdf_id`` + ``source.page``）、锚点概念或整章
    各概念、公式、实验、题型、方法，以及相邻概念（先修/后续/关联）的 ``page_refs``。
    概念/公式/实验路径此前没有任何页码，这一函数是它们能配上教材原图的唯一入口。

    顺序即优先级（:data:`MAX_IMAGES_PER_ANSWER` 截断时靠前的先占配额）：
    例题 > 锚点概念 > 实验 > 公式 > 题型/方法 > 相邻概念。例题排最前是刻意的——
    它是被提问那道题所在的页，是唯一「答即所问」的图；实验紧随其后，因为装置图
    最依赖图片、纯文字最难讲清；相邻概念放最后，只在配额有余时才展示。
    """
    if not isinstance(graph_ctx, dict):
        return []
    refs: List[ImageRef] = []
    for group in (
        refs_from_examples(graph_ctx.get("examples") or []),
        refs_from_entities([graph_ctx["concept"]] if graph_ctx.get("concept") else []),
        refs_from_entities(graph_ctx.get("concepts") or []),
        refs_from_entities(graph_ctx.get("experiments") or []),
        refs_from_entities(graph_ctx.get("formulas") or []),
        refs_from_entities(graph_ctx.get("question_types") or []),
        refs_from_entities(graph_ctx.get("methods") or []),
        refs_from_entities(graph_ctx.get("prerequisites") or []),
        refs_from_entities(graph_ctx.get("follow_ups") or []),
        refs_from_entities(graph_ctx.get("related_concepts") or []),
    ):
        for item in group:
            if item not in refs:
                refs.append(item)
    return refs


def render_image_section(refs: Iterable[Any], *, heading: str = "【教材原图】",
                         limit: Optional[int] = MAX_IMAGES_PER_ANSWER) -> str:
    """把图片引用渲染成回答末尾的 Markdown 区块；无可用图片时返回空串。

    只渲染**确实已落盘**的图片：在只建库未跑提取补图的环境里返回空串，
    避免回答中出现死链。返回内容以换行开头，便于直接 ``answer += section``。

    limit：最多渲染几张（``None`` 表示不限）。截断刻意放在「是否已落盘」过滤
    **之后**——没补图的教材不会白占配额、把另一本教材的图挤掉，实际能展示的图
    总是尽量填满配额。被截断的页码记 info，避免「图比预期少」这类现象查不到原因。
    """
    available = [r for r in normalize_refs(refs) if has_page_image(*r)]
    if not available:
        return ""
    if limit is not None and len(available) > limit:
        log.info("[image_store] 可配原图 %d 张，按优先级截断至前 %d 张（放弃页码：%s）",
                 len(available), limit,
                 "、".join(str(p) for _, p in available[limit:]))
        available = available[:limit]
    lines = ["", "---", "", f"## {heading}", ""]
    for pdf_id, page in available:
        lines += [
            f"**教材第 {page} 页**", "",
            f"![教材第 {page} 页原图]({page_image_relpath(pdf_id, page)})", "",
        ]
    log.info("[image_store] 回答追加教材原图 %d 张（页码：%s）",
             len(available), "、".join(str(p) for _, p in available))
    return "\n".join(lines)


def relativize_image_paths(text: str, out_path: Path) -> str:
    """把回答里的项目根相对图片路径重写为相对 ``out_path`` 的路径。

    :func:`render_image_section` 产出的路径以项目根为基准（人类可读、与输出
    位置无关）；而 Markdown 阅读器是按「md 文件所在目录」解析相对路径的，因此
    落盘时必须按目标文件位置换算——``output/answers/`` 与 ``output/chat/exports/``
    目录深度不同，需要的相对前缀也不同，逐个换算才不会出现断链。
    """
    if not text or IMAGE_REL_PREFIX not in text:
        return text
    base_dir = Path(out_path).resolve().parent

    def _sub(match: "re.Match[str]") -> str:
        abs_path = BASE_DIR / match.group(1)
        try:
            rel = Path(os.path.relpath(abs_path, base_dir)).as_posix()
            return f"({rel})"
        except ValueError:
            # 跨盘符（如 md 在 D:、项目在 J:）时 relpath 不可用，回退 file:// URI
            return f"({abs_path.as_uri()})"

    return _ROOT_REL_IMAGE_RE.sub(_sub, text)
