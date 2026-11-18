from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import BoundedSemaphore, Condition, Lock


class TableLock:
    def __init__(self):
        self._condition = Condition()
        self._readers = 0
        self._writing = False
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        with self._condition:
            while self._writing or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if not self._readers:
                    self._condition.notify_all()

    @contextmanager
    def write(self):
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._readers or self._writing:
                    self._condition.wait()
                self._writing = True
            finally:
                self._waiting_writers -= 1
                if not self._writing:
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._writing = False
                self._condition.notify_all()


class PoolFull(Exception):
    pass


class PoolClosed(Exception):
    pass


class WorkPool:
    def __init__(self, workers, queue_size):
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._slots = BoundedSemaphore(workers + queue_size)
        self._admission = Lock()
        self._closed = False

    def submit(self, function, *args):
        with self._admission:
            if self._closed:
                raise PoolClosed
            if not self._slots.acquire(blocking=False):
                raise PoolFull
            try:
                job = self._executor.submit(function, *args)
            except BaseException:
                self._slots.release()
                raise
            job.add_done_callback(self._release_slot)
            return job

    def _release_slot(self, _job):
        self._slots.release()

    def shutdown(self):
        with self._admission:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
