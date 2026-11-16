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
    """SQL 한 문장을 담는 JSON 본문.

    Attributes:
        sql: 숫자나 null을 문자열로 변환하지 않는 필수 문자열.
            길이와 문장 수는 작업 큐에 넣기 전에 별도로 검증한다.
    """

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
    """CSV 테이블과 공통 SQL 실행기를 연결한 API 애플리케이션을 만든다.

    앱 생성 시에는 설정만 검사하고, lifespan 시작 시 CSV의 스키마와
    데이터를 읽어 테이블별 인덱스 및 최대 ID를 복원한다. 같은 프로세스의
    요청은 이 상태와 제한된 스레드 풀을 공유한다. 종료 시에는 수락한
    작업이 끝날 때까지 기다리므로 파일 쓰기 중 자원을 먼저 해제하지 않는다.

    Args:
        table_directory: 테이블 CSV를 보관하는 디렉터리. 등록 경로는
            심볼릭 링크를 해소한 뒤에도 이 디렉터리 내부여야 한다.
        tables: 테이블명과 상대 CSV 경로의 매핑. None이면 디렉터리의
            CSV 파일명에서 확장자를 뺀 이름을 테이블명으로 사용한다.
        workers: SQL을 실행할 작업 스레드 수. 기본값은 4이며 1 이상이다.
        queue_size: 실행 중 작업 외에 수락할 대기 작업 수. 기본값은 16이며
            0이면 실행 슬롯이 없을 때 즉시 요청을 거부한다.
        max_sql_length: sql 문자열 전체의 최대 문자 수. 기본값은 10,000이며
            공백과 세미콜론도 포함한다. 바이트 수 제한이 아니다.

    Returns:
        POST /query와 기본 문서 경로 /docs, /redoc, /openapi.json을 제공하는 앱.
        lifespan 시작에 성공한 뒤 SQL 요청을 처리할 수 있다.

    Raises:
        ValueError: workers 또는 max_sql_length가 1 미만이거나 queue_size가
            음수인 경우. CSV 읽기와 인덱스 구축 오류는 lifespan 시작 시 발생한다.

    Note:
        테이블 락은 프로세스 내부에서만 유효하므로 서버는 단일 프로세스로
        실행한다. 인증은 제공하지 않으며 개발 및 테스트 환경을 전제로 한다.
    """
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
        """SQL 한 문장을 실행하고 삽입 ID 또는 조회 결과를 반환한다.

        ## 요청 본문

        `Content-Type: application/json`으로 `sql` 문자열을 전달한다.
        `sql`은 필수이며 숫자, 배열, 객체, `null`은 허용하지 않는다.
        마지막 세미콜론(`;`)을 포함한 **INSERT 또는 SELECT 한 문장**만 지원한다.
        빈 입력이나 여러 문장은 거부하며, 여러 문장이면 첫 문장도 실행하지 않는다.
        문자열 리터럴 안의 세미콜론은 문장 구분자로 취급하지 않는다.

        SQL 길이는 서버의 `max_sql_length` 설정을 따르며 기본값은
        **10,000자**이다. 공백과 세미콜론을 포함한 문자열 전체를 문자 수로
        검사하고, 길이 또는 문장 수 제한 위반은 작업 큐에 넣기 전에 거부한다.

        ```json
        {"sql": "SELECT * FROM students WHERE id = 1;"}
        ```

        ## 지원 SQL과 입력 규칙

        아래 예시는 `students` 테이블의 스키마가
        `id:int,name:string,seat:int`인 경우이다. 테이블 CSV는 서버 시작 전에
        준비되어 있어야 한다.

        - `SELECT * FROM students;`: 모든 열과 행을 조회한다.
        - `SELECT name, seat FROM students WHERE id = 1;`: 요청한 열 순서로 반환한다.
        - `SELECT * FROM students WHERE name = '민수';`: 문자열 조건으로 조회한다.
        - `INSERT INTO students (name, seat) VALUES ('민수', 20);`:
          열 목록에서 `id`만 생략하면 현재 최대 ID + 1을 부여한다.
          빈 테이블은 1부터 시작한다.
        - `INSERT INTO students VALUES (42, '민수', 20);`:
          열 목록이 없으면 ID를 포함한 모든 값을 스키마 순서로 지정한다.
          명시한 ID가 이미 존재하면 거부한다.

        키워드는 대소문자를 구분하지 않지만 테이블명과 열 이름은 등록된 표기를
        사용한다. 값은 정수 또는 작은따옴표 문자열이며, 문자열 안의 작은따옴표는
        `'O''Brien'`처럼 두 번 쓴다. 문자열 내부 줄바꿈은 허용하지 않는다.
        `WHERE`는 단일 동등 비교(`=`)만 지원하며, JOIN, ORDER BY, LIMIT,
        복합 조건, DDL, UPDATE, DELETE 및 다중 문장 트랜잭션은 지원하지 않는다.

        ## 성공 응답: HTTP 200

        **INSERT**는 저장된 ID를 반환한다. 아래 ID는 빈 테이블에 자동 ID로
        처음 삽입한 경우의 예시이며, 실제 값은 테이블 상태에 따라 달라진다.

        ```json
        {"success": true, "id": 1}
        ```

        **SELECT**의 `columns`는 열 이름 목록이고, `rows`의 각 행은 같은 순서의
        값 배열이다. 정수는 JSON 숫자, 문자열은 JSON 문자열로 반환한다.
        `*`는 스키마 순서를 사용하며, 일치하는 행이 없어도 HTTP 200과
        열 목록을 반환하고 `rows`만 빈 배열이 된다.

        ```json
        {"success": true, "columns": ["id", "name", "seat"], "rows": [[1, "민수", 20]]}
        ```

        ```json
        {"success": true, "columns": ["id", "name", "seat"], "rows": []}
        ```

        ## 오류 응답

        실패 응답은 공통으로 `success: false`와 `error.code`, `error.message`를
        포함한다. 아래 메시지는 예시이며 원인에 따라 달라질 수 있다.
        요청 검증 오류도 HTTP 422가 아닌 **HTTP 400**으로 반환한다.

        ```json
        {"success": false, "error": {"code": "constraint_error", "message": "duplicate id: 1"}}
        ```

        | HTTP 상태 | error.code | 발생 조건 |
        | --- | --- | --- |
        | 400 | `bad_request` | JSON 해석 실패, sql 누락·타입 오류, SQL 길이 초과 |
        | 400 | `syntax_error` | 빈 입력·다중 문장, 세미콜론 누락, 잘못되거나 지원하지 않는 SQL |
        | 400 | `constraint_error` | 없는 테이블·열, 스키마 타입 불일치, 중복 ID 등 입력 제약 위반 |
        | 500 | `storage_error` | 파일 I/O, 저장 데이터 해석 또는 인코딩 오류 |
        | 500 | `internal_error` | 그 밖의 예상하지 못한 실행 오류 |
        | 503 | `service_unavailable` | 작업 스레드와 대기 큐의 수락 용량이 모두 소진됨 |
        | 503 | `table_unavailable` | 삽입 실패 후 복구를 확인할 수 없어 테이블을 격리함 |

        ## 실행과 동시성

        `WHERE id = 정수`는 B+ Tree 인덱스로 조회하고, 다른 열 조건이나
        조건 없는 조회는 CSV를 순차 탐색한다. 같은 테이블의 삽입은 쓰기 락으로
        직렬화하며, 조회는 읽기 락으로 불완전한 행이 보이지 않도록 보호한다.
        쓰기 대기자가 있으면 새 조회보다 쓰기를 우선한다.

        기본 작업 스레드는 4개, 대기 슬롯은 16개이며 서버 설정으로 변경할 수 있다.
        클라이언트가 연결을 끊어도 이미 수락한 SQL 작업은 취소되지 않을 수 있다.
        특히 INSERT는 멱등 요청이 아니므로 응답을 받지 못한 요청을 재전송하면
        별도의 행이 추가될 수 있다.
        \f
        Args:
            request: JSON 본문을 읽을 원본 HTTP 요청. 공통 오류 응답 형식을
                유지하기 위해 본문을 직접 검증한다.

        Returns:
            실행 결과 또는 오류 코드와 메시지를 담은 JSONResponse.
        """
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
