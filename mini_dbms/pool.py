from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class PoolFull(Exception):
    pass


class _Slot:
    __slots__: tuple[str, ...] = ("_semaphore", "_released", "_lock")

    def __init__(self, semaphore: threading.Semaphore) -> None:
        self._semaphore: threading.Semaphore = semaphore
        self._released: bool = False
        self._lock: threading.Lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._semaphore.release()
                self._released = True


class BoundedPool:
    def __init__(self, workers: int, queue_size: int) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if queue_size < 0:
            raise ValueError("queue_size must be non-negative")
        self._semaphore: threading.Semaphore = threading.Semaphore(workers + queue_size)
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=workers)

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        if not self._semaphore.acquire(blocking=False):
            raise PoolFull()
        slot = _Slot(self._semaphore)

        def wrapper() -> T:
            try:
                return fn(*args, **kwargs)
            finally:
                slot.release()

        try:
            future = self._executor.submit(wrapper)
        except Exception:
            slot.release()
            raise
        future.add_done_callback(lambda _: slot.release())
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
