"""개발·테스트용 단일 프로세스 API 서버."""

import argparse
from pathlib import Path

import uvicorn

from mini_dbms.api import create_app


class _Options(argparse.Namespace):
    tables: Path = Path("tables")
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 4
    queue_size: int = 16
    max_sql_length: int = 10_000


def main(argv: list[str] | None = None) -> None:
    arguments = argparse.ArgumentParser(description=__doc__)
    _ = arguments.add_argument("--tables", type=Path, default=Path("tables"))
    _ = arguments.add_argument("--host", default="127.0.0.1")
    _ = arguments.add_argument("--port", type=int, default=8000)
    _ = arguments.add_argument("--workers", type=int, default=4, help="SQL worker threads")
    _ = arguments.add_argument("--queue-size", type=int, default=16)
    _ = arguments.add_argument("--max-sql-length", type=int, default=10_000)
    options = arguments.parse_args(argv, namespace=_Options())
    app = create_app(
        options.tables,
        workers=options.workers,
        queue_size=options.queue_size,
        max_sql_length=options.max_sql_length,
    )
    uvicorn.run(app, host=options.host, port=options.port, workers=1)


if __name__ == "__main__":
    main()
