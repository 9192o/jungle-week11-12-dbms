import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_dbms.bptree import BPlusTree
from mini_dbms.catalog import Catalog, TableState
from mini_dbms.errors import StorageError, TableUnavailableError
from mini_dbms.locks import ReadWriteLock
from mini_dbms.storage import CsvStorage


class _PartialWriteFile:
    def __init__(self, real, fail_after_bytes):
        self._real = real
        self._fail_after = fail_after_bytes
        self._written = 0

    def write(self, data):
        allowed = max(0, self._fail_after - self._written)
        if allowed == 0:
            raise IOError("partial write")
        to_write = data[:allowed]
        written = self._real.write(to_write)
        self._written += written
        if self._written >= self._fail_after:
            raise IOError("partial write")
        return written

    def flush(self):
        return self._real.flush()

    def seek(self, *args, **kwargs):
        return self._real.seek(*args, **kwargs)

    def tell(self):
        return self._real.tell()

    def read(self, *args, **kwargs):
        return self._real.read(*args, **kwargs)

    def truncate(self, *args, **kwargs):
        return self._real.truncate(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)


class _FailingFlushFile:
    def __init__(self, real):
        self._real = real
        self._failed = False

    def flush(self):
        if not self._failed:
            self._failed = True
            raise IOError("flush failure")
        return self._real.flush()

    def write(self, data):
        return self._real.write(data)

    def seek(self, *args, **kwargs):
        return self._real.seek(*args, **kwargs)

    def tell(self):
        return self._real.tell()

    def read(self, *args, **kwargs):
        return self._real.read(*args, **kwargs)

    def truncate(self, *args, **kwargs):
        return self._real.truncate(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)


class SchemaValidationTests(unittest.TestCase):
    def test_valid_header_with_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id : int , name : string , seat : int\n", encoding="utf-8")
            schema = CsvStorage.read_schema(path)
            self.assertEqual(schema, [("id", "int"), ("name", "string"), ("seat", "int")])

    def test_duplicate_header_names_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id:int,id:string\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                CsvStorage.read_schema(path)

    def test_unsupported_type_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id:int,name:bool\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                CsvStorage.read_schema(path)

    def test_missing_id_column_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("name:string,seat:int\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                CsvStorage.read_schema(path)

    def test_id_not_int_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id:string,name:string\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                CsvStorage.read_schema(path)

    def test_empty_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes(b"")
            with self.assertRaises(StorageError):
                CsvStorage.read_schema(path)


class ScanRowsTests(unittest.TestCase):
    def test_lf_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n2,b\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            rows = list(CsvStorage.scan_rows(path, schema))
            self.assertEqual(rows, [(19, [1, "a"]), (23, [2, "b"])])

    def test_crlf_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\r\n1,a\r\n2,b\r\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            rows = list(CsvStorage.scan_rows(path, schema))
            self.assertEqual(rows, [(20, [1, "a"]), (25, [2, "b"])])

    def test_missing_terminal_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n2,b".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            rows = list(CsvStorage.scan_rows(path, schema))
            self.assertEqual(rows, [(19, [1, "a"]), (23, [2, "b"])])

    def test_duplicate_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n1,b\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                list(CsvStorage.scan_rows(path, schema))

    def test_invalid_int_field_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\nx,b\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                list(CsvStorage.scan_rows(path, schema))

    def test_row_width_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                list(CsvStorage.scan_rows(path, schema))

    def test_independent_file_handles_per_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n2,b\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            first = CsvStorage.scan_rows(path, schema)
            second = CsvStorage.scan_rows(path, schema)
            self.assertEqual(next(first), (19, [1, "a"]))
            self.assertEqual(next(second), (19, [1, "a"]))
            self.assertEqual(list(first), [(23, [2, "b"])])
            self.assertEqual(list(second), [(23, [2, "b"])])


class ReadRowAtTests(unittest.TestCase):
    def test_reads_row_at_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,민수\n2,철수\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            row = CsvStorage.read_row_at(path, schema, 28)
            self.assertEqual(row, [2, "철수"])

    def test_verifies_id_matches_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n2,b\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                CsvStorage.read_row_at(path, schema, 19, expected_id=2)

    def test_missing_offset_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                CsvStorage.read_row_at(path, schema, 999)


class AppendRowTests(unittest.TestCase):
    def test_appends_when_terminal_newline_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            offset = CsvStorage.append_row(path, schema, [2, "b"])
            self.assertEqual(offset, 23)
            self.assertEqual(
                path.read_bytes(),
                "id:int,name:string\n1,a\n2,b\n".encode("utf-8"),
            )

    def test_appends_when_terminal_newline_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            offset = CsvStorage.append_row(path, schema, [2, "b"])
            self.assertEqual(offset, 23)
            self.assertEqual(
                path.read_bytes(),
                "id:int,name:string\n1,a\n2,b\n".encode("utf-8"),
            )

    def test_partial_write_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            original = "id:int,name:string\n1,a\n".encode("utf-8")
            path.write_bytes(original)
            schema = CsvStorage.read_schema(path)
            real_open = Path.open

            def fake_open(self, *args, **kwargs):
                file = real_open(self, *args, **kwargs)
                if self == path and args and args[0] == "r+b":
                    return _PartialWriteFile(file, 1)
                return file

            with patch.object(Path, "open", fake_open):
                with self.assertRaises(IOError):
                    CsvStorage.append_row(path, schema, [2, "b"])
            self.assertEqual(path.read_bytes(), original)

    def test_flush_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            original = "id:int,name:string\n1,a\n".encode("utf-8")
            path.write_bytes(original)
            schema = CsvStorage.read_schema(path)
            real_open = Path.open

            def fake_open(self, *args, **kwargs):
                file = real_open(self, *args, **kwargs)
                if self == path and args and args[0] == "r+b":
                    return _FailingFlushFile(file)
                return file

            with patch.object(Path, "open", fake_open):
                with self.assertRaises(IOError):
                    CsvStorage.append_row(path, schema, [2, "b"])
            self.assertEqual(path.read_bytes(), original)

    def test_type_mismatch_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_bytes("id:int,name:string\n1,a\n".encode("utf-8"))
            schema = CsvStorage.read_schema(path)
            with self.assertRaises(StorageError):
                CsvStorage.append_row(path, schema, ["x", "b"])


class CatalogTests(unittest.TestCase):
    def test_default_discovers_csv_stems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "students.csv").write_text("id:int,name:string\n", encoding="utf-8")
            (path / "rooms.csv").write_text("id:int,number:int\n", encoding="utf-8")
            catalog = Catalog(path)
            self.assertIn("students", catalog._states)
            self.assertIn("rooms", catalog._states)

    def test_explicit_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "data.csv").write_text("id:int,name:string\n", encoding="utf-8")
            catalog = Catalog(path, {"people": "data.csv"})
            state = catalog.get("people")
            self.assertEqual(state.schema, [("id", "int"), ("name", "string")])

    def test_path_safety_outside_directory_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(StorageError):
                Catalog(path, {"evil": "../outside.csv"})

    def test_corrupt_csv_at_startup_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "bad.csv").write_text("id:int,name:string\n1,a\nx,b\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                Catalog(path)

    def test_index_build_seconds_is_non_negative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "t.csv").write_text("id:int,name:string\n", encoding="utf-8")
            catalog = Catalog(path, {"t": "t.csv"})
            self.assertGreaterEqual(catalog.index_build_seconds, 0.0)

    def test_get_unknown_table_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory))
            with self.assertRaises(StorageError):
                catalog.get("missing")

    def test_get_unavailable_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "t.csv").write_text("id:int,name:string\n", encoding="utf-8")
            catalog = Catalog(path, {"t": "t.csv"})
            catalog._states["t"].available = False
            with self.assertRaises(TableUnavailableError):
                catalog.get("t")

    def test_rebuild_index_restores_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "t.csv").write_text("id:int,name:string\n1,a\n3,c\n", encoding="utf-8")
            catalog = Catalog(path, {"t": "t.csv"})
            state = catalog.get("t")
            old_index = state.index
            state.index = BPlusTree()
            state.max_id = None
            catalog.rebuild_index("t")
            self.assertEqual(state.index.find(1), 19)
            self.assertEqual(state.index.find(3), 23)
            self.assertEqual(state.max_id, 3)
            self.assertIsNot(state.index, old_index)

    def test_table_state_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "t.csv").write_text("id:int,name:string\n", encoding="utf-8")
            catalog = Catalog(path, {"t": "t.csv"})
            state = catalog.get("t")
            self.assertEqual(state.schema, [("id", "int"), ("name", "string")])
            self.assertEqual(state.path, path / "t.csv")
            self.assertIsInstance(state.index, BPlusTree)
            self.assertIsNone(state.max_id)
            self.assertIsInstance(state.lock, ReadWriteLock)
            self.assertTrue(state.available)


if __name__ == "__main__":
    unittest.main()
