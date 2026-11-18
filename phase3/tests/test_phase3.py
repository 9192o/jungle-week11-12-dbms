import logging
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from concurrency import PoolClosed, PoolFull, WorkPool
from storage import CsvStorage


HEADER = b"record_id:int,name:string,seat:int\n"
INSERT = "INSERT INTO users (name, seat) VALUES ('A;B', 20);"
SELECT = "SELECT * FROM users WHERE record_id = 1;"
TIMEOUT = 5


class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.csv = self.directory / "users.csv"
        self.csv.write_bytes(HEADER)

    def check_error(self, reply, status, code):
        self.assertEqual(reply.status_code, status, reply.text)
        body = reply.json()
        self.assertEqual(set(body), {"success", "error"})
        self.assertIs(body["success"], False)
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertTrue(body["error"]["message"])

    def test_response_contract_and_restart(self):
        with TestClient(create_app(self.directory)) as client:
            reply = client.post("/query", json={"sql": INSERT})
            self.assertEqual((reply.status_code, reply.json()), (201, {
                "success": True, "record_id": "1",
            }))
        with TestClient(create_app(self.directory)) as client:
            reply = client.post("/query", json={"sql": SELECT})
            self.assertEqual((reply.status_code, reply.json()), (200, {
                "success": True,
                "columns": ["record_id", "name", "seat"],
                "rows": [["1", "A;B", "20"]],
            }))
            empty = client.post("/query", json={"sql": SELECT.replace("= 1", "= 0")})
            self.assertEqual(empty.json()["rows"], [])
            reply = client.post("/query", json={"sql": INSERT})
            self.assertEqual(reply.json()["record_id"], "2")

    def test_validation_precedes_enqueue(self):
        application = create_app(self.directory)
        with TestClient(application) as client:
            with patch.object(application.state.pool, "submit", side_effect=AssertionError):
                self.check_error(client.post("/query", content="{"), 400, "INVALID_REQUEST")
                for body in ([], {}, {"sql": 123}):
                    self.check_error(client.post("/query", json=body), 400, "INVALID_REQUEST")
                for sql in (" ; ", INSERT + SELECT):
                    self.check_error(client.post("/query", json={"sql": sql}), 400, "INVALID_STATEMENT_COUNT")
                sql = " " * (4097 - len(SELECT)) + SELECT
                self.check_error(client.post("/query", json={"sql": sql}), 413, "SQL_TOO_LONG")
            sql = " " * (4096 - len(SELECT)) + SELECT
            self.assertEqual(client.post("/query", json={"sql": sql}).status_code, 200)
        self.assertEqual(self.csv.read_bytes(), HEADER)

    def test_error_codes_and_unsigned_precision(self):
        self.csv.write_bytes(HEADER + b"18446744073709551615,large,18446744073709551614\n")
        with TestClient(create_app(self.directory)) as client:
            for sql, status, code in (
                ("SELECT * FROM users WHERE seat > 0;", 400, "SQL_SYNTAX_ERROR"),
                ("SELECT * FROM users WHERE seat = -1;", 400, "INPUT_CONSTRAINT_ERROR"),
                ("SELECT unknown FROM users;", 400, "INPUT_CONSTRAINT_ERROR"),
                ("SELECT * FROM other;", 404, "TABLE_NOT_FOUND"),
                (INSERT, 400, "INPUT_CONSTRAINT_ERROR"),
            ):
                self.check_error(client.post("/query", json={"sql": sql}), status, code)
            reply = client.post("/query", json={"sql": SELECT.replace("= 1", "= 18446744073709551615")})
            self.assertEqual(reply.json()["rows"], [["18446744073709551615", "large", "18446744073709551614"]])
            with patch.object(CsvStorage, "read_indexed_row", side_effect=OSError("private.csv")):
                with self.assertLogs("app", logging.ERROR):
                    reply = client.post("/query", json={"sql": "SELECT * FROM users WHERE record_id = 18446744073709551615;"})
                self.check_error(reply, 500, "STORAGE_ERROR")
                self.assertNotIn("private.csv", reply.text)

    def test_startup_isolation_and_total_failure(self):
        (self.directory / "bad.csv").write_bytes(b"invalid header\r\n")
        application = create_app(self.directory, catalog={"bad": "bad.csv", "users": "users.csv"})
        with self.assertLogs("app", logging.ERROR):
            with TestClient(application) as client:
                self.check_error(
                    client.post("/query", json={"sql": "SELECT * FROM bad;"}), 503, "TABLE_UNAVAILABLE"
                )
                self.assertEqual(client.post("/query", json={"sql": SELECT}).status_code, 200)
        with self.assertLogs("app", logging.ERROR), self.assertRaises(RuntimeError):
            with TestClient(create_app(self.directory, catalog={"bad": "bad.csv"})):
                self.fail("server started without a usable table")

    def test_write_failure_restores_or_blocks_before_next_query(self):
        def fail_after_partial_write(file, payload, eof):
            file.seek(eof)
            file.write(payload[:3])
            raise OSError("internal write failure")

        with TestClient(create_app(self.directory)) as client:
            with patch("storage.append_record", fail_after_partial_write), self.assertLogs("app", logging.ERROR):
                reply = client.post("/query", json={"sql": INSERT})
                self.check_error(reply, 500, "STORAGE_ERROR")
            self.assertEqual(self.csv.read_bytes(), HEADER)
            self.assertEqual(client.post("/query", json={"sql": INSERT}).json()["record_id"], "1")
            with patch("storage.append_record", fail_after_partial_write), patch(
                "storage.restore_eof", side_effect=OSError("internal truncate failure")
            ), self.assertLogs("app", logging.ERROR):
                reply = client.post("/query", json={"sql": INSERT})
                self.check_error(reply, 503, "TABLE_UNAVAILABLE")
            self.check_error(client.post("/query", json={"sql": SELECT}), 503, "TABLE_UNAVAILABLE")

    def test_pool_saturation_returns_503_and_recovers(self):
        application = create_app(self.directory, workers=1, queue_size=1)
        entered = threading.Event()
        queued = threading.Event()
        release = threading.Event()
        real_execute = CsvStorage.execute

        def hold(storage, command):
            entered.set()
            if not release.wait(TIMEOUT):
                raise TimeoutError("held query was not resumed")
            return real_execute(storage, command)

        with TestClient(application) as client, ThreadPoolExecutor(max_workers=2) as callers:
            original_submit = application.state.pool.submit

            def observe_submit(function, statement, *args):
                result = original_submit(function, statement, *args)
                if "WHERE" not in statement:
                    queued.set()
                return result

            with patch.object(CsvStorage, "execute", hold), patch.object(application.state.pool, "submit", observe_submit):
                try:
                    first = callers.submit(client.post, "/query", json={"sql": SELECT})
                    self.assertTrue(entered.wait(TIMEOUT))
                    second = callers.submit(client.post, "/query", json={"sql": "SELECT * FROM users;"})
                    self.assertTrue(queued.wait(TIMEOUT))
                    self.check_error(client.post("/query", json={"sql": INSERT}), 503, "POOL_FULL")
                finally:
                    release.set()
                self.assertEqual(first.result(TIMEOUT).status_code, 200)
                self.assertEqual(second.result(TIMEOUT).status_code, 200)
            self.assertEqual(client.post("/query", json={"sql": INSERT}).status_code, 201)

    def test_writer_waits_for_reader_and_blocks_new_reader(self):
        self.csv.write_bytes(HEADER + b"1,old,10\n")
        (self.directory / "other.csv").write_bytes(HEADER + b"1,other,15\n")
        application = create_app(self.directory, workers=4, catalog={
            "users": "users.csv", "other": "other.csv",
        })
        entered = threading.Event()
        writer_waiting = threading.Event()
        reader_waiting = threading.Event()
        write_entered = threading.Event()
        release_read = threading.Event()
        release_write = threading.Event()
        real_read = CsvStorage.read_indexed_row
        real_insert = CsvStorage.insert

        def hold_first(storage, table, key, offset):
            if table.path.name == "users.csv" and key == 1:
                entered.set()
                if not release_read.wait(TIMEOUT):
                    raise TimeoutError("held reader was not resumed")
            return real_read(storage, table, key, offset)

        def hold_write(storage, name, command, table):
            write_entered.set()
            if not release_write.wait(TIMEOUT):
                raise TimeoutError("held writer was not resumed")
            return real_insert(storage, name, command, table)

        with TestClient(application) as client, ThreadPoolExecutor(max_workers=3) as callers:
            lock = application.state.locks["users"]
            real_wait = lock._condition.wait

            def observe_wait(*args, **kwargs):
                if lock._waiting_writers and lock._readers:
                    if writer_waiting.is_set():
                        reader_waiting.set()
                    else:
                        writer_waiting.set()
                return real_wait(*args, **kwargs)

            with patch.object(CsvStorage, "read_indexed_row", hold_first), patch.object(
                CsvStorage, "insert", hold_write
            ), patch.object(lock._condition, "wait", observe_wait):
                try:
                    first = callers.submit(client.post, "/query", json={"sql": SELECT})
                    self.assertTrue(entered.wait(TIMEOUT))
                    writer = callers.submit(client.post, "/query", json={"sql": INSERT})
                    self.assertTrue(writer_waiting.wait(TIMEOUT))
                    late = callers.submit(client.post, "/query", json={"sql": SELECT.replace("= 1", "= 2")})
                    self.assertTrue(reader_waiting.wait(TIMEOUT))
                    other = client.post("/query", json={"sql": SELECT.replace("users", "other")})
                    self.assertEqual(other.json()["rows"], [["1", "other", "15"]])
                    self.assertFalse(write_entered.is_set())
                    release_read.set()
                    self.assertTrue(write_entered.wait(TIMEOUT))
                    self.assertFalse(late.done())
                finally:
                    release_read.set()
                    release_write.set()
                self.assertEqual(first.result(TIMEOUT).json()["rows"], [["1", "old", "10"]])
                self.assertEqual(writer.result(TIMEOUT).json()["record_id"], "2")
                self.assertEqual(late.result(TIMEOUT).json()["rows"], [["2", "A;B", "20"]])

    def test_parallel_reads_and_unique_concurrent_inserts(self):
        self.csv.write_bytes(HEADER + b"1,first,10\n2,second,20\n")
        entered = threading.Event()
        release = threading.Event()
        real_read = CsvStorage.read_indexed_row

        def hold_first(storage, table, key, offset):
            if key == 1:
                entered.set()
                if not release.wait(TIMEOUT):
                    raise TimeoutError("held reader was not resumed")
            return real_read(storage, table, key, offset)

        with TestClient(create_app(self.directory)) as client, ThreadPoolExecutor(max_workers=8) as callers:
            with patch.object(CsvStorage, "read_indexed_row", hold_first):
                try:
                    first = callers.submit(client.post, "/query", json={"sql": SELECT})
                    self.assertTrue(entered.wait(TIMEOUT))
                    second = callers.submit(client.post, "/query", json={"sql": SELECT.replace("= 1", "= 2")})
                    self.assertEqual(second.result(TIMEOUT).json()["rows"], [["2", "second", "20"]])
                finally:
                    release.set()
                self.assertEqual(first.result(TIMEOUT).json()["rows"], [["1", "first", "10"]])
            writes = [callers.submit(client.post, "/query", json={"sql": INSERT}) for _ in range(8)]
            ids = set()
            for write in writes:
                reply = write.result(TIMEOUT)
                self.assertEqual(reply.status_code, 201)
                ids.add(int(reply.json()["record_id"]))
            self.assertEqual(ids, set(range(3, 11)))
            for record_id in ids:
                reply = client.post("/query", json={"sql": SELECT.replace("= 1", f"= {record_id}")})
                self.assertEqual(reply.json()["rows"], [[str(record_id), "A;B", "20"]])


class PoolTests(unittest.TestCase):
    def test_cancelled_queue_slot_reused_and_shutdown_drains(self):
        pool = WorkPool(1, 1)
        blocker = threading.Event()
        started = threading.Event()
        queued_ran = threading.Event()

        def hold():
            started.set()
            if not blocker.wait(TIMEOUT):
                raise TimeoutError

        running = pool.submit(hold)
        try:
            self.assertTrue(started.wait(TIMEOUT))
            queued = pool.submit(queued_ran.set)
            with self.assertRaises(PoolFull):
                pool.submit(lambda: None)
            self.assertFalse(running.cancel())
            self.assertTrue(queued.cancel())
            replacement = pool.submit(lambda: 42)
        finally:
            blocker.set()
            pool.shutdown()
        self.assertEqual(replacement.result(TIMEOUT), 42)
        self.assertFalse(queued_ran.is_set())
        with self.assertRaises(PoolClosed):
            pool.submit(lambda: None)


if __name__ == "__main__":
    unittest.main()
