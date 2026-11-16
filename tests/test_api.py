from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from mini_dbms.api import create_app


class UvicornTestServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, ready: threading.Event) -> None:
        super().__init__(config)
        self.ready = ready

    async def startup(self, sockets: list | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


def _post(base_url: str, body: dict, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8")
    return _post_raw(base_url, data, timeout)


def _post_raw(base_url: str, data: bytes, timeout: float = 10.0):
    request = urllib.request.Request(
        f"{base_url}/query",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            body_bytes = exc.read()
            try:
                payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except json.JSONDecodeError:
                payload = {}
        return exc.code, payload


class _ApiTestBase(unittest.TestCase):
    workers = 2
    queue_size = 2
    max_sql_length = 100

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tables = Path(self.temp.name) / "tables"
        self.tables.mkdir()
        (self.tables / "students.csv").write_text(
            "id:int,name:string,seat:int\n", encoding="utf-8"
        )
        self.app = create_app(
            self.tables,
            workers=self.workers,
            queue_size=self.queue_size,
            max_sql_length=self.max_sql_length,
        )
        self.ready = threading.Event()
        self.server = UvicornTestServer(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=0,
                loop="asyncio",
                log_level="error",
            ),
            self.ready,
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.assertTrue(self.ready.wait(10))
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def _shutdown(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


class ApiValidationTests(_ApiTestBase):
    def test_sql_length_boundary_is_accepted_and_excess_rejected(self) -> None:
        sql = "SELECT * FROM students;".ljust(self.max_sql_length)
        status, payload = _post(self.base_url, {"sql": sql})
        self.assertEqual(status, 200)
        self.assertEqual(payload["rows"], [])
        status, payload = _post(self.base_url, {"sql": sql + " "})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_non_object_json_uses_consistent_error_envelope(self) -> None:
        for body in (b"[]", b"null", b"42", b'"text"'):
            with self.subTest(body=body):
                status, payload = _post_raw(self.base_url, body)
                self.assertEqual(status, 400)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["error"]["code"], "bad_request")

    def test_invalid_json_returns_error(self) -> None:
        status, payload = _post_raw(self.base_url, b"not json")
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_missing_sql_field_returns_error(self) -> None:
        status, payload = _post(self.base_url, {})
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_sql_not_string_returns_error(self) -> None:
        status, payload = _post(self.base_url, {"sql": 123})
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_sql_too_long_returns_error(self) -> None:
        status, payload = _post(
            self.base_url, {"sql": "SELECT * FROM students;" + "x" * 200}
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_multiple_statements_rejected_without_mutation(self) -> None:
        original = (self.tables / "students.csv").read_bytes()
        status, payload = _post(
            self.base_url,
            {
                "sql": "INSERT INTO students (name, seat) VALUES ('a', 1); SELECT * FROM students;"
            },
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "syntax_error")
        self.assertEqual((self.tables / "students.csv").read_bytes(), original)

    def test_parse_error_returns_syntax_error(self) -> None:
        status, payload = _post(self.base_url, {"sql": "INVALID SQL;"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "syntax_error")


class ApiQueryTests(_ApiTestBase):
    max_sql_length = 10_000

    def test_insert_returns_success_and_id(self) -> None:
        status, payload = _post(
            self.base_url,
            {"sql": "INSERT INTO students (name, seat) VALUES ('민수', 20);"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["id"], 1)

    def test_select_returns_columns_and_rows(self) -> None:
        _post(
            self.base_url,
            {"sql": "INSERT INTO students (name, seat) VALUES ('철수', 21);"},
        )
        status, payload = _post(
            self.base_url, {"sql": "SELECT * FROM students WHERE id = 1;"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["columns"], ["id", "name", "seat"])
        self.assertEqual(payload["rows"], [[1, "철수", 21]])

    def test_id_select_uses_btree_index(self) -> None:
        _post(
            self.base_url,
            {"sql": "INSERT INTO students (name, seat) VALUES ('영희', 22);"},
        )
        state = self.app.state.catalog.get("students")
        original_find = state.index.find
        called = threading.Event()

        def tracking_find(key: int):
            called.set()
            return original_find(key)

        state.index.find = tracking_find
        status, payload = _post(
            self.base_url, {"sql": "SELECT * FROM students WHERE id = 1;"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(called.wait(5))

    def test_select_specific_columns(self) -> None:
        _post(
            self.base_url,
            {"sql": "INSERT INTO students (name, seat) VALUES ('a', 1);"},
        )
        status, payload = _post(
            self.base_url, {"sql": "SELECT name, seat FROM students WHERE id = 1;"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["columns"], ["name", "seat"])
        self.assertEqual(payload["rows"], [["a", 1]])


class ApiPoolTests(_ApiTestBase):
    workers = 1
    queue_size = 0
    max_sql_length = 10_000

    def test_pool_full_returns_503(self) -> None:
        release = threading.Event()
        inside = threading.Event()
        real_execute = self.app.state.executor.execute

        def blocking_execute(command, *, use_index=True):
            inside.set()
            release.wait(10)
            return real_execute(command, use_index=use_index)

        self.app.state.executor.execute = blocking_execute

        def first_request() -> tuple[int, dict]:
            return _post(
                self.base_url,
                {"sql": "INSERT INTO students (name, seat) VALUES ('x', 1);"},
            )

        first = threading.Thread(target=first_request)
        first.start()
        try:
            self.assertTrue(inside.wait(5))
            status, payload = _post(
                self.base_url,
                {"sql": "INSERT INTO students (name, seat) VALUES ('y', 2);"},
            )
            self.assertEqual(status, 503)
            self.assertFalse(payload["success"])
            self.assertEqual(payload["error"]["code"], "service_unavailable")
        finally:
            release.set()
            first.join(5)


class ApiConcurrencyTests(_ApiTestBase):
    workers = 4
    queue_size = 4
    max_sql_length = 10_000

    def test_concurrent_inserts_have_unique_ids(self) -> None:
        count = 8
        barrier = threading.Barrier(count)
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait(5)
            status, payload = _post(
                self.base_url,
                {
                    "sql": f"INSERT INTO students (name, seat) VALUES ('user{i}', {i});"
                },
            )
            with lock:
                results.append((status, payload))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(len(results), count)
        ids = []
        for status, payload in results:
            self.assertEqual(status, 200)
            self.assertTrue(payload["success"])
            ids.append(payload["id"])
        self.assertEqual(len(set(ids)), count)
        self.assertEqual(sorted(ids), list(range(1, count + 1)))

        status, payload = _post(
            self.base_url, {"sql": "SELECT id FROM students;"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["rows"]), count)


class ApiRestartTests(unittest.TestCase):
    def test_restart_preserves_data(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tables = Path(temp.name) / "tables"
        tables.mkdir()
        (tables / "students.csv").write_text(
            "id:int,name:string,seat:int\n", encoding="utf-8"
        )

        def start_server(tables_path: Path):
            app = create_app(tables_path, workers=2, queue_size=2)
            ready = threading.Event()
            server = UvicornTestServer(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=0,
                    loop="asyncio",
                    log_level="error",
                ),
                ready,
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(10))
            port = server.servers[0].sockets[0].getsockname()[1]
            return app, server, thread, f"http://127.0.0.1:{port}"

        app1, server1, thread1, url1 = start_server(tables)
        try:
            status, payload = _post(
                url1,
                {"sql": "INSERT INTO students (name, seat) VALUES ('재시작', 99);"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["id"], 1)
        finally:
            server1.should_exit = True
            thread1.join(5)

        app2, server2, thread2, url2 = start_server(tables)
        try:
            status, payload = _post(
                url2, {"sql": "SELECT * FROM students WHERE id = 1;"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["rows"], [[1, "재시작", 99]])
            status, payload = _post(
                url2,
                {"sql": "INSERT INTO students (name, seat) VALUES ('next', 1);"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["id"], 2)
        finally:
            server2.should_exit = True
            thread2.join(5)


if __name__ == "__main__":
    unittest.main()
