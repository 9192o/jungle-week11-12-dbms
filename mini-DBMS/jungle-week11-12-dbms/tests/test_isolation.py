import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mini_dbms.catalog import Catalog
from mini_dbms.errors import StorageError
from mini_dbms.executor import Executor
from mini_dbms.models import Insert, Inserted, Select, Selected
from mini_dbms.storage import CsvStorage


class ExecutorIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for name in ("students", "other"):
            (self.directory / f"{name}.csv").write_bytes(b"id:int,name:string\n")
        self.catalog = Catalog(self.directory)
        self.executor = Executor(self.catalog)
        self.state = self.catalog.get("students")

    def observe_waiters(self, events: dict[str, threading.Event]):
        original = self.state.lock._condition.wait

        def wait(timeout: float | None = None) -> bool:
            name = threading.current_thread().name
            for prefix, event in events.items():
                if name.startswith(prefix):
                    event.set()
            return original(timeout)

        return patch.object(self.state.lock._condition, "wait", wait)

    def test_select_waits_for_complete_file_and_index_update(self) -> None:
        appended = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        original = CsvStorage.append_row

        def append(path, schema, row):
            offset = original(path, schema, row)
            appended.set()
            if not release.wait(5):
                raise TimeoutError("INSERT not released")
            return offset

        with (
            ThreadPoolExecutor(1, thread_name_prefix="writer") as writers,
            ThreadPoolExecutor(1, thread_name_prefix="reader") as readers,
            self.observe_waiters({"reader": waiting}),
            patch.object(CsvStorage, "append_row", append),
        ):
            insert = writers.submit(
                self.executor.execute, Insert("students", ["name"], ["complete"])
            )
            try:
                self.assertTrue(appended.wait(5))
                select = readers.submit(
                    self.executor.execute, Select("students", None, ("id", 1))
                )
                self.assertTrue(waiting.wait(5))
                self.assertIsNone(self.state.index.find(1))
                self.assertFalse(select.done())
            finally:
                release.set()
            self.assertEqual(insert.result(5), Inserted(1))
            self.assertEqual(
                select.result(5), Selected(["id", "name"], [[1, "complete"]])
            )

    def test_readers_see_restored_state_after_failed_insert(self) -> None:
        appended = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        original = CsvStorage.append_row
        before = self.state.path.read_bytes()

        def fail_after_append(path, schema, row):
            original(path, schema, row)
            appended.set()
            if not release.wait(5):
                raise TimeoutError("failed INSERT not released")
            raise OSError("injected failure after CSV append")

        with (
            ThreadPoolExecutor(1, thread_name_prefix="writer") as writers,
            ThreadPoolExecutor(1, thread_name_prefix="reader") as readers,
            self.observe_waiters({"reader": waiting}),
            patch.object(CsvStorage, "append_row", fail_after_append),
        ):
            insert = writers.submit(
                self.executor.execute, Insert("students", ["name"], ["failed"])
            )
            try:
                self.assertTrue(appended.wait(5))
                select = readers.submit(self.executor.execute, Select("students"))
                self.assertTrue(waiting.wait(5))
                self.assertFalse(select.done())
            finally:
                release.set()
            with self.assertRaises(OSError):
                insert.result(5)
            self.assertEqual(select.result(5), Selected(["id", "name"], []))
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertIsNone(self.state.max_id)
        self.assertEqual(
            self.executor.execute(Insert("students", ["name"], ["next"])), Inserted(1)
        )

    def test_waiting_writer_precedes_new_query_before_id_issue(self) -> None:
        inside = threading.Event()
        release = threading.Event()
        writer_waiting = threading.Event()
        reader_waiting = threading.Event()
        original = self.state.index.find
        auto_ids: list[int] = []
        original_auto_id = self.executor._auto_id

        def find(key: int) -> int | None:
            if threading.current_thread().name.startswith("first"):
                inside.set()
                if not release.wait(5):
                    raise TimeoutError("first SELECT not released")
            return original(key)

        def auto_id(state) -> int:
            value = original_auto_id(state)
            auto_ids.append(value)
            return value

        with (
            ThreadPoolExecutor(1, thread_name_prefix="first") as first_pool,
            ThreadPoolExecutor(1, thread_name_prefix="writer") as writers,
            ThreadPoolExecutor(1, thread_name_prefix="late") as late_pool,
            patch.object(self.state.index, "find", find),
            patch.object(self.executor, "_auto_id", auto_id),
            self.observe_waiters({"writer": writer_waiting, "late": reader_waiting}),
        ):
            first = first_pool.submit(
                self.executor.execute, Select("students", None, ("id", 1))
            )
            try:
                self.assertTrue(inside.wait(5))
                writer = writers.submit(
                    self.executor.execute, Insert("students", ["name"], ["new"])
                )
                self.assertTrue(writer_waiting.wait(5))
                late = late_pool.submit(self.executor.execute, Select("students"))
                self.assertTrue(reader_waiting.wait(5))
                self.assertEqual(auto_ids, [])
            finally:
                release.set()
            self.assertEqual(first.result(5), Selected(["id", "name"], []))
            self.assertEqual(writer.result(5), Inserted(1))
            self.assertEqual(late.result(5), Selected(["id", "name"], [[1, "new"]]))
        self.assertEqual(auto_ids, [1])

    def test_other_table_runs_while_first_table_waits(self) -> None:
        waiting = threading.Event()
        with ThreadPoolExecutor(2, thread_name_prefix="query") as pool:
            with self.observe_waiters({"query": waiting}):
                with self.state.lock.write_lock():
                    blocked = pool.submit(self.executor.execute, Select("students"))
                    self.assertTrue(waiting.wait(5))
                    independent = pool.submit(self.executor.execute, Select("other"))
                    self.assertEqual(
                        independent.result(5), Selected(["id", "name"], [])
                    )
                    self.assertFalse(blocked.done())
                self.assertEqual(blocked.result(5), Selected(["id", "name"], []))

    def test_error_during_index_read_releases_lock_for_next_insert(self) -> None:
        with patch.object(
            self.state.index, "find", side_effect=StorageError("injected read")
        ):
            with self.assertRaises(StorageError):
                self.executor.execute(Select("students", None, ("id", 1)))
        self.assertEqual(
            self.executor.execute(Insert("students", ["name"], ["next"])), Inserted(1)
        )

    def test_index_reads_keep_independent_seek_positions(self) -> None:
        for identifier in (1, 2):
            self.executor.execute(
                Insert("students", None, [identifier, f"student-{identifier}"])
            )
        sought = threading.Event()
        release = threading.Event()
        original_open = Path.open
        handles: list[int] = []

        class InterleavedFile:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                handles.append(id(self.stream))
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def seek(self, offset: int):
                result = self.stream.seek(offset)
                if threading.current_thread().name.startswith("first"):
                    sought.set()
                    if not release.wait(5):
                        raise TimeoutError("first seek not released")
                return result

            def readline(self):
                return self.stream.readline()

        def open_interleaved(path, *args, **kwargs):
            return InterleavedFile(original_open(path, *args, **kwargs))

        with (
            ThreadPoolExecutor(1, thread_name_prefix="first") as first_pool,
            ThreadPoolExecutor(1, thread_name_prefix="second") as second_pool,
            patch.object(Path, "open", open_interleaved),
        ):
            first = first_pool.submit(
                self.executor.execute, Select("students", None, ("id", 1))
            )
            try:
                self.assertTrue(sought.wait(5))
                second = second_pool.submit(
                    self.executor.execute, Select("students", None, ("id", 2))
                )
                self.assertEqual(
                    second.result(5), Selected(["id", "name"], [[2, "student-2"]])
                )
            finally:
                release.set()
            self.assertEqual(
                first.result(5), Selected(["id", "name"], [[1, "student-1"]])
            )
        self.assertEqual(len(set(handles)), 2)


if __name__ == "__main__":
    unittest.main()
