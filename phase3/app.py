import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from concurrency import PoolClosed, PoolFull, TableLock, WorkPool


PHASE3 = Path(__file__).resolve().parent
PHASE2_ENGINE = PHASE3.parent / "phase2" / "mini-dbms"
sys.path.insert(0, str(PHASE2_ENGINE))

from sql import Insert, SqlError, parse, statements
from storage import CsvStorage, RestoreError, StorageError


SQL_LIMIT = 4_096
logger = logging.getLogger(__name__)


def error(status, code, message):
    return status, {"success": False, "error": {"code": code, "message": message}}


def response(result):
    status, content = result
    return JSONResponse(status_code=status, content=content)


def execute_query(statement, storage, locks):
    try:
        command = parse(statement)
    except SqlError as exc:
        # The Phase 2 parser chains a ValueError only for unsigned integer overflow.
        if isinstance(exc.__cause__, ValueError):
            return error(400, "INPUT_CONSTRAINT_ERROR", "Integer is outside the supported range.")
        return error(400, "SQL_SYNTAX_ERROR", "Invalid or unsupported SQL syntax.")

    name = command.table.casefold()
    if name not in locks:
        return error(404, "TABLE_NOT_FOUND", "Table is not registered.")
    inserting = isinstance(command, Insert)
    lock = locks[name]
    with (lock.write() if inserting else lock.read()):
        if name in storage.blocked_tables:
            return error(503, "TABLE_UNAVAILABLE", "Table is unavailable.")
        try:
            result = storage.execute(command)
            if inserting:
                record_id = storage.tables[name].max_id
        except SqlError:
            return error(400, "INPUT_CONSTRAINT_ERROR", "SQL input violates a constraint.")
        except RestoreError:
            logger.exception("Restoration failed for table %r", name)
            return error(503, "TABLE_UNAVAILABLE", "Table is unavailable.")
        except (StorageError, OSError):
            logger.exception("Storage failed for table %r", name)
            return error(500, "STORAGE_ERROR", "Database storage failed.")
    if inserting:
        return 201, {"success": True, "record_id": str(record_id)}
    rows = [[str(value) if type(value) is int else value for value in row] for row in result.rows]
    return 200, {"success": True, "columns": list(result.columns), "rows": rows}


async def wait_for_disconnect(request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def wait_for_query(request, job):
    result = asyncio.wrap_future(job)
    disconnected = asyncio.create_task(wait_for_disconnect(request))
    try:
        completed, _ = await asyncio.wait(
            (result, disconnected), return_when=asyncio.FIRST_COMPLETED
        )
        if result in completed:
            return response(result.result())
        job.cancel()
        # The connection is already gone. Any running worker retains its slot.
        return Response(status_code=204)
    except asyncio.CancelledError:
        job.cancel()
        raise
    finally:
        disconnected.cancel()
        if not result.done():
            result.cancel()
        elif not result.cancelled():
            result.exception()
        await asyncio.gather(disconnected, return_exceptions=True)


def create_app(table_directory=None, *, catalog=None, workers=4, queue_size=16):
    if workers < 1 or queue_size < 0:
        raise ValueError("workers must be positive and queue_size must be nonnegative")
    directory = PHASE3 / "tables" if table_directory is None else table_directory

    @asynccontextmanager
    async def lifespan(application):
        storage = CsvStorage(directory, catalog=catalog)
        failures = storage.build_indexes()
        for name, cause in failures.items():
            logger.error("Index build failed for table %r: %s", name, cause)
        if not storage.tables:
            raise RuntimeError("No registered table completed index construction")
        application.state.storage = storage
        application.state.locks = {name: TableLock() for name in storage.catalog}
        pool = WorkPool(workers, queue_size)
        application.state.pool = pool
        try:
            yield
        finally:
            await asyncio.to_thread(pool.shutdown)

    application = FastAPI(lifespan=lifespan)

    @application.post("/query")
    async def query(request: Request):
        try:
            body = await request.json()
        except ClientDisconnect:
            return Response(status_code=204)
        except (ValueError, RecursionError):
            return response(error(400, "INVALID_REQUEST", "Request body must be a JSON object."))
        if not isinstance(body, dict) or not isinstance(body.get("sql"), str):
            return response(error(400, "INVALID_REQUEST", "The sql field must be a string."))
        sql = body["sql"]
        if len(sql) > SQL_LIMIT:
            return response(error(413, "SQL_TOO_LONG", "The sql field exceeds 4096 characters."))
        try:
            sql.encode("utf-8")
        except UnicodeEncodeError:
            return response(error(400, "INVALID_REQUEST", "The sql field must be valid Unicode."))
        batch = statements(sql)
        statement = next(batch, None)
        if statement is None or next(batch, None) is not None:
            return response(error(400, "INVALID_STATEMENT_COUNT", "Exactly one SQL statement is required."))

        state = request.app.state
        try:
            job = state.pool.submit(execute_query, statement, state.storage, state.locks)
        except PoolFull:
            return response(error(503, "POOL_FULL", "The query pool is full."))
        except PoolClosed:
            return response(error(503, "SERVER_SHUTTING_DOWN", "The server is shutting down."))
        try:
            return await wait_for_query(request, job)
        except Exception:
            logger.exception("Unexpected query failure")
            return response(error(500, "STORAGE_ERROR", "Database execution failed."))

    return application


app = create_app(
    workers=int(os.environ.get("DBMS_WORKERS", "4")),
    queue_size=int(os.environ.get("DBMS_QUEUE_SIZE", "16")),
)
