import random
import tempfile
import unittest
from pathlib import Path

from mini_dbms.catalog import Catalog
from mini_dbms.executor import Executor
from mini_dbms.models import Inserted, Selected
from mini_dbms.parser import parse, split_statements


class SqlCsvRoundtripTests(unittest.TestCase):
    def test_fixed_seed_literals_roundtrip_with_nonleading_id_column(self) -> None:
        generator = random.Random(20261116)
        alphabet = "abcXYZ012 한글민수,;'\"_"
        statements: list[str] = []
        expected: list[list[int | str]] = []
        for identifier in range(-30, 30):
            name = "".join(
                generator.choice(alphabet) for _ in range(generator.randrange(30))
            )
            seat = generator.randrange(-1000, 1000)
            literal = name.replace("'", "''")
            statements.append(
                f"INSERT INTO students (id, name, seat) "
                f"VALUES ({identifier}, '{literal}', {seat});"
            )
            expected.append([identifier, name, seat])

        with tempfile.TemporaryDirectory() as directory:
            table = Path(directory) / "students.csv"
            table.write_text("seat:int,id:int,name:string", encoding="utf-8")
            executor = Executor(Catalog(directory))
            commands = split_statements("\n".join(statements))
            self.assertEqual(len(commands), len(expected))
            for statement, row in zip(commands, expected):
                self.assertEqual(executor.execute(parse(statement)), Inserted(row[0]))
            self.assertEqual(
                executor.execute(parse("SELECT id, name, seat FROM students;")),
                Selected(["id", "name", "seat"], expected),
            )
            rebuilt = Executor(Catalog(directory))
            for row in expected:
                sql = f"SELECT id, name, seat FROM students WHERE id = {row[0]};"
                self.assertEqual(
                    rebuilt.execute(parse(sql)), Selected(["id", "name", "seat"], [row])
                )
            self.assertEqual(
                rebuilt.execute(
                    parse("INSERT INTO students (name, seat) VALUES ('next', 7);")
                ),
                Inserted(30),
            )


if __name__ == "__main__":
    unittest.main()
