import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document

from ingestion import (
    _audit_book_versions,
    _build_vector_docs,
    _gather_known_context,
    _load_build_state,
    _normalize_extracted,
    _persist_chunk,
    _save_build_state,
    _validate_pdf_identity,
)
from storage.graph_store import K_CONCEPT, K_PDF_SOURCE, ScienceGraphStore


class FakeVectorStore:
    def __init__(self, hits):
        self.hits = hits

    def similarity_search(self, query, k, filter):
        return self.hits


class FailingVectorStore:
    def add_documents(self, documents, ids):
        raise RuntimeError("simulated vector failure")


class IngestionTests(unittest.TestCase):
    def test_source_page_is_normalized_and_invalid_value_removed(self):
        data = {
            "examples": [
                {"id": "ok", "source": {"page": "P34"}},
                {"id": "bad", "source": {"page": "34-36"}},
            ]
        }
        _normalize_extracted(data)
        self.assertEqual(data["examples"][0]["source"]["page"], 34)
        self.assertNotIn("page", data["examples"][1]["source"])

    def test_vector_docs_merge_duplicate_ids_and_omit_none_page(self):
        docs = _build_vector_docs(
            "math",
            {
                "concepts": [
                    {"name": "同名", "description": "第一段"},
                    {"name": "同名", "description": "第二段"},
                ],
                "examples": [
                    {"id": "例题", "title": "无页码", "source": {}},
                ],
            },
            pdf_id="pdf1",
        )
        concept_docs = [d for d in docs if d.metadata["type"] == K_CONCEPT]
        example_docs = [d for d in docs if d.metadata["type"] == "Example"]
        self.assertEqual(len(concept_docs), 1)
        self.assertIn("第二段", concept_docs[0].page_content)
        self.assertEqual(len(example_docs), 1)
        self.assertNotIn("page", example_docs[0].metadata)

    def test_known_context_prefers_structured_name_with_colon(self):
        hit = Document(
            page_content="概念：欧姆定律：变式\n定义",
            metadata={"id": "math:Concept:欧姆定律：变式", "type": K_CONCEPT},
        )
        chapters, concepts = _gather_known_context(
            FakeVectorStore([hit]),  # type: ignore[arg-type]  # 测试替身，仅需 add_documents/similarity_search
            ScienceGraphStore(),
            "math",
            "query",
        )
        self.assertEqual(chapters, [])
        self.assertEqual(concepts[0][0], "欧姆定律：变式")

    def test_pdf_id_required_for_registered_book(self):
        store = ScienceGraphStore()
        store.graph.add_node("meta:PdfSource:book", type=K_PDF_SOURCE, subject="meta")
        with self.assertRaises(ValueError):
            _validate_pdf_identity(store, None)
        _validate_pdf_identity(store, "pdf1")

    def test_build_state_is_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "build_state.json"
            state = {"chunks": {"math|pdf1|abc": {"status": "completed"}}}
            with patch("ingestion._BUILD_STATE_PATH", state_path):
                _save_build_state(state)
                self.assertEqual(_load_build_state(), state)
                self.assertFalse(state_path.with_suffix(".tmp").exists())

    def test_persist_chunk_writes_no_graph_on_vector_failure(self):
        store = ScienceGraphStore()
        data = {"concepts": [{"name": "待回滚", "description": "内容"}]}
        with self.assertRaises(RuntimeError):
            _persist_chunk(
                [{"page": 1, "content": "内容"}],
                "math",
                FailingVectorStore(),  # type: ignore[arg-type]  # 测试替身，模拟向量库写入失败
                store,
                data,
                pdf_id="pdf1",
            )
        self.assertNotIn("math:Concept:待回滚", store.graph)

    def test_audit_warns_when_book_gets_another_version(self):
        store = ScienceGraphStore()
        store.register_pdf_name("old", "二次函数最值")
        with patch("ingestion.log") as fake_log:
            _audit_book_versions(store, "new", "二次函数最值")
        self.assertTrue(fake_log.warning.called)
        self.assertIn("old", str(fake_log.warning.call_args))

    def test_audit_stays_silent_for_new_book_or_existing_version(self):
        store = ScienceGraphStore()
        store.register_pdf_name("old", "二次函数最值")
        for pdf_id, name in (("old", "二次函数最值"), ("fresh", "初中化学方程式")):
            with patch("ingestion.log") as fake_log:
                _audit_book_versions(store, pdf_id, name)
            self.assertFalse(fake_log.warning.called)

    def test_audit_respects_explicit_book_id(self):
        # 显式指定不同 book_id 即"另一本教材"，不应再提示同名多版本
        store = ScienceGraphStore()
        store.register_pdf_name("old", "9S合并PDF-1", book_id="9s合并pdf")
        with patch("ingestion.log") as fake_log:
            _audit_book_versions(store, "new", "9S合并PDF-完整", "9s合并pdf-完整")
        self.assertFalse(fake_log.warning.called)


if __name__ == "__main__":
    unittest.main()
