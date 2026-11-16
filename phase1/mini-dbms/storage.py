import csv
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sql import Insert, SqlError


class StorageError(ValueError):
    pass


class RestoreError(StorageError):
    pass


@dataclass(frozen=True)
class Selected:
    columns: tuple[str, ...]
    rows: list[tuple[int | str, ...]]


CSV_FIELD = r'(?:"[^"\r\n]*(?:""[^"\r\n]*)*"|[^",\r\n]*)'
CSV_RECORD = re.compile(rf"{CSV_FIELD}(?:,{CSV_FIELD})*")
DECIMAL = re.compile(r"[+-]?[0-9]+")

# The csv module's default read limit is lower than its write limit.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


def fields(raw, line_number):
    try:
        line = raw.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError as exc:
        raise StorageError(f"line {line_number}: invalid UTF-8") from exc
    if CSV_RECORD.fullmatch(line) is None:
        raise StorageError(f"line {line_number}: invalid CSV quoting or line ending")
    try:
        return next(csv.reader([line], strict=True))
    except csv.Error as exc:
        raise StorageError(f"line {line_number}: {exc}") from exc


def schema(file):
    raw_header = file.readline()
    if not raw_header:
        raise StorageError("missing CSV header")
    columns = {}
    for field in fields(raw_header, 1):
        name, separator, kind = field.partition(":")
        name, kind = name.strip(), kind.strip()
        if not separator or not name or kind not in ("int", "string"):
            raise StorageError(f"invalid schema field {field!r}")
        key = name.casefold()
        if key in columns:
            raise StorageError(f"duplicate column {name!r}")
        columns[key] = (name, kind)
    if "record_id" not in columns or columns["record_id"][1] != "int":
        raise StorageError("record_id must be an integer column")
    return columns


def rows(file, columns):
    record_id_index = tuple(columns).index("record_id")
    seen_ids = set()
    for number, raw in enumerate(file, start=2):
        values = fields(raw, number)
        if len(values) != len(columns):
            raise StorageError(f"line {number}: incorrect number of fields")
        row = []
        for (column, kind), value in zip(columns.values(), values):
            if kind == "int":
                if DECIMAL.fullmatch(value) is None:
                    raise StorageError(f"line {number}: {column!r} must be an integer")
                try:
                    value = int(value)
                except ValueError as exc:
                    raise StorageError(
                        f"line {number}: integer in {column!r} is too large"
                    ) from exc
            row.append(value)

        record_id = row[record_id_index]
        if record_id in seen_ids:
            raise StorageError(f"line {number}: duplicate record_id {record_id}")
        seen_ids.add(record_id)
        yield tuple(row)


def validate_value(column, value, schema_columns):
    name, kind = schema_columns[column]
    if type(value) is not (int if kind == "int" else str):
        raise SqlError(f"{name!r} requires a value of type {kind}")


def insert_values(command, schema_columns):
    columns = tuple(schema_columns)
    if command.columns is not None:
        columns = tuple(name.casefold() for name in command.columns)
    if len(columns) != len(command.values):
        raise SqlError("column and value counts differ")
    if len(columns) != len(set(columns)):
        raise SqlError("duplicate INSERT column")
    for column, value in zip(columns, command.values):
        if column not in schema_columns:
            raise SqlError(f"unknown column {column!r}")
        validate_value(column, value, schema_columns)
    if len(columns) != len(schema_columns):
        raise SqlError("INSERT requires a value for each column, including record_id")
    by_column = dict(zip(columns, command.values))
    return tuple(by_column[column] for column in schema_columns)


def select_rows(command, file):
    schema_columns = schema(file)
    positions = {column: index for index, column in enumerate(schema_columns)}
    columns = tuple(schema_columns)
    if command.columns is not None:
        columns = tuple(name.casefold() for name in command.columns)

    for column in columns:
        if column not in positions:
            raise SqlError(f"unknown column {column!r}")
    projection = tuple(positions[column] for column in columns)
    if command.condition is not None:
        column, value = command.condition
        column = column.casefold()
        if column not in positions:
            raise SqlError(f"unknown column {column!r}")
        validate_value(column, value, schema_columns)
        condition_index = positions[column]

    selected = []
    for row in rows(file, schema_columns):
        if command.condition is None or row[condition_index] == value:
            selected.append(tuple(row[index] for index in projection))
    output_columns = tuple(schema_columns[column][0] for column in columns)
    return Selected(output_columns, selected)


def serialize_record(values):
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(values)
    return output.getvalue().encode("utf-8")


def append_record(file, payload):
    eof = file.seek(0, io.SEEK_END)
    if eof:
        file.seek(eof - 1)
        if file.read(1) != b"\n":
            payload = b"\n" + payload
    file.seek(eof)

    try:
        if file.write(payload) != len(payload):
            raise OSError("short write")
        file.flush()
    except OSError as exc:
        try:
            file.truncate(eof)
            file.flush()
            if file.seek(0, io.SEEK_END) != eof:
                raise OSError("restored size does not match the previous EOF")
        except OSError as restore_exc:
            raise RestoreError(
                f"append failed ({exc}) and restoration failed ({restore_exc})"
            ) from restore_exc
        raise StorageError(f"append failed ({exc}); previous EOF restored") from exc


class CsvStorage:
    REGISTERED_TABLES = {"users": "users.csv"}

    def __init__(self):
        self.directory = Path.cwd() / "tables"
        self.blocked_tables = set()

    def resolve_table(self, table):
        table = table.casefold()
        if table not in self.REGISTERED_TABLES:
            raise SqlError(f"unregistered table {table!r}")
        if table in self.blocked_tables:
            raise StorageError(f"table {table!r} is blocked after a failed restoration")
        directory = self.directory.resolve()
        path = (directory / self.REGISTERED_TABLES[table]).resolve()
        if directory not in path.parents:
            raise StorageError(f"CSV path for {table!r} escapes the tables directory")
        return path

    def insert(self, command, path):
        # Read with buffering, then append on the same file without buffering.
        # A failed buffered write must not be retried while closing the file.
        with path.open("r+b", buffering=0) as file:
            reader = io.BufferedReader(file)
            try:
                columns = schema(reader)
                values = insert_values(command, columns)
                id_index = tuple(columns).index("record_id")
                record_id = values[id_index]
                duplicate = False
                for row in rows(reader, columns):
                    if row[id_index] == record_id:
                        duplicate = True
            finally:
                reader.detach()

            if duplicate:
                raise SqlError(f"duplicate record_id {record_id}")
            append_record(file, serialize_record(values))

    def execute(self, command):
        try:
            path = self.resolve_table(command.table)
            if isinstance(command, Insert):
                self.insert(command, path)
                return None
            with path.open("rb") as file:
                return select_rows(command, file)
        except RestoreError:
            self.blocked_tables.add(command.table.casefold())
            raise
        except FileNotFoundError as exc:
            raise StorageError(f"CSV file for {command.table!r} does not exist") from exc
        except (OSError, RuntimeError) as exc:
            raise StorageError(f"cannot access table {command.table!r}: {exc}") from exc
