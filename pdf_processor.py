#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 页面 -> Markdown 提取（初中物理 / 化学 / 数学多科通用）。

实现思路与 knowledge_extract/extract_pdf/main.py 一致，但模型统一通过
config.py 创建（不再直连 openai）：
- 通用结构化提取 PROMPT：对物理、化学、数学页面一视同仁，完整保留
  印刷文字、公式（统一 LaTeX）、表格、图形标签、手写批注；
- 视觉模型经 config.get_vision_llm() 获取，
  base_url / api_key / model_name 在 sida-agent/.env（VISION_*）中配置；
- 用 PyMuPDF 将每页渲染成 PNG（喂模型的那份仅内存，不落盘）交给视觉大模型
  按提示词提取；
- 每页结果独立落盘 output/pdf_extract/{pdf_id}/p{页码}.md，
  存在且非空即视为已提取，不再调用模型（断点续跑，中断不丢数据）；
- 残页保护（页缓存一旦写入就只随 _EXTRACT_VERSION 失效，坏内容会被永久固化）：
  端点报告 finish_reason 为 length/max_tokens 时按可重试失败处理，绝不返回残页；
  输出短于 MIN_PAGE_CHARS 时同页重试一次，取更完整的一份（见 _has_suspected_truncation）；
- pdf_id = PDF 文件内容的哈希前 16 位，与 extract_pdf 同算法，
  因此两个项目可共用同一份提取缓存目录。

另有一条与文本链路无关的「教材原图」旁路：每页额外按显示尺寸（1600px 长边）
渲染一份 PNG 落盘 output/pdf_images/{pdf_id}/p{页码:04d}.png，供回答引用
（见 storage/image_store.py）。该旁路以「文件是否存在」判重，**不参与
_EXTRACT_VERSION 版本控制**——补图绝不能让既有页缓存失效，否则会让整本书的
视觉提取全量重跑。同理，图片路径也不得写入页 Markdown（那是 ingestion 抽取
缓存的哈希输入）。

对外接口保持：
    extract_pdf_pages_as_markdown(pdf_path, start_page, end_page,
                                  output_dir=None)
    -> List[Dict]： [{"page": 页码, "content": Markdown 文本}, ...]
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path
from typing import Any

import pymupdf  # PyMuPDF
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from config import VISION_ROLE, get_vision_llm, resolve_llm_config
from logger import get_logger
from storage.image_store import (
    DISPLAY_LONG_EDGE,
    page_image_path,
    write_page_image,
)

BASE_DIR = Path(__file__).resolve().parent
log = get_logger()

# ---- 输出路径 -----------------------------------------------------------
# 缓存结构：output/pdf_extract/{pdf_id}/p{页码}_{版本}.md（版本见 _EXTRACT_VERSION）
OUTPUT_DIR = BASE_DIR / "output" / "pdf_extract"

# ---- 视觉解析模型 ---------------------------------------------------------
# 模型服务（base_url / api_key / model_name）统一在 sida-agent/.env 中配置，
# 由 config.get_vision_llm() 创建，调用方无需指定模型。

LLM_TIMEOUT = 300.0      # 单次请求超时（秒）
LLM_RETRIES = 2          # 本地调用失败重试次数（指数退避）
MAX_TOKENS = 8192        # 单次生成最大 token 数

# 页输出字符数阈值：用于识别「疑似残页」（见 _has_suspected_truncation）。
# 视觉模型偶发截断（思考 token 挤占 max_tokens）时会吐出一份不完整的页文本，
# 直接落盘就会被永久固化——缓存只在 _EXTRACT_VERSION 变更时整体失效。
# 阈值取自本仓 563 份页缓存的实测分布：纯封面/近空白页 30~51 字符（21 页，
# 各讲首页），其余页最短 121 字符、中位数 1476 字符。故仅 (BLANK_PAGE_CHARS,
# MIN_PAGE_CHARS) 开区间视为可疑：命中则用同一份 messages 同页重试一次。
MIN_PAGE_CHARS = 600     # 正文字符数下限，低于此值即视为疑似截断
BLANK_PAGE_CHARS = 120   # 低于此值视为封面/近空白页，正常接受、不重试

# 手写批注（红笔小字、上下标）在 ~170dpi 下易糊，长边提到 3000px（A4 ≈ 257dpi）；
# MAX_ZOOM 需同步放大，否则 A4（长边 842pt）会被 3.0x 截在 2526px 到不了 3000。
MAX_LONG_EDGE = 3000     # PDF 页面渲染 PNG 的长边像素上限
MAX_ZOOM = 4.0           # 页面渲染最大放大倍数（72dpi 为 1.0）

# 页缓存版本：PROMPT、渲染参数（MAX_LONG_EDGE/MAX_ZOOM）、后处理（_fix_math）
# 任一变更时递增，使旧页缓存整体失效（对齐 ingestion._EXTRACT_SCHEMA_VERSION）
_EXTRACT_VERSION = "v2"

# ---- 图片内容提取提示词（物理 / 化学 / 数学通用，勿改为单学科专用）----
PROMPT = """你是一名专业的 PDF 页面内容结构化提取器。

你的唯一任务是：
将当前页面中的视觉信息完整、准确地转换为结构化 Markdown。

==================================================
一、最高优先级原则
==================================================

1. 完整性优先。
   页面中可见的印刷文字、公式、表格、图形标签、手写批注都必须尽可能保留。

2. 准确性优先。
   严禁根据常识、学科知识、上下文或题目答案猜测图片中不存在或无法确认的内容。

3. 禁止幻觉。
   看不清的文字、数字、公式、上下标：
   使用 [无法辨认]。

   如果存在两种可能：
   使用 [疑似：A / B]。

4. 不得修改原文。
   不要纠正错别字、公式错误、学生错误答案或教师批注。
   图片中是什么，就提取什么。

5. 不要解题。
   不要计算答案，不要补充图片中没有出现的知识点。
   只提取页面实际包含的信息。

==================================================
二、页面结构
==================================================

首先识别页面中的内容区域，并按照逻辑结构组织：

- 标题
- 小节
- 知识点
- 例题
- 题干
- 选项
- 公式
- 表格
- 图片/电路图/装置图/几何图
- 手写批注
- 思维导图
- 页眉页脚

不要简单地按照像素坐标逐行 OCR。

==================================================
三、普通文字
==================================================

完整提取：

- 标题
- 正文
- 题号
- 题干
- 选项
- 注释
- 页眉页脚

尽可能保持原文标点和文字。

不要自行改写或总结。

==================================================
四、数学 / 物理 / 化学公式
==================================================

所有公式必须使用 LaTeX，并区分学科场景正确转写：

- 数学：代数式、方程、不等式、函数、几何量（角度、线段）等；
- 物理：物理量关系式与单位，如 $P=UI$、$Q=I^2Rt$；
- 化学：化学式、化学方程式与上下标，如 $\\mathrm{Na_2CO_3}$、
  $\\mathrm{2H_2 + O_2 \\xrightarrow{点燃} 2H_2O}$（严禁把化学式转成中文）。

行内公式：

$I=U/R$

独立公式：

$$
P=I^2R=\\frac{U^2}{R}
$$

要求：

- 正确识别上下标
- 正确识别分数
- 正确识别希腊字母
- 正确识别单位
- 正确识别括号
- 正确区分数字、字母和变量
- 不要使用 Unicode 数学字符代替 LaTeX

如果无法确认公式：
使用 [无法辨认的公式]
不要根据学科规律补全。

==================================================
五、表格
==================================================

如果页面存在表格：

必须恢复表格的行列关系。

使用 Markdown table：

| 项目 | 串联 | 并联 |
|---|---|---|
| 电压 | ... | ... |
| 电流 | ... | ... |
| 电阻 | ... | ... |

不得把表格简单展开成普通文字。

==================================================
六、思维导图 / 流程图
==================================================

如果页面存在思维导图、树状图或流程图：

优先恢复节点之间的层级和连接关系。

例如：

## 电功
- 电能
  - 来源
  - 利用
  - 单位与换算
- 电能表
  - 作用
  - 参数
- 电功
  - 实质
  - 公式

不要仅按照图片的从上到下、从左到右顺序输出。

==================================================
七、图形 / 电路图 / 实验装置图 / 几何图
==================================================

对于图形：

必须提取：

1. 所有文字标签
2. 所有元件名称
3. 元件之间的连接关系
4. 重要的位置关系
5. 图中的箭头、虚线、辅助线
6. 图中明确标出的数值

物理电路图，必须描述：

- 电源
- 开关
- 电阻
- 灯泡
- 电流表
- 电压表
- 各元件之间的串联/并联关系
- 电表连接位置

化学实验装置图，必须描述：

- 仪器名称（烧杯、酒精灯、集气瓶、导管、铁架台等）
- 装置连接顺序与气路走向
- 液面、药品颜色等可见信息

几何图 / 函数图，必须描述：

- 图形种类（三角形、圆、坐标系、抛物线等）
- 关键点、边、角、辅助线及其标注

不要只输出标签。

不要根据图形推断图中没有明确表达的信息。

==================================================
八、手写批注
==================================================

必须区分：

1. 印刷体
2. 手写内容

如果发现手写内容：

使用：

[红笔手写：...]
[蓝笔手写：...]
[黑笔手写：...]

同时尽可能说明它的位置和关联对象：

[红笔手写，位于例23选项D右侧：
$Q=I^2Rt$]

如果手写内容无法辨认：

[红笔手写：无法辨认]

不要把手写批注与印刷文字混为一体。

==================================================
九、手写批注与原文的关系
==================================================

如果手写内容明确：

- 圈选某个选项
- 划掉某个选项
- 指向某个公式
- 指向某个图形元件
- 在某道题旁边进行计算

必须描述这种关系。

例如：

[红笔手写：圈选 D]
[红笔手写：位于例25选项附近：Q=I²Rt]
[红笔箭头：指向例26中的 R1]

不要推测箭头没有明确指向的对象。

==================================================
十、阅读顺序
==================================================

普通文本：
按照自然阅读顺序组织。

多栏：
先完成左栏，再完成右栏。

表格：
按照表格结构。

思维导图：
按照节点层级。

图形：
按照图形结构。

手写批注：
放在其关联的内容附近。

==================================================
十一、颜色
==================================================

彩色印刷文字仍然属于印刷体。

只有明确属于手写笔迹的内容才标记：

[红笔手写：...]
[蓝笔手写：...]

不要因为文字本身是红色/蓝色就认为它是手写。

==================================================
十二、完整性检查
==================================================

输出前检查：

□ 是否遗漏标题
□ 是否遗漏题号
□ 是否遗漏题干
□ 是否遗漏选项
□ 是否遗漏公式
□ 是否遗漏表格
□ 是否遗漏图形标签
□ 是否遗漏手写批注
□ 是否遗漏手写公式
□ 是否保持表格结构
□ 是否保持思维导图层级
□ 是否描述重要图形连接关系
□ 是否存在自行猜测的内容

如果看不清，必须标记 [无法辨认]，不能猜。

==================================================
十三、输出要求
==================================================

只输出 Markdown。

不要输出：

- 分析过程
- OCR 过程
- 识别置信度说明
- 题目答案
- 额外知识
- 图片中不存在的内容

开始提取当前页面。"""


def _vision_llm() -> ChatOpenAI:
    """构造视觉解析 LLM（经 config.get_vision_llm，配置来自 .env 的 VISION_*）。

    max_retries=0：重试交给 _invoke_llm 的本地指数退避，避免双份重试。
    """
    return get_vision_llm(
        temperature=0.0,
        max_tokens=MAX_TOKENS,
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def _fix_math(text: str) -> str:
    """修复模型输出中导致 KaTeX 报错的 $ 写法：把成对的 $$ 块折叠为单个 $。

    注意不能只把 $$ 各自替换成 $：那会留下跨行的 $...$，而主流渲染器的
    行内公式模式（如 ``$([^$\n]+?)$``）不允许换行，块级公式反而渲染失败。
    因此折叠时必须把 $$ 之间及其内部的换行一并折成空格，收成单行行内公式。
    """
    return re.sub(
        r"\$\$\s*(.*?)\s*\$\$",
        lambda m: "$" + re.sub(r"\s*\n\s*", " ", m.group(1)) + "$",
        text,
        flags=re.S,
    )


# 端点若以这些 finish_reason 收尾，说明输出被 max_tokens 截断，而非正常结束
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


def _find_finish_reason(payload: Any, _depth: int = 0) -> str | None:
    """从响应对象/元数据里尽力取出 finish_reason（各端点字段位置不一）。

    只做有限深度遍历；取不到返回 None——「无法判断」绝不当作截断，
    以免在不上报该字段的端点上把正常输出误判为失败。
    """
    if _depth > 4:
        return None
    if isinstance(payload, (list, tuple)):
        children: list[Any] = list(payload)
    elif isinstance(payload, dict):
        value = payload.get("finish_reason")
        if isinstance(value, str) and value:
            return value.lower()
        children = list(payload.values())
    elif hasattr(payload, "response_metadata"):   # LangChain AIMessage / Generation 等
        value = getattr(payload, "finish_reason", None)
        if isinstance(value, str) and value:
            return value.lower()
        children = [getattr(payload, "response_metadata", None),
                    getattr(payload, "generation_info", None)]
    else:
        return None
    for child in children:
        found = _find_finish_reason(child, _depth + 1)
        if found:
            return found
    return None


def _token_usage_brief(response: Any) -> str:
    """把响应的 token 用量整理成一行可读文本（缺失项略过）。

    单独暴露 reasoning_tokens 是刻意的：该端点把它计入 completion_tokens，
    思考量一大就挤占 max_tokens 并把正文挤掉——这正是「残页」事故的主因。
    """
    meta = getattr(response, "response_metadata", None)
    usage = dict(meta.get("token_usage") or {}) if isinstance(meta, dict) else {}
    summary = getattr(response, "usage_metadata", None)
    summary = summary if isinstance(summary, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    out_details = summary.get("output_token_details")
    out_details = out_details if isinstance(out_details, dict) else {}
    pairs = (
        ("prompt", usage.get("prompt_tokens", summary.get("input_tokens"))),
        ("output", usage.get("completion_tokens", summary.get("output_tokens"))),
        ("reasoning", details.get("reasoning_tokens", out_details.get("reasoning"))),
    )
    return ", ".join(f"{k}={v}" for k, v in pairs if v is not None) or "未知"


def _has_suspected_truncation(content: str) -> bool:
    """页文本是否短到「疑似截断」，需要同页重试一次。

    只看 (BLANK_PAGE_CHARS, MIN_PAGE_CHARS) 开区间：封面/近空白页（各讲首页
    只有一行标题，实测 30~51 字符）落在下界以内，属正常输出，对它重试纯属浪费
    模型调用；而真正的正文页若只吐出百来字，几乎必然是截断（历史事故 321 字符）。
    """
    return BLANK_PAGE_CHARS < len(content) < MIN_PAGE_CHARS


def _invoke_llm(llm: ChatOpenAI, messages: list[HumanMessage],
                meter: Any | None = None) -> str:
    """调用视觉 LLM 并带指数退避重试，返回文本结果。

    meter 为可选的 TokenMeter 兼容对象（只需有 .add(response) 方法，
    见 main.TokenMeter），用于累计本次真实 token 消耗。

    每次调用都记录 finish_reason 与 token 用量（含 reasoning_tokens）：
    这两项缺失正是历史事故「日志里只有输出字符数、看不出截断」的根因。
    端点若报告 finish_reason 为 length/max_tokens，说明正文被 max_tokens
    截断，按可重试失败处理——残页绝不能返回给调用方、更不能写入页缓存。
    """
    last_error: Exception | None = None
    for attempt in range(LLM_RETRIES + 1):
        started = time.perf_counter()
        try:
            response = llm.invoke(messages)
            if meter is not None:
                meter.add(response)
            content = response.content
            text = content.strip() if isinstance(content, str) else str(content or "").strip()
            finish = _find_finish_reason(response)
            log.debug(
                "    LLM 响应：耗时 %.1fs，输出 %d 字符，finish_reason=%s，tokens(%s)"
                "（第 %d/%d 次尝试）",
                time.perf_counter() - started, len(text), finish or "未上报",
                _token_usage_brief(response), attempt + 1, LLM_RETRIES + 1,
            )
            if finish in _TRUNCATED_FINISH_REASONS:
                raise RuntimeError(
                    f"输出被 max_tokens 截断（finish_reason={finish}，已输出 {len(text)} 字符）"
                )
            return text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.debug(
                "    LLM 异常（第 %d/%d 次，耗时 %.1fs）：%s: %s",
                attempt + 1, LLM_RETRIES + 1, time.perf_counter() - started,
                type(exc).__name__, exc,
            )
            if attempt < LLM_RETRIES:
                wait = 2 * (attempt + 1)
                log.warning("    ! 调用失败，%ds 后重试：%s", wait, exc)
                time.sleep(wait)
    log.error("LLM 调用最终失败：%s", last_error)
    raise RuntimeError(f"LLM 调用最终失败：{last_error}")


def _pdf_id(pdf_path: Path) -> str:
    """PDF 唯一标识：PDF 文件内容 SHA-256 前 16 位（与 extract_pdf 同算法）。"""
    h = hashlib.sha256()
    h.update(pdf_path.read_bytes())
    return h.hexdigest()[:16]


def _render_page(page: pymupdf.Page, *, long_edge: int = MAX_LONG_EDGE) -> bytes:
    """把 PDF 单页渲染成 PNG 字节，长边不超过 long_edge。

    默认 MAX_LONG_EDGE(3000px) 是喂视觉模型的尺寸（手写小字需高分辨率）；
    传 DISPLAY_LONG_EDGE 则产出落盘给人看的「教材原图」。
    """
    rect = page.rect
    edge = max(rect.width, rect.height)
    zoom = min(MAX_ZOOM, long_edge / edge) if edge else 1.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _save_page_image(doc: pymupdf.Document, page_no: int, pdf_id: str) -> Path | None:
    """按显示尺寸渲染该页并落盘为「教材原图」（已存在则直接返回，不重渲染）。

    与文本提取链路完全解耦：不参与缓存命中判定、不受 _EXTRACT_VERSION 影响、
    不写任何页 Markdown（见 storage/image_store 模块 docstring 的两条硬约束）。
    渲染失败不阻断提取主流程，仅记 warning 并返回 None——原图属可选增强，
    不该让一次渲染异常毁掉整轮断点续跑的提取。
    """
    path = page_image_path(pdf_id, page_no)
    if path.exists():
        return path
    try:
        png = _render_page(doc.load_page(page_no - 1), long_edge=DISPLAY_LONG_EDGE)
        return write_page_image(pdf_id, page_no, png)
    except Exception:  # noqa: BLE001  可选产物：任何渲染/写盘异常都不中断提取
        log.warning("  ! 第 %d 页教材原图落盘失败（不影响文本提取）", page_no,
                    exc_info=True)
        return None


def _page_file_path(book_dir: Path, page_no: int) -> Path:
    """页码对应的缓存文件路径（含版本号，_EXTRACT_VERSION 递增即整体失效）。"""
    return book_dir / f"p{page_no:04d}_{_EXTRACT_VERSION}.md"


def _load_cached_pages(pdf_path: str | Path, *, output_dir: str | Path | None = None
                       ) -> dict[int, str]:
    """只读某 PDF 已有的逐页提取缓存，返回 {页码: Markdown 内容}；不调用任何模型。

    与 extract_pdf_pages_as_markdown 使用同一缓存目录/命名规则，供建库前的
    「规模预估」复用：pdf_id 需对 PDF 全文做一次 SHA-256（本地 IO，无模型开销）。

    Args:
        pdf_path: PDF 文件路径（须存在）。
        output_dir: 可选，覆盖默认缓存目录 output/pdf_extract。
    """
    pdf = Path(pdf_path)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF 文件不存在：{pdf_path}")
    cache_root = Path(output_dir) if output_dir else OUTPUT_DIR
    book_dir = cache_root / _pdf_id(pdf)
    out: dict[int, str] = {}
    if not book_dir.is_dir():
        return out
    for f in book_dir.glob(f"p*_{_EXTRACT_VERSION}.md"):
        m = re.match(r"^p(\d+)_", f.name)
        if not m:
            continue
        try:
            content = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            out[int(m.group(1))] = content
    return out


def _emit(progress: Any, event: dict) -> None:
    """向调用方进度回调推送一个事件；回调自身异常不得影响提取主流程。

    progress 为 None（CLI 默认）时直接返回，因此对既有调用方零开销、零行为变化。
    """
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # noqa: BLE001 - 进度上报属于旁路信息，绝不允许打断建库
        log.warning("[pdf_processor] progress 回调异常，已忽略: %s", event)


def extract_pdf_pages_as_markdown(
    pdf_path: str,
    start_page: int,
    end_page: int,
    *,
    output_dir: str | Path | None = None,
    meter: Any | None = None,
    max_new_calls: int | None = None,
    progress: Any | None = None,
) -> list[dict]:
    """截取 PDF 页面渲染成 PNG，用视觉大模型提取为结构化 Markdown。

    结果逐页缓存到 output/pdf_extract/{pdf_id}/p{页码}_{版本}.md：
    已提取（存在且非空）的页直接读缓存，不重复调用模型；
    中断重跑会自动跳过已完成页。缓存目录可通过 output_dir 覆盖
    （例如指向 extract_pdf 项目的 output 目录以复用其既有提取结果，
    但旧版命名的 p{页码}.md 不会被命中，等同于重新提取）。
    文件名带 _EXTRACT_VERSION：PROMPT / 渲染参数 / 后处理变更时递增版本号，
    旧缓存自动失效，无需手动删目录。

    Args:
        pdf_path: PDF 文件路径。
        start_page: 起始页码（从 1 计）。
        end_page: 结束页码（含），超出 PDF 总页数时自动截断。
        output_dir: 可选，覆盖默认缓存目录 output/pdf_extract。
        meter: 可选 TokenMeter 兼容对象（.add(response)），累计视觉调用真实 token。
        max_new_calls: 可选，本次最多新提取的页数上限（缓存命中的页不占额度）；
            达到上限即提前停止，剩余未提取页仍落在下次重跑（逐页缓存 + 自动跳过
            已完成页保证续跑），与 build_knowledge_bases 的 max_chunks 同一套模式。
            视觉模型通常是更贵的多模态输入，传一个有限上限可分批消费、控成本。
        progress: 可选回调 ``progress(event: dict)``，每处理完一页推一个
            ``{"stage": "vision", "event": "page_done", "page": N, "cached": bool,
            "done": i, "total": j}``，结束推 ``{"event": "vision_done", ...}``。
            供 HTTP API 以 SSE 向前端播报进度（见 api/routers/build.py）；
            CLI 不传即为 None，行为与之前完全一致。

    视觉模型固定由 config.py + sida-agent/.env 的 VISION_* 配置决定。

    Returns:
        pages_data: [{"page": 页码, "content": Markdown 文本}, ...]
    """
    # 视觉模型配置（全部来自 .env 的 VISION_*），缺项时提前给出可操作提示
    vision_cfg = resolve_llm_config(VISION_ROLE)
    missing = [k for k in ("base_url", "model_name", "api_key") if not vision_cfg[k]]
    if missing:
        msg = ("视觉模型配置不完整：请在 sida-agent/.env 设置 "
               + ", ".join(f"VISION_{m.upper()}" for m in missing) + "。")
        log.error("[pdf_processor] %s", msg)
        raise RuntimeError(msg)
    log.info("[pdf_processor] 视觉模型: model=%s base_url=%s",
             vision_cfg["model_name"], vision_cfg["base_url"])

    pdf = Path(pdf_path)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF 文件不存在：{pdf_path}")

    cache_root = Path(output_dir) if output_dir else OUTPUT_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    pdf_id = _pdf_id(pdf)
    book_dir = cache_root / pdf_id
    book_dir.mkdir(parents=True, exist_ok=True)

    vision_llm = _vision_llm()
    doc = pymupdf.open(pdf)
    try:
        total = doc.page_count
        start = max(1, start_page)
        end = min(total, end_page)
        if start > end:
            log.error("页范围 %d-%d 无效（PDF 共 %d 页）", start, end, total)
            raise ValueError(f"页范围 {start}-{end} 无效（PDF 共 {total} 页）。")

        log.info("[pdf_processor] PDF=%s（共 %d 页），提取第 %d-%d 页, id=%s",
                 pdf.name, total, start, end, pdf_id)
        _emit(progress, {"stage": "vision", "event": "start", "pdf_id": pdf_id,
                         "pdf_name": pdf.name, "start_page": start, "end_page": end,
                         "total_pages": end - start + 1})
        pages_data: list[dict] = []
        new_calls = 0
        # 教材原图旁路：先行、独立于下面的提取循环按显示尺寸为范围内每页落盘一张图，
        # 存在即跳过。刻意放在提取循环之外——补图是纯本地渲染、零模型成本，不该受
        # max_new_calls（约束的是视觉模型花费）影响；否则"已达上限 break"会让剩余页
        # 漏图。补图不写页 Markdown、不改版本号，因此不会使任何既有缓存失效。
        new_images = 0
        for page_no in range(start, end + 1):
            if not page_image_path(pdf_id, page_no).exists():
                if _save_page_image(doc, page_no, pdf_id) is not None:
                    new_images += 1
        log.info("[pdf_processor] 教材原图：本次新落盘 %d 张（第 %d-%d 页范围，"
                 "输出 output/pdf_images/%s/）", new_images, start, end, pdf_id)
        for page_no in range(start, end + 1):
            page_file = _page_file_path(book_dir, page_no)
            if page_file.exists() and page_file.read_text(encoding="utf-8").strip():
                content = page_file.read_text(encoding="utf-8").strip()
                cached_hit = True
                log.info("  [已提取] 第 %d 页（%s）", page_no, page_file.name)
            else:
                cached_hit = False
                # 达到本次新提取页上限即主动停：已完成页已落盘缓存，下次重跑
                # 会跳过它们、从第一个未提取页继续，等价分批消费视觉模型成本。
                if max_new_calls is not None and new_calls >= max_new_calls:
                    log.info("[pdf_processor] 已达本次新提取页上限 %d，停止（剩余页保留待下次续跑）",
                             max_new_calls)
                    _emit(progress, {"stage": "vision", "event": "capped",
                                     "max_new_calls": max_new_calls,
                                     "processed_pages": len(pages_data)})
                    break
                png = _render_page(doc.load_page(page_no - 1))
                messages = [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"},
                            },
                        ]
                    )
                ]
                log.info("  [提取] 第 %d 页 ...（缓存：%s）", page_no, page_file)
                content = _fix_math(_invoke_llm(vision_llm, messages, meter=meter))
                new_calls += 1
                if _has_suspected_truncation(content):
                    # 视觉模型偶发截断：同页原样重试一次，取两次里更完整的一份。
                    # 只重试一次——两次都短多半是页面本身稀疏，继续重试无益；此时
                    # 仍按结果落盘（宁可留个可疑页也不丢页），但打警告便于人工复核。
                    log.warning("  ! 第 %d 页：输出仅 %d 字符（<%d），疑似截断，同页重试一次",
                                page_no, len(content), MIN_PAGE_CHARS)
                    retry = _fix_math(_invoke_llm(vision_llm, messages, meter=meter))
                    new_calls += 1
                    if len(retry) > len(content):
                        content = retry
                    if _has_suspected_truncation(content):
                        log.warning("  ! 第 %d 页：重试后仍仅 %d 字符，疑漏抽；"
                                    "如确认缺失请删除该页缓存后重跑：%s",
                                    page_no, len(content), page_file)
                if content:
                    page_file.write_text(content, encoding="utf-8")
                else:
                    log.warning("  ! 第 %d 页：模型返回内容为空，未写入缓存", page_no)
            if content:
                pages_data.append({"page": page_no, "content": content})
            _emit(progress, {"stage": "vision", "event": "page_done", "page": page_no,
                             "cached": bool(cached_hit),
                             "done": len(pages_data),
                             "total_pages": end - start + 1})
        _emit(progress, {"stage": "vision", "event": "vision_done",
                         "processed_pages": len(pages_data),
                         "new_vision_calls": new_calls})
        return pages_data
    finally:
        doc.close()