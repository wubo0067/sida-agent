"""会话清单分页单测（chat_session.list_sessions / GET /chat/sessions）。

为什么单测：分页改动同时触碰三层（SQL 分组排序 / checkpoint 反序列化 / API
参数与响应），最易出的错是「页间重叠与遗漏」「total 与 limit 语义混淆」。单元
测试用临时 sqlite 造 N 个会话（每个 3 层历史 checkpoint），零 LLM 调用即可覆盖。

跑法（tests/ 无 __init__.py，必须带模块路径）：
    python -m unittest tests.test_chat_sessions
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

import chat_session
from chat_session import (
    _count_threads,
    _recent_thread_ids,
    list_sessions,
)


def _seed(db: Path, *, sessions: int, turns: int = 3) -> None:
    """造 sessions 个会话，每个 turns 层历史 checkpoint。

    checkpoint_id 用零填充的单调递增数字：i 越大越新，故第 i 个会话是第 i 新的
    （与旧实现按 ts 倒序同序，便于断言分页切片 == 全量切片）。
    """
    with SqliteSaver.from_conn_string(str(db)) as saver:
        for i in range(sessions):
            tid = "s-%012d" % i
            msgs = [HumanMessage("问题%d" % i), AIMessage("回答%d" % i)]
            for r in range(turns):
                cp = {
                    "id": "%032d" % (i * 10 + r),
                    "ts": "2026-09-%02dT00:00:00+00:00" % (r + 1),
                    "channel_values": {"messages": msgs * (r + 1)},
                }
                saver.put(
                    {"configurable": {"thread_id": tid, "checkpoint_ns": ""}},
                    cp,
                    {"source": "loop", "step": r, "writes": {}},
                    {},
                )


class _TempChatDb(unittest.TestCase):
    """把 chat_session 的库路径指向临时文件的测试基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "checkpoints.sqlite"
        self.addCleanup(self._tmp.cleanup)
        # chat_db_path() 是模块级函数：patch 它即可让 _count_threads /
        # _recent_thread_ids / open_saver 全部落到临时库
        self._p = patch.object(chat_session, "chat_db_path", lambda: self.db)
        self._p.start()
        self.addCleanup(self._p.stop)


class TestListSessionsPaging(_TempChatDb):
    def test_missing_db_returns_empty(self) -> None:
        # 库不存在：不抛错，返回空页与 0 总数
        self.assertEqual(list_sessions(limit=10), ([], 0))
        self.assertEqual(list_sessions(), ([], 0))

    def test_full_list_matches_page_concatenation(self) -> None:
        _seed(self.db, sessions=25)
        full, total = list_sessions()
        self.assertEqual(total, 25)
        self.assertEqual(len(full), 25)

        pages = [list_sessions(limit=10, offset=off)[0] for off in (0, 10, 20)]
        self.assertEqual([len(p) for p in pages], [10, 10, 5])
        # 页间不重不漏：拼接 == 全量
        self.assertEqual(
            [r["thread_id"] for p in pages for r in p], [r["thread_id"] for r in full]
        )

    def test_default_limit_applied_by_caller_not_core(self) -> None:
        # 核心层 limit=None 即「全部」；默认页大小是 API/CLI 调用方的责任
        _seed(self.db, sessions=7)
        rows, total = list_sessions(limit=None)
        self.assertEqual(len(rows), 7)
        self.assertEqual(total, 7)

    def test_offset_out_of_range_keeps_total(self) -> None:
        _seed(self.db, sessions=5)
        rows, total = list_sessions(limit=10, offset=99)
        self.assertEqual(rows, [])
        self.assertEqual(total, 5)  # 越界仍回真实总数，便于前端校正页码

    def test_limit_zero_returns_total_only(self) -> None:
        _seed(self.db, sessions=5)
        rows, total = list_sessions(limit=0)
        self.assertEqual(rows, [])
        self.assertEqual(total, 5)

    def test_negative_limit_and_offset_are_clamped(self) -> None:
        _seed(self.db, sessions=5)
        rows, total = list_sessions(limit=-3, offset=-7)
        self.assertEqual(rows, [])  # limit<=0 钳成 0 -> 只回总数
        self.assertEqual(total, 5)

    def test_page_is_ordered_by_recency(self) -> None:
        _seed(self.db, sessions=6)
        rows, _ = list_sessions(limit=6)
        # i 越大 checkpoint_id 越大 -> 越新；全量倒序即 i 递减
        self.assertEqual(
            [r["thread_id"] for r in rows], ["s-%012d" % i for i in (5, 4, 3, 2, 1, 0)]
        )

    def test_row_fields(self) -> None:
        _seed(self.db, sessions=1, turns=3)
        rows, _ = list_sessions(limit=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 最新 checkpoint 是第 3 层：messages = (问+答) * 3
        self.assertEqual(row["turns"], 3)
        self.assertEqual(row["first_question"], "问题0")
        self.assertTrue(row["updated_at"].startswith("2026-09-03"))
        self.assertGreater(row["chars"], 0)


class TestSqlHelpers(_TempChatDb):
    def test_count_threads(self) -> None:
        self.assertEqual(_count_threads(), 0)
        _seed(self.db, sessions=4)
        self.assertEqual(_count_threads(), 4)

    def test_recent_thread_ids_order_and_slice(self) -> None:
        _seed(self.db, sessions=6)
        ids = _recent_thread_ids(3, 0)
        self.assertEqual(ids, ["s-%012d" % i for i in (5, 4, 3)])
        self.assertEqual(_recent_thread_ids(2, 3), ["s-%012d" % i for i in (2, 1)])

    def test_recent_thread_ids_uses_sql_not_limit_scan(self) -> None:
        # 会话数远大于页大小时，只应把页大小的行交给反序列化层
        _seed(self.db, sessions=50)
        rows, total = list_sessions(limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(total, 50)

    def test_corrupt_db_is_swallowed(self) -> None:
        # 库损坏/非 sqlite：_count_threads / _recent_thread_ids 视为无会话
        self.db.write_bytes(b"not a sqlite database")
        self.assertEqual(_count_threads(), 0)
        self.assertEqual(_recent_thread_ids(10, 0), [])
        self.assertEqual(list_sessions(limit=10), ([], 0))


class TestApiSessionsEndpoint(_TempChatDb):
    """GET /chat/sessions 分页参数与响应字段（用 TestClient，零 LLM 调用）。"""

    def _client(self):
        # 延迟导入：api.app 的 lifespan 会加载图谱/向量库，代价高但可接受；
        # 在其他用例里已 patch 到临时会话库，故不会碰生产 chat 数据
        from fastapi.testclient import TestClient
        from api.app import app

        return TestClient(app)

    def test_endpoint_paging_contract(self) -> None:
        _seed(self.db, sessions=12)
        with self._client() as client:
            r = client.get("/chat/sessions", params={"limit": 5, "offset": 0})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["total"], 12)
            self.assertEqual(body["count"], 5)
            self.assertEqual(body["limit"], 5)
            self.assertEqual(body["offset"], 0)
            self.assertTrue(body["has_more"])
            self.assertEqual(len(body["sessions"]), 5)

            tail = client.get(
                "/chat/sessions", params={"limit": 5, "offset": 10}
            ).json()
            self.assertEqual(tail["count"], 2)
            self.assertFalse(tail["has_more"])

    def test_endpoint_default_limit_and_validation(self) -> None:
        _seed(self.db, sessions=3)
        with self._client() as client:
            # 不传 limit：回显 null（未分页），但服务端按省流默认页查询
            body = client.get("/chat/sessions").json()
            self.assertEqual(body["limit"], None)
            self.assertEqual(body["total"], 3)
            self.assertEqual(body["count"], 3)
            self.assertEqual(
                body["count"],
                len(
                    client.get("/chat/sessions", params={"limit": 50}).json()[
                        "sessions"
                    ]
                ),
            )
            # 边界校验：limit 越界 / offset 负数 -> 422
            self.assertEqual(
                client.get("/chat/sessions", params={"limit": 0}).status_code, 422
            )
            self.assertEqual(
                client.get("/chat/sessions", params={"limit": 201}).status_code, 422
            )
            self.assertEqual(
                client.get("/chat/sessions", params={"offset": -1}).status_code, 422
            )


if __name__ == "__main__":
    unittest.main()
