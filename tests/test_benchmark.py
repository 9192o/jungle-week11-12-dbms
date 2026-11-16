import tempfile
import unittest
from pathlib import Path

from mini_dbms.benchmark import run_benchmark


class BenchmarkTests(unittest.TestCase):
    def test_small_run_verifies_rows_and_reports_separate_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_benchmark(Path(directory), rows=25, repeats=2)
            self.assertEqual(report["rows"], 25)
            self.assertEqual(report["repeats"], 2)
            self.assertTrue(report["results_equal"])
            self.assertGreater(report["file_bytes"], 25)
            for name in (
                "preparation_seconds", "insertion_seconds", "index_build_seconds",
                "output_seconds",
            ):
                self.assertGreaterEqual(report[name], 0)
            self.assertEqual(
                set(report["queries"]), {"front", "middle", "end", "missing"}
            )
            for measurements in report["queries"].values():
                self.assertEqual(
                    set(measurements),
                    {"indexed_seconds", "linear_id_seconds", "linear_field_seconds"},
                )

    def test_rejects_invalid_size_without_creating_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for rows, repeats in ((0, 1), (1, 0), (-1, 2)):
                with self.subTest(rows=rows, repeats=repeats):
                    with self.assertRaises(ValueError):
                        run_benchmark(path, rows=rows, repeats=repeats)
            self.assertEqual(list(path.iterdir()), [])

    def test_existing_table_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            table = path / "benchmark.csv"
            table.write_bytes(b"keep existing data")
            with self.assertRaises(FileExistsError):
                run_benchmark(path, rows=5, repeats=1)
            self.assertEqual(table.read_bytes(), b"keep existing data")


if __name__ == "__main__":
    unittest.main()
