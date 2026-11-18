import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_dbms.bptree import BPlusTree
from mini_dbms.catalog import Catalog
from mini_dbms.errors import ConstraintError, StorageError, TableUnavailableError
from mini_dbms.executor import Executor
from mini_dbms.models import Insert, Inserted, Select, Selected
from mini_dbms.storage import CsvStorage


class ExecutorInsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.table_path = self.directory / "students.csv"
        self.table_path.write_text("id:int,name:string,seat:int\n", encoding="utf-8")
        self.catalog = Catalog(self.directory, {"students": "students.csv"})
        self.executor = Executor(self.catalog)

    def test_positional_insert(self) -> None:
        result = self.executor.execute(
            Insert("students", None, [1, "민수", 20])
        )
        self.assertEqual(result, Inserted(1))
        self.assertIn("1,민수,20", self.table_path.read_text(encoding="utf-8"))

    def test_boolean_cannot_corrupt_integer_columns(self) -> None:
        before = self.table_path.read_bytes()
        for row in ([True, "test", 20], [1, "test", False]):
            with self.subTest(row=row):
                with self.assertRaises(ConstraintError):
                    self.executor.execute(Insert("students", None, row))
                self.assertEqual(self.table_path.read_bytes(), before)
        self.assertEqual(self.executor.execute(Insert("students", None, [1, "test", 20])), Inserted(1))
        for column in ("id", "seat"):
            with self.assertRaises(ConstraintError):
                self.executor.execute(Select("students", where=(column, True)))

    def test_storage_rejects_boolean_without_writing(self) -> None:
        before = self.table_path.read_bytes()
        with self.assertRaises(StorageError):
            CsvStorage.append_row(self.table_path, self.catalog.get("students").schema, [True, "test", 20])
        self.assertEqual(self.table_path.read_bytes(), before)

    def test_reordered_insert(self) -> None:
        result = self.executor.execute(
            Insert("students", ["name", "id", "seat"], ["영희", 3, 22])
        )
        self.assertEqual(result, Inserted(3))
        self.assertEqual(
            self.table_path.read_text(encoding="utf-8"),
            "id:int,name:string,seat:int\n3,영희,22\n",
        )

    def test_auto_id_empty_table(self) -> None:
        result = self.executor.execute(
            Insert("students", ["name", "seat"], ["민수", 20])
        )
        self.assertEqual(result, Inserted(1))

    def test_auto_id_after_explicit(self) -> None:
        self.executor.execute(Insert("students", None, [5, "민수", 20]))
        result = self.executor.execute(
            Insert("students", ["name", "seat"], ["영희", 22])
        )
        self.assertEqual(result, Inserted(6))

    def test_auto_id_after_negative_max(self) -> None:
        self.executor.execute(Insert("students", None, [-5, "민수", 20]))
        result = self.executor.execute(
            Insert("students", ["name", "seat"], ["영희", 22])
        )
        self.assertEqual(result, Inserted(-4))

    def test_duplicate_id_rejected(self) -> None:
        self.executor.execute(Insert("students", None, [1, "민수", 20]))
        with self.assertRaises(ConstraintError):
            self.executor.execute(Insert("students", None, [1, "중복", 21]))

    def test_wrong_value_count_rejected(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Insert("students", None, [1, "민수"]))

    def test_unknown_column_rejected(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(
                Insert("students", ["name", "unknown"], ["민수", 20])
            )

    def test_missing_non_id_column_rejected(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Insert("students", ["name"], ["민수"]))

    def test_invalid_int_type_rejected(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Insert("students", None, ["x", "민수", 20]))

    def test_invalid_string_type_rejected(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(
                Insert("students", ["id", "name", "seat"], [1, 123, 20])
            )

    def test_unknown_table_rejected(self) -> None:
        with self.assertRaises(StorageError):
            self.executor.execute(Insert("missing", None, [1, "민수", 20]))

    def test_insert_persists_across_new_catalog(self) -> None:
        self.executor.execute(Insert("students", None, [1, "민수", 20]))
        fresh = Executor(Catalog(self.directory, {"students": "students.csv"}))
        result = fresh.execute(Select("students", None, ("id", 1)))
        self.assertEqual(result.rows, [[1, "민수", 20]])

    def test_inserted_row_is_indexed(self) -> None:
        self.executor.execute(Insert("students", None, [7, "민수", 20]))
        state = self.catalog.get("students")
        offset = state.index.find(7)
        self.assertIsNotNone(offset)


class ExecutorSelectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.table_path = self.directory / "students.csv"
        self.table_path.write_text(
            "id:int,name:string,seat:int\n1,민수,20\n2,철수,21\n3,영희,22\n",
            encoding="utf-8",
        )
        self.catalog = Catalog(self.directory, {"students": "students.csv"})
        self.executor = Executor(self.catalog)

    def test_select_star(self) -> None:
        result = self.executor.execute(Select("students"))
        self.assertEqual(
            result,
            Selected(
                ["id", "name", "seat"],
                [[1, "민수", 20], [2, "철수", 21], [3, "영희", 22]],
            ),
        )

    def test_select_projected(self) -> None:
        result = self.executor.execute(
            Select("students", ["name", "id"])
        )
        self.assertEqual(
            result,
            Selected(
                ["name", "id"],
                [["민수", 1], ["철수", 2], ["영희", 3]],
            ),
        )

    def test_select_where_id(self) -> None:
        result = self.executor.execute(Select("students", None, ("id", 2)))
        self.assertEqual(result, Selected(["id", "name", "seat"], [[2, "철수", 21]]))

    def test_select_where_id_missing(self) -> None:
        result = self.executor.execute(Select("students", None, ("id", 99)))
        self.assertEqual(result, Selected(["id", "name", "seat"], []))

    def test_select_where_other_column(self) -> None:
        result = self.executor.execute(Select("students", ["id", "seat"], ("name", "영희")))
        self.assertEqual(result, Selected(["id", "seat"], [[3, 22]]))

    def test_select_use_index_false_forces_linear(self) -> None:
        result = self.executor.execute(
            Select("students", None, ("id", 2)), use_index=False
        )
        self.assertEqual(result, Selected(["id", "name", "seat"], [[2, "철수", 21]]))

    def test_select_unknown_output_column(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Select("students", ["unknown"]))

    def test_select_unknown_where_column(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Select("students", None, ("unknown", 1)))

    def test_select_where_type_mismatch(self) -> None:
        with self.assertRaises(ConstraintError):
            self.executor.execute(Select("students", None, ("id", "문자")))


class ExecutorRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.table_path = self.directory / "students.csv"
        self.table_path.write_text(
            "id:int,name:string,seat:int\n1,민수,20\n", encoding="utf-8"
        )
        self.catalog = Catalog(self.directory, {"students": "students.csv"})
        self.executor = Executor(self.catalog)

    def test_index_mutation_failure_recovers(self) -> None:
        state = self.catalog.get("students")
        original_offset = state.index.find(1)
        self.assertIsNotNone(original_offset)

        real_insert = state.index.insert

        def failing_insert(key: int, offset: int) -> None:
            if key == 2:
                raise RuntimeError("index mutation")
            real_insert(key, offset)

        state.index.insert = failing_insert

        with self.assertRaises(RuntimeError):
            self.executor.execute(
                Insert("students", ["name", "seat"], ["영희", 22])
            )

        self.assertEqual(
            self.table_path.read_text(encoding="utf-8"),
            "id:int,name:string,seat:int\n1,민수,20\n",
        )
        self.assertEqual(state.index.find(1), original_offset)
        self.assertIsNone(state.index.find(2))
        self.assertTrue(state.available)

    def test_recovery_failure_marks_table_unavailable(self) -> None:
        state = self.catalog.get("students")

        def failing_insert(key: int, offset: int) -> None:
            raise RuntimeError("index mutation")

        state.index.insert = failing_insert

        with patch.object(CsvStorage, "truncate", side_effect=IOError("rollback failed")):
            with self.assertRaises(TableUnavailableError):
                self.executor.execute(
                    Insert("students", ["name", "seat"], ["영희", 22])
                )

        self.assertFalse(state.available)
        with self.assertRaises(TableUnavailableError):
            self.executor.execute(Select("students"))


class ExecutorResultContractTests(unittest.TestCase):
    def test_insert_returns_inserted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id:int,name:string\n", encoding="utf-8")
            executor = Executor(Catalog(Path(directory), {"t": "t.csv"}))
            result = executor.execute(Insert("t", None, [1, "a"]))
            self.assertIsInstance(result, Inserted)
            self.assertEqual(result.id, 1)

    def test_select_returns_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.csv"
            path.write_text("id:int,name:string\n1,a\n", encoding="utf-8")
            executor = Executor(Catalog(Path(directory), {"t": "t.csv"}))
            result = executor.execute(Select("t"))
            self.assertIsInstance(result, Selected)
            self.assertEqual(result.columns, ["id", "name"])
            self.assertEqual(result.rows, [[1, "a"]])


if __name__ == "__main__":
    unittest.main()
