import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import anyio

from mini_dbms.api import _await_result
from mini_dbms.models import Inserted
from mini_dbms.pool import BoundedPool, PoolFull


class ApiCancellationTests(unittest.TestCase):
    def test_already_completed_future_can_be_awaited_on_loop_thread(self) -> None:
        future = Future()
        future.set_result(Inserted(42))

        async def scenario() -> None:
            with anyio.fail_after(5):
                self.assertEqual(await _await_result(future), Inserted(42))

        anyio.run(scenario)

    def test_cancelled_request_keeps_running_job_slot_until_completion(self) -> None:
        pool = BoundedPool(1, 0)
        entered = threading.Event()
        release = threading.Event()

        def work() -> Inserted:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("SQL work was not released")
            return Inserted(1)

        async def scenario() -> None:
            future = pool.submit(work)
            subscribed = anyio.Event()
            cancelled = anyio.Event()
            scopes: list[anyio.CancelScope] = []
            original_subscribe = future.add_done_callback

            def subscribe(callback) -> None:
                original_subscribe(callback)
                subscribed.set()

            async def request() -> None:
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    try:
                        await _await_result(future)
                    finally:
                        cancelled.set()

            try:
                self.assertTrue(entered.wait(5))
                with anyio.fail_after(5):
                    with patch.object(future, "add_done_callback", subscribe):
                        async with anyio.create_task_group() as group:
                            group.start_soon(request)
                            await subscribed.wait()
                            scopes[0].cancel()
                            await cancelled.wait()
                            self.assertFalse(future.cancelled())
                            with self.assertRaises(PoolFull):
                                pool.submit(lambda: Inserted(99))
                            release.set()
                            self.assertEqual(
                                await anyio.to_thread.run_sync(future.result), Inserted(1)
                            )
                            self.assertEqual(
                                await _await_result(pool.submit(lambda: Inserted(2))),
                                Inserted(2),
                            )
            finally:
                release.set()
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(pool.shutdown)

        anyio.run(scenario)
