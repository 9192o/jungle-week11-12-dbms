import csv
import io
import sys
from collections.abc import Iterator
from pathlib import Path

from mini_dbms.errors import StorageError

# 쓰기가 허용한 문자열을 CSV 리더의 기본 128 KiB 제한 때문에 잃지 않도록 한다.
_ = csv.field_size_limit(sys.maxsize)


class CsvStorage:
    @staticmethod
    def read_schema(path: Path) -> list[tuple[str, str]]:
        try:
            with path.open("rb") as stream:
                header_bytes = stream.readline()
        except (OSError, UnicodeDecodeError) as error:
            raise StorageError(f"failed to read schema from {path}") from error
        if not header_bytes:
            raise StorageError(f"empty schema in {path}")
        try:
            header_line = header_bytes.decode("utf-8").removesuffix("\n").removesuffix("\r")
        except UnicodeDecodeError as error:
            raise StorageError(f"invalid UTF-8 in {path}") from error
        if "\n" in header_line or "\r" in header_line:
            raise StorageError("newline in schema")
        try:
            rows = list(csv.reader([header_line], strict=True))
        except csv.Error as error:
            raise StorageError(f"invalid CSV header in {path}") from error
        if not rows or not rows[0]:
            raise StorageError(f"empty schema in {path}")
        raw_columns = rows[0]
        schema: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in raw_columns:
            if ":" not in raw:
                raise StorageError(f"missing type in column {raw!r}")
            name_part, type_part = raw.split(":", 1)
            name = name_part.strip()
            col_type = type_part.strip()
            if not name:
                raise StorageError("empty column name")
            if name in seen:
                raise StorageError(f"duplicate column {name}")
            if col_type not in ("int", "string"):
                raise StorageError(f"unsupported type {col_type}")
            seen.add(name)
            schema.append((name, col_type))
        if not any(n == "id" and t == "int" for n, t in schema):
            raise StorageError("missing id:int column")
        return schema

    @staticmethod
    def scan_rows(
        path: Path, schema: list[tuple[str, str]]
    ) -> Iterator[tuple[int, list[int | str]]]:
        id_index = next(i for i, (name, _) in enumerate(schema) if name == "id")
        seen_ids: set[int] = set()
        try:
            with path.open("rb") as stream:
                _ = stream.readline()
                while True:
                    offset = stream.tell()
                    line_bytes = stream.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8")
                    row = CsvStorage._parse_line(line, schema)
                    record_id = row[id_index]
                    assert isinstance(record_id, int)
                    if record_id in seen_ids:
                        raise StorageError(f"duplicate id {record_id} in {path}")
                    seen_ids.add(record_id)
                    yield offset, row
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise StorageError(f"failed to read {path}") from error

    @staticmethod
    def read_row_at(
        path: Path,
        schema: list[tuple[str, str]],
        offset: int,
        expected_id: int | None = None,
    ) -> list[int | str]:
        id_index = next(i for i, (name, _) in enumerate(schema) if name == "id")
        try:
            with path.open("rb") as stream:
                _ = stream.seek(offset)
                line_bytes = stream.readline()
                if not line_bytes:
                    raise StorageError(f"no record at offset {offset}")
                line = line_bytes.decode("utf-8")
                row = CsvStorage._parse_line(line, schema)
                if expected_id is not None and row[id_index] != expected_id:
                    raise StorageError(
                        f"id mismatch at offset {offset}: expected {expected_id}, got {row[id_index]}"
                    )
                return row
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise StorageError(f"failed to read {path} at {offset}") from error

    @staticmethod
    def append_row(
        path: Path,
        schema: list[tuple[str, str]],
        row: list[int | str],
    ) -> int:
        validated = CsvStorage._validate_values(schema, row)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows([validated])
        row_bytes = output.getvalue().encode("utf-8")

        with path.open("r+b", buffering=0) as stream:
            _ = stream.seek(0, 2)
            old_eof = stream.tell()
            needs_newline = old_eof > 0 and not CsvStorage._ends_with_newline(stream, old_eof)
            if needs_newline:
                row_bytes = b"\n" + row_bytes
            try:
                written = stream.write(row_bytes)
                if written != len(row_bytes):
                    raise StorageError(f"short write: {written} != {len(row_bytes)}")
                stream.flush()
                return old_eof + (1 if needs_newline else 0)
            except Exception:
                CsvStorage._rollback(stream, old_eof)
                raise

    @staticmethod
    def truncate(path: Path, size: int) -> None:
        with path.open("r+b", buffering=0) as stream:
            _ = stream.truncate(size)
            stream.flush()

    @staticmethod
    def _rollback(stream: io.FileIO, old_eof: int) -> None:
        stream.flush()
        _ = stream.truncate(old_eof)
        stream.flush()

    @staticmethod
    def _parse_line(line: str, schema: list[tuple[str, str]]) -> list[int | str]:
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        if "\n" in line or "\r" in line:
            raise StorageError("newline in CSV record")
        try:
            rows = list(csv.reader([line], strict=True))
        except csv.Error as error:
            raise StorageError(f"invalid CSV record: {error}") from error
        if not rows:
            raise StorageError("empty record")
        parsed = rows[0]
        if len(parsed) != len(schema):
            raise StorageError(
                f"column count mismatch: {len(parsed)} != {len(schema)}"
            )
        return CsvStorage._coerce_row(schema, parsed)

    @staticmethod
    def _coerce_row(
        schema: list[tuple[str, str]], values: list[str]
    ) -> list[int | str]:
        result: list[int | str] = []
        for (name, col_type), value in zip(schema, values, strict=True):
            if col_type == "int":
                try:
                    result.append(int(value))
                except ValueError as error:
                    raise StorageError(f"invalid int in {name}: {value!r}") from error
            else:
                result.append(value)
        return result

    @staticmethod
    def _validate_values(
        schema: list[tuple[str, str]], row: list[int | str]
    ) -> list[int | str]:
        if len(row) != len(schema):
            raise StorageError("row width mismatch")
        result: list[int | str] = []
        for (name, col_type), value in zip(schema, row, strict=True):
            if col_type == "int":
                if type(value) is not int:
                    raise StorageError(f"expected int in {name}, got {type(value).__name__}")
            else:
                if not isinstance(value, str):
                    raise StorageError(
                        f"expected string in {name}, got {type(value).__name__}"
                    )
                if "\n" in value or "\r" in value:
                    raise StorageError(f"newline in {name}")
            result.append(value)
        return result

    @staticmethod
    def _ends_with_newline(stream: io.FileIO, size: int) -> bool:
        if size == 0:
            return False
        _ = stream.seek(size - 1)
        return stream.read(1) == b"\n"
