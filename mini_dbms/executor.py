from collections.abc import Callable

from mini_dbms.catalog import Catalog, TableState
from mini_dbms.errors import ConstraintError, TableUnavailableError
from mini_dbms.models import Insert, Inserted, Select, Selected
from mini_dbms.storage import CsvStorage


class Executor:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog: Catalog = catalog

    def execute(
        self, command: Insert | Select, *, use_index: bool = True,
        on_csv_row: Callable[[], None] | None = None,
    ) -> Inserted | Selected:
        match command:
            case Insert():
                return self._execute_insert(command)
            case Select():
                return self._execute_select(command, use_index=use_index, on_csv_row=on_csv_row)
            case _:
                assert_never(command)

    def _execute_insert(self, command: Insert) -> Inserted:
        state = self._catalog.get(command.table)
        with state.lock.write_lock():
            self._ensure_available(state, command.table)
            row = self._build_row(state, command.columns, command.values)
            record_id = self._id_value(state.schema, row)
            if state.index.find(record_id) is not None:
                raise ConstraintError(f"duplicate id: {record_id}")
            old_eof = state.path.stat().st_size
            try:
                offset = CsvStorage.append_row(state.path, state.schema, row)
                state.index.insert(record_id, offset)
                state.max_id = self._update_max_id(state.max_id, record_id)
                return Inserted(record_id)
            except Exception:
                self._recover(state, command.table, old_eof)
                raise

    def _execute_select(
        self, command: Select, *, use_index: bool, on_csv_row: Callable[[], None] | None
    ) -> Selected:
        state = self._catalog.get(command.table)
        with state.lock.read_lock():
            self._ensure_available(state, command.table)
            schema_names = [name for name, _ in state.schema]
            output_columns = (
                command.columns if command.columns is not None else schema_names
            )
            self._ensure_columns_known(schema_names, output_columns)
            where_column, where_value = None, None
            if command.where is not None:
                where_column, where_value = command.where
                self._ensure_columns_known(schema_names, [where_column])
                self._ensure_type_match(state.schema, where_column, where_value)

            rows = self._collect_rows(state, use_index, where_column, where_value, on_csv_row)
            projected = [
                self._project(row, schema_names, output_columns) for row in rows
            ]
            return Selected(output_columns, projected)

    def _build_row(
        self, state: TableState, columns: list[str] | None, values: list[int | str]
    ) -> list[int | str]:
        schema = state.schema
        schema_names = [name for name, _ in schema]
        if columns is None:
            if len(values) != len(schema):
                raise ConstraintError("value count mismatch")
            ordered = values
        else:
            if len(columns) != len(values):
                raise ConstraintError("column count and value count mismatch")
            if len(set(columns)) != len(columns):
                raise ConstraintError("duplicate column name")
            for col in columns:
                if col not in schema_names:
                    raise ConstraintError(f"unknown column: {col}")
            provided = dict(zip(columns, values, strict=True))
            if "id" not in provided:
                provided["id"] = self._auto_id(state)
            for name in schema_names:
                if name not in provided:
                    raise ConstraintError(f"missing column: {name}")
            ordered = [provided[name] for name in schema_names]
        return self._validate_types(schema, ordered)

    def _validate_types(
        self, schema: list[tuple[str, str]], row: list[int | str]
    ) -> list[int | str]:
        if len(row) != len(schema):
            raise ConstraintError("row width mismatch")
        result: list[int | str] = []
        for (name, col_type), value in zip(schema, row, strict=True):
            if col_type == "int":
                if type(value) is not int:
                    raise ConstraintError(f"{name} expects int")
            else:
                if not isinstance(value, str):
                    raise ConstraintError(f"{name} expects string")
                if "\n" in value or "\r" in value:
                    raise ConstraintError(f"{name} contains newline")
            result.append(value)
        return result

    def _auto_id(self, state: TableState) -> int:
        if state.max_id is None:
            return 1
        return state.max_id + 1

    def _update_max_id(self, current: int | None, record_id: int) -> int:
        if current is None:
            return record_id
        return max(current, record_id)

    def _id_value(self, schema: list[tuple[str, str]], row: list[int | str]) -> int:
        index = next(i for i, (name, _) in enumerate(schema) if name == "id")
        value = row[index]
        if not isinstance(value, int):
            raise ConstraintError("id must be int")
        return value

    def _collect_rows(
        self,
        state: TableState,
        use_index: bool,
        where_column: str | None,
        where_value: int | str | None,
        on_csv_row: Callable[[], None] | None,
    ) -> list[list[int | str]]:
        schema_names = [name for name, _ in state.schema]
        rows: list[list[int | str]] = []
        if (
            use_index
            and where_column == "id"
            and isinstance(where_value, int)
        ):
            offset = state.index.find(where_value)
            if offset is not None:
                row = CsvStorage.read_row_at(
                    state.path, state.schema, offset, expected_id=where_value
                )
                rows.append(row)
                if on_csv_row is not None:
                    on_csv_row()
        else:
            index = schema_names.index(where_column) if where_column is not None else None
            for _, row in CsvStorage.scan_rows(state.path, state.schema):
                if on_csv_row is not None:
                    on_csv_row()
                if index is None or row[index] == where_value:
                    rows.append(row)
                    if where_column == "id":
                        break
        return rows

    def _project(
        self, row: list[int | str], schema_names: list[str], output_columns: list[str]
    ) -> list[int | str]:
        return [row[schema_names.index(name)] for name in output_columns]

    def _ensure_available(self, state: TableState, name: str) -> None:
        if not state.available:
            raise TableUnavailableError(f"table {name} is unavailable")

    def _ensure_columns_known(
        self, schema_names: list[str], columns: list[str]
    ) -> None:
        for name in columns:
            if name not in schema_names:
                raise ConstraintError(f"unknown column: {name}")

    def _ensure_type_match(
        self,
        schema: list[tuple[str, str]],
        column: str,
        value: int | str,
    ) -> None:
        col_type = next(t for n, t in schema if n == column)
        if col_type == "int" and type(value) is not int:
            raise ConstraintError(f"{column} expects int")
        if col_type == "string" and not isinstance(value, str):
            raise ConstraintError(f"{column} expects string")

    def _recover(self, state: TableState, name: str, old_eof: int) -> None:
        try:
            CsvStorage.truncate(state.path, old_eof)
            self._catalog.rebuild_index(name)
        except Exception as error:
            state.available = False
            raise TableUnavailableError(f"recovery failed for {name}") from error
from typing import assert_never

