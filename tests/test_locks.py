import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from mini_dbms.locks import ReadWriteLock


class ReadWriteLockTests(unittest.TestCase):
    def test_readers_share_lock_before_first_reader_finishes(self) -> None:
        lock = ReadWriteLock()
        first_inside = threading.Event()
        release_first = threading.Event()

        def first_reader() -> None:
            with lock.read_lock():
                first_inside.set()
                if not release_first.wait(5):
                    raise TimeoutError("first reader was not released")

        def second_reader() -> str:
            with lock.read_lock():
                return "shared"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_reader)
            try:
                self.assertTrue(first_inside.wait(5))
                self.assertEqual(pool.submit(second_reader).result(5), "shared")
            finally:
                release_first.set()
            first.result(5)

    def test_waiting_writer_runs_before_later_reader(self) -> None:
        lock = ReadWriteLock()
        writer_waiting = threading.Event()
        reader_waiting = threading.Event()
        writer_inside = threading.Event()
        release_writer = threading.Event()
        reader_inside = threading.Event()
        original_wait = lock._condition.wait

        def observed_wait(timeout: float | None = None) -> bool:
            if threading.current_thread().name == "writer":
                writer_waiting.set()
            if threading.current_thread().name == "reader":
                reader_waiting.set()
            return original_wait(timeout)

        def writer() -> None:
            with lock.write_lock():
                writer_inside.set()
                if not release_writer.wait(5):
                    raise TimeoutError("writer was not released")

        def reader() -> None:
            with lock.read_lock():
                reader_inside.set()

        writer_thread = threading.Thread(target=writer, name="writer")
        reader_thread = threading.Thread(target=reader, name="reader")
        with patch.object(lock._condition, "wait", observed_wait):
            try:
                with lock.read_lock():
                    writer_thread.start()
                    self.assertTrue(writer_waiting.wait(5))
                    reader_thread.start()
                    self.assertTrue(reader_waiting.wait(5))
                    with lock._condition:
                        self.assertEqual(lock._readers, 1)
                        self.assertEqual(lock._waiting_writers, 1)
                self.assertTrue(writer_inside.wait(5))
                self.assertFalse(reader_inside.is_set())
            finally:
                release_writer.set()
                writer_thread.join(5)
                if reader_thread.ident is not None:
                    reader_thread.join(5)
        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(reader_thread.is_alive())
        self.assertTrue(reader_inside.is_set())

    def test_reader_waits_while_writer_holds_lock(self) -> None:
        lock = ReadWriteLock()
        waiting = threading.Event()
        original_wait = lock._condition.wait

        def observed_wait(timeout: float | None = None) -> bool:
            waiting.set()
            return original_wait(timeout)

        def read() -> str:
            with lock.read_lock():
                return "complete"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(lock._condition, "wait", observed_wait):
                with lock.write_lock():
                    result = pool.submit(read)
                    self.assertTrue(waiting.wait(5))
                    self.assertFalse(result.done())
                self.assertEqual(result.result(5), "complete")

    def test_exceptions_release_both_lock_modes(self) -> None:
        lock = ReadWriteLock()
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with lock.read_lock():
                raise RuntimeError("injected read failure")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with lock.write_lock():
                raise RuntimeError("injected write failure")
        with lock._condition:
            self.assertEqual(lock._readers, 0)
            self.assertFalse(lock._writer)
        with lock.write_lock():
            pass
        with lock.read_lock():
            pass

    def test_interrupted_writer_cleans_waiter_count(self) -> None:
        lock = ReadWriteLock()
        with lock.read_lock():
            with patch.object(
                lock._condition, "wait", side_effect=RuntimeError("interrupted")
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    with lock.write_lock():
                        self.fail("interrupted writer acquired lock")
        with lock._condition:
            self.assertEqual(lock._waiting_writers, 0)
            self.assertFalse(lock._writer)
        with lock.read_lock():
            pass

    def test_separate_tables_do_not_share_locks(self) -> None:
        first = ReadWriteLock()
        second = ReadWriteLock()

        def read_second() -> bool:
            with second.read_lock():
                return True

        with ThreadPoolExecutor(max_workers=1) as pool:
            with first.write_lock():
                self.assertTrue(pool.submit(read_second).result(5))


if __name__ == "__main__":
    unittest.main()
