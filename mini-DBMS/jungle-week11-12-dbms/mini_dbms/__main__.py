"""UTF-8 SQL 배치 파일을 실행하는 CLI."""

import argparse
import csv
import sys
from pathlib import Path
from typing import assert_never

from mini_dbms.catalog import Catalog
from mini_dbms.errors import DBError
from mini_dbms.executor import Executor
from mini_dbms.models import Inserted, Selected
from mini_dbms.parser import parse, split_statements


class _Options(argparse.Namespace):
    sql_file: Path = Path()
    tables: Path = Path("tables")


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(description="CSV-backed mini DBMS")
    _ = arguments.add_argument("sql_file", type=Path)
    _ = arguments.add_argument("--tables", type=Path, default=Path("tables"))
    options = arguments.parse_args(argv, namespace=_Options())
    try:
        sql = options.sql_file.read_text(encoding="utf-8")
        executor = Executor(Catalog(options.tables))
    except (OSError, UnicodeError, DBError) as error:
        print(f"input error: {error}", file=sys.stderr)
        return 1

    failed = False
    writer = csv.writer(sys.stdout, lineterminator="\n")
    for number, statement in enumerate(split_statements(sql), start=1):
        try:
            result = executor.execute(parse(statement))
            match result:
                case Selected(columns=columns, rows=rows):
                    writer.writerows([columns])
                    writer.writerows(rows)
                case Inserted():
                    pass
                case _:
                    assert_never(result)
        except (DBError, OSError, UnicodeError) as error:
            failed = True
            print(f"statement {number}: {error}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
