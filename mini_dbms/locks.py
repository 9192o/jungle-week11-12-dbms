"""테이블별 쓰기 대기 우선 읽기·쓰기 락."""

from collections.abc import Generator
from contextlib import contextmanager
from threading import Condition


class ReadWriteLock:
    def __init__(self) -> None:
        self._condition: Condition = Condition()
        self._readers: int = 0
        self._writer: bool = False
        self._waiting_writers: int = 0

    @contextmanager
    def read_lock(self) -> Generator[None]:
        with self._condition:
            # 이미 기다리는 쓰기가 새 조회에 계속 밀리지 않도록 한다.
            while self._writer or self._waiting_writers:
                _ = self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if not self._readers:
                    self._condition.notify_all()

    @contextmanager
    def write_lock(self) -> Generator[None]:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._readers or self._writer:
                    _ = self._condition.wait()
                self._writer = True
            finally:
                # 획득 전 예외도 대기 수에서 빠져야 이후 읽기가 진행된다.
                self._waiting_writers -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()
