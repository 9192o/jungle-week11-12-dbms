import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PublicApiTests(unittest.TestCase):
    def test_library_executes_without_http_or_sibling_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "mini_dbms",
                Path(directory) / "mini_dbms",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            result = subprocess.run(
                [sys.executable, "-S", "-c", """
from pathlib import Path
from mini_dbms import Catalog, Executor, Insert, Select, Selected, ConstraintError
Path("books.csv").write_text("id:int,title:string\\n", encoding="utf-8")
executor = Executor(Catalog(Path("."), {"books": "books.csv"}))
executor.execute(Insert("books", values=[7, "A different domain"]))
result = executor.execute(Select("books", where=("id", 7)))
assert isinstance(result, Selected)
assert result.rows == [[7, "A different domain"]]
try:
    executor.execute(Insert("books", values=[7, "duplicate"]))
except ConstraintError:
    pass
else:
    raise AssertionError("duplicate accepted")
assert Executor(Catalog(Path("."))).execute(Select("books")).rows == result.rows
"""],
                cwd=directory, env={**os.environ, "PYTHONPATH": directory},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
