import contextlib
import io
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "mini-dbms"
sys.path.insert(0, str(SOURCE))

import cli
from bptree import BPlusTree, DuplicateKeyError
from sql import SqlError, parse, statements
import storage


HEADER = b"record_id:int,name:string,seat:int"
INSERT = "INSERT INTO users (name, seat) VALUES ('Kim', 9);"


class ParseTests(unittest.TestCase):
    def test_quoted_delimiter_does_not_split_batch(self):
        source = " ;\n iNsErT INTO users (name,seat) VALUES ('O''Brien, 민;수', 7);\nSELECT * FROM users;"
        first, second = tuple(statements(source))
        self.assertEqual(parse(first).values, ("O'Brien, 민;수", 7))
        self.assertEqual(parse(second).table, "users")

    def test_unsupported_sql_reports_parse_error(self):
        for query in (
            "SELECT * FROM users WHERE seat > 0;",
            "SELECT * FROM users WHERE seat = 4 AND name = 'Kim';",
            "SELECT * FROM users; trailing",
            "SELECT * FROM users",
            "INSERT INTO users (name,seat) VALUES ('unclosed, 7);",
            "INSERT INTO users (name,seat VALUES ('Kim', 7);",
            "UPDATE users SET seat = 8;",
            "DELETE FROM users;",
            "CREATE TABLE users;",
        ):
            with self.subTest(query=query), self.assertRaises(SqlError):
                parse(query)


class TreeTests(unittest.TestCase):
    def inspect_node(self, node, depth=0):
        self.assertEqual(node.keys, sorted(set(node.keys)))
        self.assertLessEqual(len(node.keys), 3)
        if not node.children:
            self.assertEqual(len(node.keys), len(node.offsets))
            return list(node.keys), {depth}
        self.assertEqual(len(node.children), len(node.keys) + 1)
        groups, depths = [], set()
        for child in node.children:
            keys, child_depths = self.inspect_node(child, depth + 1)
            groups.append(keys)
            depths.update(child_depths)
        self.assertEqual(node.keys, [keys[0] for keys in groups[1:]])
        self.assertTrue(all(left[-1] < right[0] for left, right in zip(groups, groups[1:])))
        self.assertEqual(len(depths), 1)
        return [key for group in groups for key in group], depths

    def test_order_and_structure_after_each_insert(self):
        inputs = list(range(80))
        cases = [("ascending", inputs), ("descending", list(reversed(inputs)))]
        for seed in (3, 145, 2026):
            shuffled = inputs.copy()
            random.Random(seed).shuffle(shuffled)
            cases.append((seed, shuffled))
        for label, order in cases:
            with self.subTest(order=label, keys=order):
                tree = BPlusTree(max_keys=3)
                self.assertIsNone(tree.search(50))
                reference = {}
                for key in order:
                    offset = key * 87 + 31
                    tree.insert(key, offset)
                    reference[key] = offset
                    tree_keys, depths = self.inspect_node(tree.root)
                    self.assertEqual(tree_keys, sorted(reference))
                    self.assertEqual({known: tree.search(known) for known in reference}, reference)
                self.assertGreaterEqual(next(iter(depths)), 2)
                self.assertIsNone(tree.search(999))
                with self.assertRaises(DuplicateKeyError):
                    tree.insert(34, 999)
                self.assertEqual(tree.search(34), reference[34])

    def test_internal_split_promotes_upper_middle(self):
        tree = BPlusTree(max_keys=3)
        for key in range(10):
            tree.insert(key, key)
        self.assertEqual(tree.root.keys, [6])
        self.assertEqual([child.keys for child in tree.root.children], [[2, 4], [8]])
        self.assertEqual(tree.search(6), 6)


class StorageTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.path = self.directory / "users.csv"
        self.path.write_bytes(HEADER + b"\n")

    def build(self, catalog=None):
        db = storage.CsvStorage(self.directory, catalog=catalog, max_keys=3)
        self.assertEqual(db.build_indexes(), {})
        return db

    def test_header_without_lf_and_utf8_offsets(self):
        self.path.write_bytes(HEADER)
        db = self.build()
        db.execute(parse("INSERT INTO users (seat,name) VALUES (4,'민,수');"))
        after_first = HEADER + '\n1,"민,수",4\n'.encode("utf-8")
        self.assertEqual(self.path.read_bytes(), after_first)
        self.assertEqual(db.tables["users"].index.search(1), len(HEADER) + 1)

        without_final_lf = after_first[:-1]
        self.path.write_bytes(without_final_lf)
        db = self.build()
        db.execute(parse(INSERT))
        self.assertEqual(db.tables["users"].index.search(2), len(without_final_lf) + 1)
        self.assertEqual(db.execute(parse("SELECT name FROM users WHERE record_id = 1;")).rows, [("민,수",)])
        self.assertEqual(db.execute(parse("SELECT name FROM users WHERE record_id = 2;")).rows, [("Kim",)])

    def test_auto_id_state_uses_each_tables_maximum(self):
        self.path.write_bytes(HEADER + b"\n40,A,1\n5,B,2\n")
        (self.directory / "other.csv").write_bytes(HEADER + b"\n")
        db = self.build({"users": "users.csv", "other": "other.csv"})
        original = self.path.read_bytes()
        for query in (
            "INSERT INTO users VALUES ('bad', 3);",
            "INSERT INTO users (RECORD_ID,name,seat) VALUES (90,'bad',3);",
            "INSERT INTO users (name,seat) VALUES ('bad','3');",
            "INSERT INTO users (name,seat) VALUES ('bad',-1);",
            "INSERT INTO users (name,seat) VALUES ('bad',18446744073709551616);",
            "INSERT INTO users (name) VALUES ('bad');",
            "INSERT INTO users (name,NAME) VALUES ('bad','duplicate');",
            "INSERT INTO users (name,missing) VALUES ('bad',3);",
            "INSERT INTO users (name,seat) VALUES ('bad');",
            "INSERT INTO users (name,seat) VALUES ('first\nsecond',3);",
        ):
            with self.subTest(query=query), self.assertRaises((SqlError, storage.StorageError)):
                db.execute(parse(query))
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(db.tables["users"].max_id, 40)
        db.execute(parse(INSERT))
        db.execute(parse(INSERT.replace("users", "other")))
        self.assertEqual(db.tables["users"].max_id, 41)
        self.assertEqual(db.tables["other"].max_id, 1)

    def test_uint64_limits_and_id_exhaustion(self):
        self.path.write_bytes(HEADER + b"\n0,A,0\n18446744073709551615,B,18446744073709551615\n")
        original = self.path.read_bytes()
        db = self.build()
        for key in (0, 2**64 - 1):
            self.assertEqual(db.execute(parse(f"SELECT seat FROM users WHERE record_id = {key};")).rows, [(key,)])
        with self.assertRaises((SqlError, storage.StorageError)):
            db.execute(parse(INSERT))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(db.tables["users"].max_id, 2**64 - 1)

    def test_build_rejects_bad_rows_but_keeps_other_table(self):
        (self.directory / "other.csv").write_bytes(HEADER + b"\n3,A,2\n")
        for raw in (
            HEADER + b"\r\n", HEADER + b"\n1,A,2\r\n",
            HEADER + b"\n1,A,2\n1,B,3\n", HEADER + b"\n-1,A,2\n",
            HEADER + b"\n1,A,18446744073709551616\n", HEADER + b"\n1,\xff,2\n",
            HEADER + b"\n1,A\n", HEADER + b'\n1,"broken,2\n',
            b"record_id:string,name:string,seat:int\n",
            b"record_id:int,NAME:string,name:string\n",
        ):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                db = storage.CsvStorage(self.directory, catalog={"users": "users.csv", "other": "other.csv"})
                self.assertEqual(set(db.build_indexes()), {"users"})
                for query in (INSERT, "SELECT * FROM users;"):
                    with self.assertRaises(storage.StorageError):
                        db.execute(parse(query))
                self.assertEqual(db.execute(parse("SELECT record_id FROM other;")).rows, [(3,)])

    def test_index_path_scan_equivalence_and_bad_offsets(self):
        first = HEADER + b"\n1,A,8\n"
        self.path.write_bytes(first + b"2,B,9\n")
        db = self.build()
        tree_type = type(db.tables["users"].index)
        for key in (1, 2, 99):
            query = parse(f"SELECT seat,name FROM users WHERE record_id = {key};")
            with (
                patch.object(tree_type, "search", autospec=True,
                             side_effect=tree_type.search) as search,
                patch.object(db, "read_indexed_row", wraps=db.read_indexed_row) as indexed_read,
                patch.object(db, "read_sequential_rows", wraps=db.read_sequential_rows) as scan,
            ):
                indexed = db.execute(query)
                self.assertEqual(search.call_args.args[1], key)
                self.assertEqual(indexed_read.call_count, int(key != 99))
                scan.assert_not_called()
                search.reset_mock()
                indexed_read.reset_mock()
                self.assertEqual(db.execute(query, force_linear=True), indexed)
                search.assert_not_called()
                indexed_read.assert_not_called()
                scan.assert_called_once()
        with patch.object(tree_type, "search", side_effect=AssertionError("unexpected tree search")):
            self.assertEqual(db.execute(parse("SELECT seat FROM users WHERE name = 'B';")).rows, [(9,)])
            self.assertEqual(db.execute(parse("SELECT seat FROM users;")).rows, [(8,), (9,)])
        for wrong_offset in (len(first), 90000):
            db = self.build()
            with patch.object(tree_type, "search", return_value=wrong_offset), self.assertRaises(storage.StorageError):
                db.execute(parse("SELECT * FROM users WHERE record_id = 1;"))

    def test_catalog_cannot_read_an_external_csv(self):
        root = self.directory
        inside = root / "tables"
        inside.mkdir()
        (inside / "users.csv").write_bytes(HEADER + b"\n")
        original = self.path.read_bytes()
        db = storage.CsvStorage(inside, catalog={"users": "users.csv", "external": "../users.csv"})
        self.assertEqual(set(db.build_indexes()), {"external"})
        with self.assertRaises(storage.StorageError):
            db.execute(parse("SELECT * FROM external;"))
        with self.assertRaises((SqlError, storage.StorageError)):
            db.execute(parse("INSERT INTO missing (name,seat) VALUES ('Kim', 9);"))
        self.assertEqual(self.path.read_bytes(), original)

    def test_partial_write_and_flush_failure_restore_state(self):
        for kind in ("partial write", "flush"):
            with self.subTest(kind=kind):
                original = HEADER + b"\n1,A,5"
                self.path.write_bytes(original)
                db = self.build()

                def fail_append(file, payload, eof):
                    file.seek(eof)
                    file.write(b"\n" + (payload[:3] if kind == "partial write" else payload))
                    raise OSError(f"injected {kind}")

                with db.batch_writes():
                    with patch("storage.append_record", side_effect=fail_append), self.assertRaises(storage.StorageError):
                        db.execute(parse(INSERT))
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertEqual(db.tables["users"].max_id, 1)
                    self.assertIsNone(db.tables["users"].index.search(2))
                    db.execute(parse(INSERT))
                    self.assertEqual(db.tables["users"].max_id, 2)

    def test_mutating_tree_error_rebuilds_or_blocks(self):
        for recovery in ("success", "truncate fails", "rebuild fails"):
            with self.subTest(recovery=recovery):
                original = HEADER + b"\n1,A,5\n"
                self.path.write_bytes(original)
                db = self.build()
                broken_tree = db.tables["users"].index
                tree_type = type(broken_tree)
                insert = tree_type.insert

                def fail_registration(instance, key, offset):
                    insert(instance, key, offset)
                    if instance is broken_tree:
                        raise OSError("injected partial tree mutation")

                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(tree_type, "insert", autospec=True,
                                                     side_effect=fail_registration))
                    if recovery == "truncate fails":
                        stack.enter_context(patch("storage.restore_eof", side_effect=OSError("truncate failed")))
                    if recovery == "rebuild fails":
                        stack.enter_context(patch.object(db, "build_table", side_effect=OSError("rebuild failed")))
                    with self.assertRaises(storage.StorageError):
                        db.execute(parse(INSERT))
                if recovery == "success":
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertEqual(db.tables["users"].max_id, 1)
                    self.assertIsNone(db.tables["users"].index.search(2))
                    db.execute(parse(INSERT))
                    self.assertEqual(db.tables["users"].max_id, 2)
                else:
                    for query in (INSERT, "SELECT * FROM users;"):
                        with self.assertRaises(storage.StorageError):
                            db.execute(parse(query))


class CliTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.tables = self.directory / "tables"
        self.tables.mkdir()
        (self.tables / "users.csv").write_bytes(HEADER)

    def run_batch(self, sql):
        query = self.directory / "query.sql"
        query.write_text(sql, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(SOURCE / "cli.py"), query.name],
            cwd=self.directory, text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def test_batch_failure_continues_and_restart_rebuilds(self):
        process = self.run_batch(
            "INSERT INTO users (seat,name) VALUES (4,'O''Brien, 민;수');"
            "SELECT * FROM users WHERE record_id > 1;"
            "INSERT INTO users (record_id,name,seat) VALUES (8,'bad',9);"
            "SELECT SEAT,NAME FROM USERS WHERE RECORD_ID = 1;"
            + INSERT
            + "SELECT record_id FROM users WHERE record_id = 2;"
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(process.stdout, "seat,name\n4,\"O'Brien, 민;수\"\nrecord_id\n2\n")
        self.assertIn("statement 2:", process.stderr)
        self.assertIn("statement 3:", process.stderr)
        restarted = self.run_batch(INSERT + "SELECT record_id FROM users;")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout, "record_id\n1\n2\n3\n")

    def test_startup_error_names_table_without_statement_number(self):
        (self.tables / "bad.csv").write_bytes(HEADER + b"\n1,A,2\n1,B,3\n")
        query = self.directory / "query.sql"
        query.write_text("SELECT * FROM users;", encoding="utf-8")
        db = storage.CsvStorage(self.tables, catalog={"bad": "bad.csv", "users": "users.csv"})
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("cli.CsvStorage", return_value=db), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main([str(query)]), 1)
        self.assertEqual(stdout.getvalue(), "record_id,name,seat\n")
        self.assertIn("bad", stderr.getvalue())
        self.assertIn("duplicate", stderr.getvalue().lower())
        self.assertNotIn("statement", stderr.getvalue().lower())

    def test_missing_sql_file_and_unclosed_tail(self):
        missing = subprocess.run(
            [sys.executable, "-B", str(SOURCE / "cli.py"), "absent.sql"],
            cwd=self.directory, text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertFalse(missing.stdout)
        self.assertTrue(missing.stderr)
        tail = self.run_batch(
            "SELECT * FROM users;"
            "INSERT INTO users (name,seat) VALUES ('unclosed;SELECT * FROM users;"
        )
        self.assertEqual(tail.returncode, 1, tail.stderr)
        self.assertEqual(tail.stdout, "record_id,name,seat\n")
        self.assertIn("statement 2:", tail.stderr)
        self.assertNotIn("statement 3:", tail.stderr)


if __name__ == "__main__":
    unittest.main()
