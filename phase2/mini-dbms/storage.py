import csv
import io
import re
import sys
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from bptree import BPlusTree, DEFAULT_MAX_KEYS, DuplicateKeyError
from sql import Insert, MAX_UINT64, SqlError, parse_uint64


class StorageError(ValueError):
    pass


class RestoreError(StorageError):
    pass


@dataclass(frozen=True)
class Selected:
    columns: tuple[str, ...]
    rows: list[tuple[int | str, ...]]


@dataclass
class Table:
    path: Path
    columns: dict[str, tuple[str, str]]
    record_id_index: int
    index: BPlusTree
    max_id: int = 0


CSV_FIELD = r'(?:"[^"\r\n]*(?:""[^"\r\n]*)*"|[^",\r\n]*)'
CSV_RECORD = re.compile(rf"{CSV_FIELD}(?:,{CSV_FIELD})*")
DECIMAL = re.compile(r"[+-]?[0-9]+")

# Avoid rejecting CSV fields written by the same program.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


def fields(raw):
    try:
        line = raw.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError as exc:
        raise StorageError("invalid UTF-8") from exc
    if CSV_RECORD.fullmatch(line) is None:
        raise StorageError("invalid CSV quoting or line ending")
    try:
        return next(csv.reader([line], strict=True))
    except csv.Error as exc:
        raise StorageError(str(exc)) from exc


def schema(file):
    raw_header = file.readline()
    if not raw_header:
        raise StorageError("missing CSV header")
    columns = {}
    for field in fields(raw_header):
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


def parse_row(raw, columns):
    values = fields(raw)
    if len(values) != len(columns):
        raise StorageError("incorrect number of fields")
    row = []
    for (column, kind), value in zip(columns.values(), values):
        if kind == "int":
            if DECIMAL.fullmatch(value) is None:
                raise StorageError(f"{column!r} must be an integer")
            try:
                value = parse_uint64(value)
            except ValueError as exc:
                raise StorageError(f"{column!r}: {exc}") from exc
        row.append(value)
    return tuple(row)


def validate_value(column, value, columns):
    name, kind = columns[column]
    if type(value) is not (int if kind == "int" else str):
        raise SqlError(f"{name!r} requires a value of type {kind}")
    if kind == "int" and not 0 <= value <= MAX_UINT64:
        raise SqlError(f"{name!r}: integer outside unsigned 64-bit range")


def insert_values(command, columns):
    if command.columns is None:
        raise SqlError("INSERT requires a column list")
    names = tuple(name.casefold() for name in command.columns)
    if "record_id" in names:
        raise SqlError("record_id is automatically assigned")
    if len(names) != len(command.values):
        raise SqlError("column and value counts differ")
    if len(names) != len(set(names)):
        raise SqlError("duplicate INSERT column")
    for name, value in zip(names, command.values):
        if name not in columns:
            raise SqlError(f"unknown column {name!r}")
        validate_value(name, value, columns)
    if len(names) != len(columns) - 1:
        raise SqlError("INSERT requires a value for every column except record_id")
    return dict(zip(names, command.values))


def serialize_record(values):
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(values)
    return output.getvalue().encode("utf-8")


def append_record(file, payload, eof):
    if eof:
        file.seek(eof - 1)
        if file.read(1) != b"\n":
            payload = b"\n" + payload
            offset = eof + 1
        else:
            offset = eof
    else:
        offset = eof
    file.seek(eof)
    if file.write(payload) != len(payload):
        raise OSError("short write")
    file.flush()
    return offset


def restore_eof(file, eof):
    file.truncate(eof)
    file.flush()
    if file.seek(0, io.SEEK_END) != eof:
        raise OSError("restored size does not match the previous EOF")


class CsvStorage:
    REGISTERED_TABLES = {"users": "users.csv"}

    def __init__(self, directory=None, *, catalog=None, max_keys=DEFAULT_MAX_KEYS):
        self.directory = Path.cwd() / "tables" if directory is None else Path(directory)
        registered = self.REGISTERED_TABLES if catalog is None else catalog
        self.catalog = {name.casefold(): filename for name, filename in registered.items()}
        self.max_keys = max_keys
        self.tables = {}
        self.blocked_tables = set()
        self._batch = None

    @contextmanager
    def batch_writes(self):
        with ExitStack() as stack:
            self._batch = stack, {}
            try:
                yield
            finally:
                self._batch = None

    def open_writer(self, path):
        if self._batch is None:
            return path.open("r+b", buffering=0)
        stack, writers = self._batch
        if path not in writers:
            writers[path] = stack.enter_context(path.open("r+b", buffering=0))
        return nullcontext(writers[path])

    def resolve_table(self, name):
        directory = self.directory.resolve()
        path = (directory / self.catalog[name]).resolve()
        if directory not in path.parents:
            raise StorageError("CSV path escapes the tables directory")
        return path

    def build_table(self, path):
        with path.open("rb") as file:
            columns = schema(file)
            table = Table(
                path, columns, tuple(columns).index("record_id"), BPlusTree(self.max_keys)
            )
            number = 2
            while True:
                offset = file.tell()
                raw = file.readline()
                if not raw:
                    break
                try:
                    row = parse_row(raw, columns)
                    record_id = row[table.record_id_index]
                    table.index.insert(record_id, offset)
                except (StorageError, DuplicateKeyError) as exc:
                    raise StorageError(f"line {number}: {exc}") from exc
                if record_id > table.max_id:
                    table.max_id = record_id
                number += 1
        return table

    def build_indexes(self):
        failures = {}
        for name in self.catalog:
            try:
                self.tables[name] = self.build_table(self.resolve_table(name))
            except (StorageError, OSError, RuntimeError) as exc:
                self.tables.pop(name, None)
                self.blocked_tables.add(name)
                failures[name] = str(exc)
        return failures

    def insert(self, name, command, table):
        values = insert_values(command, table.columns)
        if table.max_id == MAX_UINT64:
            raise SqlError("record_id exhausted at unsigned 64-bit maximum")
        record_id = table.max_id + 1
        if table.index.search(record_id) is not None:
            raise StorageError(f"duplicate record_id {record_id}")
        values["record_id"] = record_id
        payload = serialize_record(values[column] for column in table.columns)

        # Unbuffered writes cannot be retried implicitly while closing after rollback.
        with self.open_writer(table.path) as file:
            eof = file.seek(0, io.SEEK_END)
            registering = False
            try:
                offset = append_record(file, payload, eof)
                registering = True
                table.index.insert(record_id, offset)
            except Exception as exc:
                try:
                    restore_eof(file, eof)
                    if registering:
                        self.tables.pop(name)
                        self.tables[name] = self.build_table(table.path)
                except Exception as restore_exc:
                    self.tables.pop(name, None)
                    self.blocked_tables.add(name)
                    raise RestoreError(
                        f"INSERT failed ({exc}) and restoration failed ({restore_exc})"
                    ) from restore_exc
                raise StorageError(f"INSERT failed ({exc}); previous state restored") from exc
            table.max_id = record_id

    def read_indexed_row(self, table, key, offset):
        with table.path.open("rb") as file:
            try:
                file.seek(offset)
            except (OSError, OverflowError, ValueError) as exc:
                raise StorageError(f"invalid CSV offset {offset}: {exc}") from exc
            raw = file.readline()
        if not raw:
            raise StorageError(f"no CSV row at offset {offset}")
        try:
            row = parse_row(raw, table.columns)
        except StorageError as exc:
            raise StorageError(f"offset {offset}: {exc}") from exc
        if row[table.record_id_index] != key:
            raise StorageError(f"record_id mismatch at offset {offset}: expected {key}")
        return row

    def read_sequential_rows(self, table):
        with table.path.open("rb") as file:
            schema(file)
            for number, raw in enumerate(file, start=2):
                try:
                    yield parse_row(raw, table.columns)
                except StorageError as exc:
                    raise StorageError(f"line {number}: {exc}") from exc

    def select(self, command, table, *, force_linear=False):
        names = tuple(table.columns)
        if command.columns is not None:
            names = tuple(name.casefold() for name in command.columns)
        positions = {name: index for index, name in enumerate(table.columns)}
        for name in names:
            if name not in positions:
                raise SqlError(f"unknown column {name!r}")
        projection = tuple(positions[name] for name in names)
        output_columns = tuple(table.columns[name][0] for name in names)

        if command.condition is not None:
            column, value = command.condition
            column = column.casefold()
            if column not in positions:
                raise SqlError(f"unknown column {column!r}")
            validate_value(column, value, table.columns)
            if column == "record_id" and not force_linear:
                offset = table.index.search(value)
                if offset is None:
                    return Selected(output_columns, [])
                row = self.read_indexed_row(table, value, offset)
                return Selected(output_columns, [tuple(row[index] for index in projection)])
            condition_index = positions[column]

        rows = []
        for row in self.read_sequential_rows(table):
            if command.condition is None or row[condition_index] == value:
                rows.append(tuple(row[index] for index in projection))
        return Selected(output_columns, rows)

    def execute(self, command, *, force_linear=False):
        name = command.table.casefold()
        if name not in self.catalog:
            raise SqlError(f"unregistered table {name!r}")
        if name in self.blocked_tables:
            raise StorageError(f"table {name!r} is blocked after an index or restoration error")
        try:
            table = self.tables[name]
            if isinstance(command, Insert):
                self.insert(name, command, table)
                return None
            return self.select(command, table, force_linear=force_linear)
        except (OSError, RuntimeError) as exc:
            raise StorageError(f"cannot access table {name!r}: {exc}") from exc
