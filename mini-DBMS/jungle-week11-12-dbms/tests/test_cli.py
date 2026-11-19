import csv
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.tables = self.directory / "tables"
        self.tables.mkdir()
        (self.tables / "students.csv").write_text(
            "id:int,name:string,seat:int\n", encoding="utf-8"
        )

    def run_sql(self, sql: str) -> subprocess.CompletedProcess[str]:
        source = self.directory / "query.sql"
        source.write_text(sql, encoding="utf-8")
        return self.run_file(source)

    def run_file(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, "-m", "mini_dbms", str(path),
                "--tables", str(self.tables),
            ],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    def test_batch_continues_after_errors_and_preserves_successes(self) -> None:
        result = self.run_sql(
            "INSERT INTO students VALUES (1, '민;수', 20);"
            "UPDATE students SET seat = 4;"
            "INSERT INTO students VALUES (1, '중복', 21);"
            "INSERT INTO students (name, seat) VALUES ('영희', 22);"
            "SELECT name, id FROM students;"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("statement 2", result.stderr)
        self.assertIn("statement 3", result.stderr)
        self.assertEqual(
            list(csv.reader(io.StringIO(result.stdout))),
            [["name", "id"], ["민;수", "1"], ["영희", "2"]],
        )

    def test_new_process_rebuilds_index_and_reads_existing_data(self) -> None:
        inserted = self.run_sql("INSERT INTO students VALUES (42, '민수', 20);")
        self.assertEqual(inserted.returncode, 0, inserted.stderr)
        selected = self.run_sql("SELECT * FROM students WHERE id = 42;")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(
            list(csv.reader(io.StringIO(selected.stdout))),
            [["id", "name", "seat"], ["42", "민수", "20"]],
        )

    def test_unclosed_literal_is_one_error_and_does_not_execute_its_tail(self) -> None:
        result = self.run_sql(
            "SELECT * FROM students;"
            "INSERT INTO students VALUES (1, 'broken; SELECT * FROM students;"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count("statement "), 1)
        self.assertEqual(result.stdout.strip(), "id,name,seat")
        self.assertEqual(
            (self.tables / "students.csv").read_text(encoding="utf-8"),
            "id:int,name:string,seat:int\n",
        )

    def test_missing_input_returns_failure(self) -> None:
        result = self.run_file(self.directory / "missing.sql")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr)
        self.assertEqual(result.stdout, "")

    def test_empty_file_succeeds(self) -> None:
        result = self.run_sql(" \n ; ; ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
