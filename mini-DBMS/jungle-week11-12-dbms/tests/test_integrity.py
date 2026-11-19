import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_dbms.catalog import Catalog
from mini_dbms.errors import ConstraintError, StorageError, TableUnavailableError
from mini_dbms.executor import Executor
from mini_dbms.models import Insert, Select, Selected
from mini_dbms.storage import CsvStorage


class StorageIntegrityTests(unittest.TestCase):
    def test_recovery_rebuilds_index_after_mutation_then_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.csv"
            before = b"id:int,name:string\n1,original\n"
            path.write_bytes(before)
            catalog = Catalog(directory)
            executor = Executor(catalog)
            state = catalog.get("students")
            original_insert = state.index.insert

            def mutate_then_fail(key: int, offset: int) -> None:
                original_insert(key, offset)
                raise RuntimeError("injected failure after index mutation")

            with patch.object(state.index, "insert", mutate_then_fail):
                with self.assertRaises(RuntimeError):
                    executor.execute(Insert("students", ["name"], ["failed"]))
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(state.index.find(2))
            self.assertIsNotNone(state.index.find(1))
            self.assertEqual(state.max_id, 1)
            self.assertEqual(
                executor.execute(Insert("students", ["name"], ["next"])).id, 2
            )

    def test_failed_rebuild_isolates_only_affected_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("students", "other"):
                (root / f"{name}.csv").write_bytes(b"id:int,name:string\n")
            catalog = Catalog(directory)
            executor = Executor(catalog)
            state = catalog.get("students")
            with (
                patch.object(state.index, "insert", side_effect=RuntimeError("injected")),
                patch.object(catalog, "rebuild_index", side_effect=StorageError("injected")),
            ):
                with self.assertRaises(TableUnavailableError):
                    executor.execute(Insert("students", ["name"], ["failed"]))
            with self.assertRaises(TableUnavailableError):
                executor.execute(Select("students"))
            self.assertEqual(
                executor.execute(Insert("other", ["name"], ["unaffected"])).id, 1
            )
            self.assertEqual(
                (root / "students.csv").read_bytes(), b"id:int,name:string\n"
            )

    def test_inserted_long_string_is_readable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.csv"
            path.write_bytes(b"id:int,name:string\n")
            name = "x" * 140_000
            Executor(Catalog(directory)).execute(Insert("students", None, [1, name]))
            self.assertEqual(
                Executor(Catalog(directory)).execute(Select("students")),
                Selected(["id", "name"], [[1, name]]),
            )

    def test_startup_rejects_unclosed_csv_quotes_and_invalid_utf8(self) -> None:
        invalid_rows = [
            b'1,"unclosed\n',
            b'1,"closed"tail\n',
            b"1,\xff\n",
            b'1,"two\nlines"\n',
            b'1,"embedded\rcarriage"\n',
        ]
        for row in invalid_rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "students.csv"
                contents = b"id:int,name:string\n" + row
                path.write_bytes(contents)
                with self.assertRaises(StorageError):
                    Catalog(directory)
                self.assertEqual(path.read_bytes(), contents)

    def test_csv_store_rejects_field_newlines_without_writing(self) -> None:
        for name in ("a\nb", "a\rb", "a\r\nb"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "students.csv"
                original = b"id:int,name:string\n"
                path.write_bytes(original)
                with self.assertRaises(StorageError):
                    CsvStorage.append_row(
                        path, [("id", "int"), ("name", "string")], [1, name]
                    )
                self.assertEqual(path.read_bytes(), original)

    def test_executor_rejects_field_newlines_without_issuing_auto_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.csv"
            original = b"id:int,name:string\n"
            path.write_bytes(original)
            catalog = Catalog(directory)
            executor = Executor(catalog)
            with self.assertRaises(ConstraintError):
                executor.execute(Insert("students", ["name"], ["a\nb"]))
            self.assertEqual(path.read_bytes(), original)
            self.assertIsNone(catalog.get("students").max_id)
            self.assertIsNone(catalog.get("students").index.find(1))

    def test_discovery_rejects_symlink_to_outside_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tables = root / "tables"
            tables.mkdir()
            outside = root / "outside.csv"
            original = b"id:int,name:string\n1,external\n"
            outside.write_bytes(original)
            (tables / "students.csv").symlink_to(outside)
            with self.assertRaises(StorageError):
                Catalog(tables)
            self.assertEqual(outside.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
