from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from unittest.mock import patch

from mini_dbms.locks import ReadWriteLock
from mini_dbms.pool import BoundedPool, PoolFull


class ReadWriteLockConcurrencyTests(unittest.TestCase):
    def _observed_wait(self, lock: ReadWriteLock, events: dict[str, threading.Event]):
        original = lock._condition.wait

        def wrapper(timeout: float | None = None) -> bool:
            name = threading.current_thread().name
            if name in events:
                events[name].set()
            return original(timeout)

        return wrapper

    def test_readers_overlap_but_writer_blocks_new_reader(self) -> None:
        lock = ReadWriteLock()
        first_inside = threading.Event()
        release_first = threading.Event()
        writer_waiting = threading.Event()
        reader_waiting = threading.Event()

        def first_reader() -> None:
            with lock.read_lock():
                first_inside.set()
                if not release_first.wait(5):
                    raise TimeoutError("first reader not released")

        def writer() -> str:
            with lock.write_lock():
                return "written"

        def late_reader() -> str:
            with lock.read_lock():
                return "late read"

        observed = self._observed_wait(
            lock, {"writer": writer_waiting, "late_reader": reader_waiting}
        )
        with patch.object(lock._condition, "wait", observed):
            first_thread = threading.Thread(target=first_reader, name="first_reader")
            first_thread.start()
            try:
                self.assertTrue(first_inside.wait(5))
                writer_thread = threading.Thread(target=writer, name="writer")
                writer_thread.start()
                self.assertTrue(writer_waiting.wait(5))
                late_thread = threading.Thread(target=late_reader, name="late_reader")
                late_thread.start()
                self.assertTrue(reader_waiting.wait(5))
                with lock._condition:
                    self.assertEqual(lock._readers, 1)
                    self.assertEqual(lock._waiting_writers, 1)
            finally:
                release_first.set()
            first_thread.join(5)
            writer_thread.join(5)
            late_thread.join(5)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertFalse(late_thread.is_alive())

        self.assertTrue(writer_waiting.is_set())
        self.assertTrue(reader_waiting.is_set())

    def test_writer_runs_before_later_reader(self) -> None:
        lock = ReadWriteLock()
        writer_inside = threading.Event()
        release_writer = threading.Event()
        reader_inside = threading.Event()
        writer_waiting = threading.Event()
        reader_waiting = threading.Event()

        def writer() -> None:
            with lock.write_lock():
                writer_inside.set()
                if not release_writer.wait(5):
                    raise TimeoutError("writer not released")

        def reader() -> None:
            with lock.read_lock():
                reader_inside.set()

        observed = self._observed_wait(
            lock, {"writer": writer_waiting, "reader": reader_waiting}
        )
        writer_thread = threading.Thread(target=writer, name="writer")
        reader_thread = threading.Thread(target=reader, name="reader")
        with patch.object(lock._condition, "wait", observed):
            try:
                with lock.read_lock():
                    writer_thread.start()
                    self.assertTrue(writer_waiting.wait(5))
                    reader_thread.start()
                    self.assertTrue(reader_waiting.wait(5))
                self.assertTrue(writer_inside.wait(5))
                self.assertFalse(reader_inside.is_set())
            finally:
                release_writer.set()
                writer_thread.join(5)
                reader_thread.join(5)
        self.assertTrue(reader_inside.is_set())

    def test_table_independence(self) -> None:
        first = ReadWriteLock()
        second = ReadWriteLock()
        inside = threading.Event()
        release = threading.Event()

        def hold_first() -> None:
            with first.write_lock():
                inside.set()
                release.wait(5)

        def use_second() -> str:
            with second.read_lock():
                return "second"

        holder = threading.Thread(target=hold_first)
        holder.start()
        try:
            self.assertTrue(inside.wait(5))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(use_second).result(5), "second")
        finally:
            release.set()
            holder.join(5)

    def test_injected_exception_releases_both_lock_modes(self) -> None:
        lock = ReadWriteLock()
        with self.assertRaisesRegex(RuntimeError, "injected read"):
            with lock.read_lock():
                raise RuntimeError("injected read")
        with self.assertRaisesRegex(RuntimeError, "injected write"):
            with lock.write_lock():
                raise RuntimeError("injected write")
        with lock._condition:
            self.assertEqual(lock._readers, 0)
            self.assertFalse(lock._writer)
            self.assertEqual(lock._waiting_writers, 0)


class BoundedPoolTests(unittest.TestCase):

    def test_pool_full_rejects_when_slots_exhausted(self) -> None:
        pool = BoundedPool(workers=1, queue_size=1)
        self.addCleanup(pool.shutdown)
        blocker = threading.Event()
        release_blocker = threading.Event()

        def block() -> str:
            blocker.set()
            release_blocker.wait(5)
            return "done"

        first = pool.submit(block)
        self.assertTrue(blocker.wait(5))
        second = pool.submit(lambda: "queued")

        with self.assertRaises(PoolFull):
            pool.submit(lambda: "rejected")

        release_blocker.set()
        self.assertEqual(first.result(5), "done")
        self.assertEqual(second.result(5), "queued")

    def test_pool_releases_slot_after_completion(self) -> None:
        pool = BoundedPool(workers=1, queue_size=1)
        self.addCleanup(pool.shutdown)

        def task() -> int:
            return 42

        for _ in range(4):
            self.assertEqual(pool.submit(task).result(5), 42)

    def test_pool_reuses_worker_thread(self) -> None:
        pool = BoundedPool(workers=1, queue_size=0)
        self.addCleanup(pool.shutdown)
        ids: list[int] = []
        ready = threading.Event()
        release = threading.Event()

        def capture() -> None:
            ids.append(threading.current_thread().ident)
            ready.set()
            release.wait(5)

        first = pool.submit(capture)
        self.assertTrue(ready.wait(5))
        ready.clear()
        release.set()
        first.result(5)

        release.clear()
        second = pool.submit(capture)
        self.assertTrue(ready.wait(5))
        release.set()
        second.result(5)

        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])

    def test_pool_exception_releases_slot(self) -> None:
        pool = BoundedPool(workers=1, queue_size=0)
        self.addCleanup(pool.shutdown)

        def fail() -> None:
            raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            pool.submit(fail).result(5)

        self.assertEqual(pool.submit(lambda: "ok").result(5), "ok")

    def test_pool_cancel_before_start_releases_slot(self) -> None:
        pool = BoundedPool(workers=1, queue_size=1)
        self.addCleanup(pool.shutdown)
        blocker = threading.Event()
        release_blocker = threading.Event()

        def block() -> str:
            blocker.set()
            release_blocker.wait(5)
            return "done"

        first = pool.submit(block)
        self.assertTrue(blocker.wait(5))
        queued = pool.submit(lambda: "queued")
        self.assertTrue(queued.cancel())

        replacement = pool.submit(lambda: "replacement")
        release_blocker.set()
        self.assertEqual(first.result(5), "done")
        self.assertEqual(replacement.result(5), "replacement")

    def test_pool_saturates_and_accepts_after_completion(self) -> None:
        pool = BoundedPool(workers=2, queue_size=2)
        self.addCleanup(pool.shutdown)
        barrier = threading.Barrier(2)
        release = threading.Event()
        entered = [threading.Event() for _ in range(2)]

        def occupy(i: int) -> str:
            entered[i].set()
            barrier.wait(5)
            release.wait(5)
            return f"worker-{i}"

        futures = [pool.submit(occupy, i) for i in range(2)]
        for event in entered:
            self.assertTrue(event.wait(5))

        queued_futures = [pool.submit(lambda i=i: f"queued-{i}") for i in range(2)]
        with self.assertRaises(PoolFull):
            pool.submit(lambda: "overflow")

        release.set()
        for future in futures + queued_futures:
            self.assertTrue(future.result(5).startswith(("worker-", "queued-")))


if __name__ == "__main__":
    unittest.main()
