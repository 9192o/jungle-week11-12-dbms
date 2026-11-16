"""공통 실행기를 사용하는 단일 프로세스 HTTP 경계."""

from collections.abc import AsyncGenerator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
from threading import get_ident
from typing import assert_never

import anyio
from anyio.lowlevel import current_token
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr, ValidationError
from starlette.requests import ClientDisconnect
from starlette import status

from mini_dbms.catalog import Catalog
from mini_dbms.errors import (
    ConstraintError,
    ParseError,
    StorageError,
    TableUnavailableError,
)
from mini_dbms.executor import Executor
from mini_dbms.models import Inserted, Selected
from mini_dbms.parser import parse, split_statements
from mini_dbms.pool import BoundedPool, PoolFull


_logger = logging.getLogger(__name__)


class _Query(BaseModel):
    sql: StrictStr


@dataclass(frozen=True, slots=True)
class _Runtime:
    executor: Executor
    pool: BoundedPool


def _error_response(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": {"code": code, "message": message}},
        status_code=http_status,
    )


def _execute_sql(executor: Executor, statement: str) -> Inserted | Selected:
    return executor.execute(parse(statement))


async def _await_result(future: Future[Inserted | Selected]) -> Inserted | Selected:
    ready = anyio.Event()
    token = current_token()
    loop_thread = get_ident()

    def completed(_: Future[Inserted | Selected]) -> None:
        if get_ident() == loop_thread:
            ready.set()
        else:
            anyio.from_thread.run_sync(ready.set, token=token)

    # 요청 취소는 실행 중 작업이나 그 작업의 슬롯 소유권을 취소하지 않는다.
    future.add_done_callback(completed)
    await ready.wait()
    return future.result()


def create_app(
    table_directory: str | Path,
    *,
    tables: dict[str, str] | None = None,
    workers: int = 4,
    queue_size: int = 16,
    max_sql_length: int = 10_000,
) -> FastAPI:
    if workers < 1 or queue_size < 0 or max_sql_length < 1:
        raise ValueError(
            "workers and max_sql_length must be positive; queue_size cannot be negative"
        )
    resources: list[_Runtime] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        catalog = Catalog(table_directory, tables)
        executor = Executor(catalog)
        pool = BoundedPool(workers, queue_size)
        resources.append(_Runtime(executor, pool))
        app.state.catalog = catalog
        app.state.executor = executor
        app.state.pool = pool
        try:
            yield
        finally:
            # 완료 콜백이 이벤트 루프에서 처리될 동안 종료 대기는 별도 스레드에서 한다.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(pool.shutdown)
            resources.clear()

    app = FastAPI(lifespan=lifespan)

    @app.post("/query")
    async def query(request: Request) -> JSONResponse:
        try:
            body = _Query.model_validate_json(await request.body())
        except (ValidationError, ClientDisconnect):
            return _error_response(
                "bad_request", "invalid JSON SQL request", status.HTTP_400_BAD_REQUEST
            )
        sql = body.sql
        if len(sql) > max_sql_length:
            return _error_response(
                "bad_request", "SQL exceeds maximum length", status.HTTP_400_BAD_REQUEST
            )
        statements = split_statements(sql)
        if len(statements) != 1:
            return _error_response(
                "syntax_error",
                "exactly one SQL statement is required",
                status.HTTP_400_BAD_REQUEST,
            )
        runtime = resources[0]
        try:
            future = runtime.pool.submit(_execute_sql, runtime.executor, statements[0])
        except PoolFull:
            return _error_response(
                "service_unavailable",
                "server is busy",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            result = await _await_result(future)
        except ParseError as error:
            return _error_response(
                "syntax_error", str(error), status.HTTP_400_BAD_REQUEST
            )
        except ConstraintError as error:
            return _error_response(
                "constraint_error", str(error), status.HTTP_400_BAD_REQUEST
            )
        except TableUnavailableError:
            _logger.exception("table unavailable")
            return _error_response(
                "table_unavailable",
                "table is unavailable",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (StorageError, OSError, UnicodeError):
            _logger.exception("storage operation failed")
            return _error_response(
                "storage_error",
                "storage operation failed",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception:
            _logger.exception("unexpected query failure")
            return _error_response(
                "internal_error",
                "internal server error",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        match result:
            case Inserted(id=identifier):
                return JSONResponse({"success": True, "id": identifier})
            case Selected(columns=columns, rows=rows):
                return JSONResponse({"success": True, "columns": columns, "rows": rows})
            case _:
                assert_never(result)

    return app
