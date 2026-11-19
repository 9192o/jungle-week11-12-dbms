import time
from dataclasses import dataclass
from pathlib import Path

from mini_dbms.bptree import BPlusTree
from mini_dbms.errors import StorageError, TableUnavailableError
from mini_dbms.locks import ReadWriteLock
from mini_dbms.storage import CsvStorage


@dataclass
class TableState:
    schema: list[tuple[str, str]]
    path: Path
    index: BPlusTree
    max_id: int | None
    lock: ReadWriteLock
    available: bool = True


class Catalog:
    def __init__(
        self, directory: Path | str, tables: dict[str, str] | None = None
    ) -> None:
        self.directory: Path = Path(directory).resolve()
        self._states: dict[str, TableState] = {}
        if not self.directory.exists():
            raise StorageError(f"directory not found: {self.directory}")
        if not self.directory.is_dir():
            raise StorageError(f"not a directory: {self.directory}")

        started = time.perf_counter()
        if tables is None:
            for path in sorted(self.directory.glob("*.csv")):
                self._load_table(path.stem, path)
        else:
            for name, filename in tables.items():
                table_path = (self.directory / filename).resolve()
                try:
                    _ = table_path.relative_to(self.directory)
                except ValueError as error:
                    raise StorageError(
                        f"table path {filename} escapes directory"
                    ) from error
                self._load_table(name, table_path)
        self.index_build_seconds: float = time.perf_counter() - started

    def get(self, name: str) -> TableState:
        if name not in self._states:
            raise StorageError(f"unknown table: {name}")
        state = self._states[name]
        if not state.available:
            raise TableUnavailableError(f"table {name} is unavailable")
        return state

    def rebuild_index(self, name: str) -> None:
        if name not in self._states:
            raise StorageError(f"unknown table: {name}")
        state = self._states[name]
        schema = CsvStorage.read_schema(state.path)
        index = BPlusTree()
        max_id: int | None = None
        for offset, row in CsvStorage.scan_rows(state.path, schema):
            record_id = row[next(i for i, (n, _) in enumerate(schema) if n == "id")]
            assert isinstance(record_id, int)
            index.insert(record_id, offset)
            max_id = record_id if max_id is None else max(max_id, record_id)
        state.schema = schema
        state.index = index
        state.max_id = max_id
        state.available = True

    def _load_table(self, name: str, path: Path) -> None:
        resolved = path.resolve()
        try:
            _ = resolved.relative_to(self.directory)
        except ValueError as error:
            raise StorageError(f"table path {path} escapes directory") from error
        if not resolved.exists():
            raise StorageError(f"table file not found: {path}")
        path = resolved
        schema = CsvStorage.read_schema(path)
        index = BPlusTree()
        max_id: int | None = None
        for offset, row in CsvStorage.scan_rows(path, schema):
            record_id = row[next(i for i, (n, _) in enumerate(schema) if n == "id")]
            assert isinstance(record_id, int)
            try:
                index.insert(record_id, offset)
            except ValueError as error:
                raise StorageError(f"duplicate id {record_id} in {path}") from error
            max_id = record_id if max_id is None else max(max_id, record_id)
        self._states[name] = TableState(
            schema=schema,
            path=path,
            index=index,
            max_id=max_id,
            lock=ReadWriteLock(),
        )
