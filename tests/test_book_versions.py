"""教材版本管理（逻辑书聚合 / 当前版本切换）单元测试。

背景：pdf_id 是 PDF 内容哈希，同一本教材改一次就是一个新 id，注册表里因此
会出现多个同名条目，展示层看起来像"多了好几本书"。这里的测试锁定三件事：
1. 同名版本必须折叠成一本（零迁移，老数据靠惰性派生）；
2. 不同名的书默认不合并（不做"智能"猜测，要合并必须显式 book_id）；
3. 当前版本只在被显式指定时才有值，且切换只动登记表属性、不碰知识实体。
"""

import unittest

from storage.graph_store import (
    ScienceGraphStore,
    logical_book_id,
    node_book_id,
    node_key,
)

_PDF_NODE_PREFIX = "meta:PdfSource:"


class LogicalBookTests(unittest.TestCase):
    def _store(self, *entries) -> ScienceGraphStore:
        """entries 每项为 (pdf_id, name) 或 (pdf_id, name, book_id)。"""
        store = ScienceGraphStore()
        for entry in entries:
            pdf_id, name, *rest = entry
            store.register_pdf_name(pdf_id, name, book_id=rest[0] if rest else None)
        return store

    # ------------------------------------------------------ 分组与折叠
    def test_same_name_versions_fold_into_one_book(self):
        store = self._store(("p1", "二次函数最值"), ("p2", "二次函数最值"),
                            ("p3", "二次函数最值"))
        books = store.logical_books()
        self.assertEqual(len(books), 1)
        book = books[logical_book_id("二次函数最值")]
        self.assertEqual(book["version_count"], 3)
        self.assertEqual(book["name"], "二次函数最值")
        self.assertEqual([v["pdf_id"] for v in book["versions"]],
                         ["p1", "p2", "p3"])   # 无时间字段时按 pdf_id 稳定排序

    def test_versions_are_sorted_by_created_at(self):
        store = ScienceGraphStore()
        for pdf_id, created in (("p2", "2026-01-02T00:00:00"),
                                ("p1", "2026-01-01T00:00:00"),
                                ("p3", "2026-01-03T00:00:00")):
            store.register_pdf_name(pdf_id, "同名书")
            store.graph.nodes[node_key("meta", "PdfSource", pdf_id)]["created_at"] = created
        book = store.logical_books()[logical_book_id("同名书")]
        self.assertEqual([v["pdf_id"] for v in book["versions"]], ["p1", "p2", "p3"])

    def test_different_names_stay_separate_books(self):
        # 刻意不做文件名相似度猜测：9S合并PDF-1 与 9S合并PDF-完整 是两本
        store = self._store(("p1", "9S合并PDF-1"), ("p2", "9S合并PDF-完整"))
        self.assertEqual(len(store.logical_books()), 2)

    def test_explicit_book_id_merges_differently_named_versions(self):
        store = self._store(("p1", "9S合并PDF-1", "9s合并pdf"),
                            ("p2", "9S合并PDF-完整", "9s合并pdf"))
        books = store.logical_books()
        self.assertEqual(len(books), 1)
        self.assertEqual(books["9s合并pdf"]["version_count"], 2)

    def test_whitespace_and_case_differences_do_not_split_a_book(self):
        self.assertEqual(logical_book_id("  9S  合并 "), logical_book_id("9s 合并"))
        self.assertEqual(logical_book_id("  "), "")

    def test_legacy_nodes_without_book_id_are_grouped_by_name(self):
        # 老图谱没有 logical_book_id 属性：不做迁移也必须能折叠
        store = ScienceGraphStore()
        for pdf_id in ("old1", "old2"):
            store.graph.add_node(_PDF_NODE_PREFIX + pdf_id,
                                 type="PdfSource", subject="meta",
                                 pdf_id=pdf_id, name="初中物理重点概念大全")
        books = store.logical_books()
        self.assertEqual(len(books), 1)
        self.assertEqual(books[logical_book_id("初中物理重点概念大全")]["version_count"], 2)

    def test_unnamed_registrations_do_not_collapse_together(self):
        store = ScienceGraphStore()
        store.graph.add_node(_PDF_NODE_PREFIX + "a", type="PdfSource", subject="meta",
                             pdf_id="a", name="")
        store.graph.add_node(_PDF_NODE_PREFIX + "b", type="PdfSource", subject="meta",
                             pdf_id="b", name="")
        self.assertEqual(len(store.logical_books()), 2)

    def test_non_pdf_source_nodes_are_ignored(self):
        store = self._store(("p1", "书"))
        store.graph.add_node("math:Concept:概念", type="Concept", subject="math")
        self.assertEqual(len(store.logical_books()), 1)

    # ---------------------------------------------------------- 当前版本
    def test_single_version_is_implicitly_active(self):
        store = self._store(("p1", "独本"))
        self.assertEqual(store.logical_books()[logical_book_id("独本")]["active_pdf_id"], "p1")
        self.assertEqual(store.active_pdf_ids(), {"p1"})

    def test_multi_version_without_explicit_choice_is_unspecified(self):
        store = self._store(("p1", "同名"), ("p2", "同名"))
        self.assertIsNone(store.logical_books()[logical_book_id("同名")]["active_pdf_id"])
        self.assertEqual(store.active_pdf_ids(), set())

    def test_set_active_version_switches_only_within_the_group(self):
        store = self._store(("p1", "同名"), ("p2", "同名"), ("other", "别的书"))
        self.assertEqual(store.set_active_version("p2"), logical_book_id("同名"))

        books = store.logical_books()
        book = books[logical_book_id("同名")]
        self.assertEqual(book["active_pdf_id"], "p2")
        self.assertEqual({v["pdf_id"]: v["is_active"] for v in book["versions"]},
                         {"p1": False, "p2": True})
        # 别的书不受影响（单版本继续隐式活跃）
        self.assertEqual(books[logical_book_id("别的书")]["active_pdf_id"], "other")
        self.assertEqual(store.active_pdf_ids(), {"p2", "other"})

        # 可反复切换，并把分组 id 固化到节点上
        store.set_active_version("p1")
        self.assertEqual(store.logical_books()[logical_book_id("同名")]["active_pdf_id"], "p1")
        self.assertEqual(node_book_id(store.graph.nodes[_PDF_NODE_PREFIX + "p2"]),
                         logical_book_id("同名"))

    def test_set_active_version_returns_none_for_unknown_pdf_id(self):
        store = self._store(("p1", "书"))
        self.assertIsNone(store.set_active_version("不存在"))
        self.assertEqual(store.logical_books()[logical_book_id("书")]["active_pdf_id"], "p1")

    # ------------------------------------------------------------ 注册表
    def test_reregister_keeps_created_at_and_updates_name(self):
        store = self._store(("p1", "旧名"))
        created = store.graph.nodes[_PDF_NODE_PREFIX + "p1"]["created_at"]
        store.register_pdf_name("p1", "新名")
        node = store.graph.nodes[_PDF_NODE_PREFIX + "p1"]
        self.assertEqual(node["name"], "新名")
        self.assertEqual(node["created_at"], created)
        self.assertEqual(node["logical_book_id"], logical_book_id("新名"))

    def test_register_ignores_empty_arguments(self):
        store = ScienceGraphStore()
        store.register_pdf_name("", "书")
        store.register_pdf_name("p1", "")
        self.assertEqual(store.pdf_names(), {})

    def test_pdf_names_still_returns_flat_registry(self):
        # 问答侧（agent/workflow._books_of）与图谱截断告警仍依赖这个扁平视图
        store = self._store(("p1", "同名"), ("p2", "同名"))
        self.assertEqual(store.pdf_names(), {"p1": "同名", "p2": "同名"})

    def test_registration_does_not_touch_knowledge_entities(self):
        store = self._store(("p1", "同名"))
        store.register_pdf_name("p2", "同名", is_active=True)
        self.assertEqual(
            sorted(n for n in store.graph.nodes if not n.startswith(_PDF_NODE_PREFIX)), [])


if __name__ == "__main__":
    unittest.main()
