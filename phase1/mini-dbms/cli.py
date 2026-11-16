import csv
import sys
from pathlib import Path

from sql import SqlError, parse, statements
from storage import CsvStorage, StorageError


def main(args):
    if len(args) != 1:
        print("usage: python mini-dbms/cli.py SQL_FILE", file=sys.stderr)
        return 1

    try:
        with Path(args[0]).open(encoding="utf-8", newline="") as file:
            source = file.read()
    except (OSError, UnicodeError) as exc:
        print(f"SQL input error: {exc}", file=sys.stderr)
        return 1

    storage = CsvStorage()
    output = csv.writer(sys.stdout, lineterminator="\n")
    failed = False
    for number, statement in enumerate(statements(source), start=1):
        try:
            result = storage.execute(parse(statement))
        except (SqlError, StorageError) as exc:
            print(f"statement {number}: {exc}", file=sys.stderr)
            failed = True
            continue

        if result is not None:
            output.writerow(result.columns)
            output.writerows(result.rows)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
