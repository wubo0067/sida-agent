"""图谱全空时的「教材原文语义兜底」纯函数测试（见 README 6.16）。

覆盖三个口径（都不依赖 LLM / Chroma，故可直接构造 Document 断言）：
1. `_graph_context_empty` —— 图谱命中判据，决定「拒答」与「是否启用兜底」；
2. `_fallback_query_text` —— 兜底用什么文本检索（原话 vs 意图锚点）；
3. `_tag_page_header` / `_select_fallback_pages` —— 兜底页切片的标注与筛选。

运行：python -m unittest tests.test_vector_fallback
（tests/ 下无 __init__.py，不要用 unittest discover）
"""

import unittest

from langchain_core.documents import Document

from agent.workflow import (
    _VECTOR_FALLBACK_MIN_CHARS,
    _VECTOR_FALLBACK_TOP_K,
    _fallback_query_text,
    _graph_context_empty,
    _select_fallback_pages,
    _semantic_page_fallback,
    _tag_page_header,
)


def _doc(text: str) -> Document:
    return Document(page_content=text, metadata={})


def _page_doc(page: int, pdf_id: str = "pdf1", body: str = "x" * 400) -> Document:
    """构造一个带 pdf_id / page 元数据的 Page 切片（与 ingestion._build_page_docs 同形）。"""
    return Document(
        page_content=f"--- 第 {page} 页 ---\n\n{body}",
        metadata={"id": f"chemistry:Page:{pdf_id}:{page}", "subject": "chemistry",
                  "type": "Page", "pdf_id": pdf_id, "page": page},
    )


class FakeVectorStore:
    """只实现兜底用到的 similarity_search_with_score，并记录 filter 供断言。"""

    def __init__(self, hits=None, error=None):
        self.hits = hits or []
        self.error = error
        self.calls = []

    def similarity_search_with_score(self, query, k, filter=None):
        self.calls.append({"query": query, "k": k, "filter": filter})
        if self.error:
            raise self.error
        return self.hits


class GraphContextEmptyTests(unittest.TestCase):
    def test_empty_dict_and_none_are_empty(self):
        self.assertTrue(_graph_context_empty({}))
        self.assertTrue(_graph_context_empty(None))

    def test_all_buckets_empty_is_empty(self):
        self.assertTrue(_graph_context_empty({
            "concept": None, "concepts": [], "formulas": [], "experiments": [],
            "question_types": [], "methods": [], "examples": [],
        }))

    def test_only_prerequisites_is_still_empty(self):
        # 先修/后续/关联概念不构成「命中」：它们只有名字，不能支撑作答。
        self.assertTrue(_graph_context_empty({
            "concept": None, "prerequisites": [{"name": "元素"}],
            "follow_ups": [{"name": "同位素"}],
        }))

    def test_any_entity_bucket_counts_as_hit(self):
        # 锚点是公式/题型名而非概念名时 concept/concepts 恒为空，只看这两个会误判未命中。
        self.assertFalse(_graph_context_empty({"formulas": [{"name": "合成尿素"}]}))
        self.assertFalse(_graph_context_empty({"methods": [{"name": "差量法"}]}))
        self.assertFalse(_graph_context_empty({"examples": [{"id": "e1"}]}))

    def test_concept_and_concepts_count_as_hit(self):
        self.assertFalse(_graph_context_empty({"concept": {"name": "元素"}}))
        self.assertFalse(_graph_context_empty({"concepts": [{"name": "元素"}]}))

    def test_empty_concept_dict_is_not_a_hit(self):
        # 空 dict 与 None 同义（都表示没有该概念），必须同样触发兜底。
        self.assertTrue(_graph_context_empty({"concept": {}}))


class FallbackQueryTextTests(unittest.TestCase):
    def test_long_query_wins_over_concept(self):
        self.assertEqual(
            _fallback_query_text("如何制作尿素[CO(NH2)2]", "尿素的制备"),
            "如何制作尿素[CO(NH2)2]")

    def test_short_query_falls_back_to_concept(self):
        # 代词式追问（"那第二题呢"）原话几乎检索不到东西，改用意图锚点。
        self.assertEqual(_fallback_query_text("那第二题呢", "尿素的制备"), "尿素的制备")

    def test_short_query_without_concept_keeps_query(self):
        self.assertEqual(_fallback_query_text("怎么制备?", ""), "怎么制备?")

    def test_blank_inputs_return_empty(self):
        self.assertEqual(_fallback_query_text("", ""), "")
        self.assertEqual(_fallback_query_text(None, None), "")


class TagPageHeaderTests(unittest.TestCase):
    CHUNK = "--- 第 131 页 ---\n\n# 模块二\n\n正文"

    def test_header_gets_book_name(self):
        tagged = _tag_page_header(self.CHUNK, "二期B段9S全部笔记")
        self.assertTrue(tagged.startswith("--- 第 131 页（《二期B段9S全部笔记》） ---"))
        self.assertIn("# 模块二", tagged)

    def test_only_first_header_is_replaced(self):
        chunk = "--- 第 131 页 ---\n引用了 --- 第 131 页 ---\n"
        tagged = _tag_page_header(chunk, "书")
        self.assertEqual(tagged.count("（《书》）"), 1)

    def test_missing_book_or_header_returns_original(self):
        self.assertEqual(_tag_page_header(self.CHUNK, None), self.CHUNK)
        self.assertEqual(_tag_page_header(self.CHUNK, ""), self.CHUNK)
        self.assertEqual(_tag_page_header("没有页码头", "书"), "没有页码头")


class SelectFallbackPagesTests(unittest.TestCase):
    def test_short_pages_are_skipped_and_order_kept(self):
        short = _doc("--- 第 1 页 ---\n封面")
        hits = [(short, 0.1), (_doc("A" * 500), 0.2), (_doc("B" * 500), 0.3)]
        picked = _select_fallback_pages(hits)
        self.assertEqual([d.page_content[:1] for d in picked], ["A", "B"])
        self.assertNotIn(short, picked)

    def test_result_is_capped_at_top_k(self):
        hits = [(_doc(chr(65 + i) * 500), float(i)) for i in range(6)]
        picked = _select_fallback_pages(hits)
        self.assertEqual(len(picked), _VECTOR_FALLBACK_TOP_K)

    def test_boundary_length_is_kept(self):
        exact = _doc("x" * _VECTOR_FALLBACK_MIN_CHARS)
        below = _doc("x" * (_VECTOR_FALLBACK_MIN_CHARS - 1))
        self.assertEqual(_select_fallback_pages([(exact, 0.1)]), [exact])
        self.assertEqual(_select_fallback_pages([(below, 0.1)]), [])

    def test_plain_documents_without_score_are_accepted(self):
        doc = _doc("y" * 500)
        self.assertEqual(_select_fallback_pages([doc]), [doc])

    def test_empty_and_none_return_empty_list(self):
        self.assertEqual(_select_fallback_pages([]), [])
        self.assertEqual(_select_fallback_pages(None), [])


class SemanticPageFallbackTests(unittest.TestCase):
    def test_missing_vector_db_or_text_returns_empty(self):
        self.assertEqual(_semantic_page_fallback(None, {}, "chemistry", "尿素", ""),
                         ([], []))
        store = FakeVectorStore([(_page_doc(131), 0.7)])
        self.assertEqual(_semantic_page_fallback(store, {}, "chemistry", "", ""),
                         ([], []))
        self.assertEqual(store.calls, [])

    def test_page_subject_filter_and_top_k(self):
        store = FakeVectorStore([(_page_doc(131), 0.7), (_page_doc(18), 1.0)])
        _semantic_page_fallback(store, {}, "chemistry", "如何制作尿素[CO(NH2)2]", "")
        call = store.calls[0]
        self.assertEqual(call["query"], "如何制作尿素[CO(NH2)2]")
        self.assertEqual(call["k"], 8)  # _VECTOR_FALLBACK_CANDIDATES
        self.assertEqual(call["filter"],
                         {"$and": [{"subject": "chemistry"}, {"type": "Page"}]})

    def test_short_query_uses_concept(self):
        store = FakeVectorStore([(_page_doc(131), 0.7)])
        _semantic_page_fallback(store, {}, "chemistry", "那第二题呢", "尿素的制备")
        self.assertEqual(store.calls[0]["query"], "尿素的制备")

    def test_chunks_are_tagged_with_book_name_and_refs_collected(self):
        store = FakeVectorStore([(_page_doc(131, "pdf1"), 0.7),
                                 (_page_doc(18, "pdf2"), 1.0)])
        chunks, refs = _semantic_page_fallback(
            store, {"pdf1": "二期B段9S全部笔记"}, "chemistry", "如何制作尿素", "")
        self.assertTrue(chunks[0].startswith("--- 第 131 页（《二期B段9S全部笔记》） ---"))
        # pdf2 未登记教材名 → 不标书名（模型回退到「见教材第 X 页」）
        self.assertTrue(chunks[1].startswith("--- 第 18 页 ---"))
        self.assertEqual(refs, [("pdf1", 131), ("pdf2", 18)])

    def test_short_pages_are_filtered_out(self):
        store = FakeVectorStore([(Document(page_content="--- 第 1 页 ---\n封面",
                                          metadata={"pdf_id": "pdf1", "page": 1}), 0.1)])
        self.assertEqual(
            _semantic_page_fallback(store, {}, "chemistry", "如何制作尿素", ""), ([], []))

    def test_search_exception_is_swallowed(self):
        store = FakeVectorStore(error=RuntimeError("boom"))
        self.assertEqual(
            _semantic_page_fallback(store, {}, "chemistry", "如何制作尿素", ""), ([], []))


if __name__ == "__main__":
    unittest.main()
