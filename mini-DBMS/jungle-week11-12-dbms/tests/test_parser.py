import unittest

from mini_dbms.errors import (
    ConstraintError,
    DBError,
    ParseError,
    StorageError,
    TableUnavailableError,
)
from mini_dbms.models import Insert, Inserted, Select, Selected
from mini_dbms.parser import ParseError as ParserParseError, parse, split_statements


class ErrorContractTests(unittest.TestCase):
    def test_dberror_base_class_code(self) -> None:
        error = DBError("execution failed")
        self.assertEqual(error.code, "execution_error")
        self.assertIsInstance(error, Exception)

    def test_parse_error_code_and_message(self) -> None:
        error = ParseError("unexpected token")
        self.assertEqual(error.code, "syntax_error")
        self.assertEqual(str(error), "unexpected token")

    def test_constraint_error_code(self) -> None:
        error = ConstraintError("duplicate id")
        self.assertEqual(error.code, "constraint_error")

    def test_storage_error_code(self) -> None:
        error = StorageError("io failed")
        self.assertEqual(error.code, "storage_error")

    def test_table_unavailable_error_code(self) -> None:
        error = TableUnavailableError("table broken")
        self.assertEqual(error.code, "table_unavailable")

    def test_subclass_inheritance(self) -> None:
        self.assertTrue(issubclass(ParseError, DBError))
        self.assertTrue(issubclass(ConstraintError, DBError))
        self.assertTrue(issubclass(StorageError, DBError))
        self.assertTrue(issubclass(TableUnavailableError, DBError))


class ModelContractTests(unittest.TestCase):
    def test_insert_positional_arguments_follow_shared_contract(self) -> None:
        insert = Insert("students", ["name", "seat"], ["민수", 20])
        self.assertEqual(insert.columns, ["name", "seat"])
        self.assertEqual(insert.values, ["민수", 20])

    def test_insert_defaults(self) -> None:
        insert = Insert(table="students", values=[1, "minsu"])
        self.assertEqual(insert.table, "students")
        self.assertIsNone(insert.columns)
        self.assertEqual(insert.values, [1, "minsu"])

    def test_insert_with_columns(self) -> None:
        insert = Insert(table="students", columns=["id", "name"], values=[2, "cheolsu"])
        self.assertEqual(insert.columns, ["id", "name"])

    def test_select_star(self) -> None:
        select = Select(table="students")
        self.assertEqual(select.table, "students")
        self.assertIsNone(select.columns)
        self.assertIsNone(select.where)

    def test_select_projected(self) -> None:
        select = Select(table="students", columns=["name", "seat"])
        self.assertEqual(select.columns, ["name", "seat"])

    def test_select_with_where(self) -> None:
        select = Select(table="students", where=("id", 1))
        self.assertEqual(select.where, ("id", 1))

    def test_selected_result(self) -> None:
        selected = Selected(columns=["name", "seat"], rows=[["minsu", 20]])
        self.assertEqual(selected.columns, ["name", "seat"])
        self.assertEqual(selected.rows, [["minsu", 20]])

    def test_inserted_result(self) -> None:
        inserted = Inserted(id=42)
        self.assertEqual(inserted.id, 42)


class SplitStatementsTests(unittest.TestCase):
    def test_escaped_quote_does_not_end_literal_before_semicolon(self) -> None:
        first = "INSERT INTO t VALUES ('O''Brien, 민;수');"
        second = " SELECT * FROM t;"
        self.assertEqual(split_statements(first + second), [first, second])

    def test_escaped_quote_in_unclosed_tail_never_exposes_sql(self) -> None:
        text = "INSERT INTO t VALUES ('O''Brien; SELECT * FROM t;"
        self.assertEqual(split_statements(text), [text])

    def test_empty_and_whitespace(self) -> None:
        self.assertEqual(split_statements(""), [])
        self.assertEqual(split_statements("   "), [])
        self.assertEqual(split_statements(";"), [])
        self.assertEqual(split_statements(" ; ; "), [])

    def test_single_statement(self) -> None:
        self.assertEqual(
            split_statements("SELECT * FROM t;"),
            ["SELECT * FROM t;"],
        )

    def test_preserves_terminating_semicolon(self) -> None:
        self.assertEqual(
            split_statements("INSERT INTO t VALUES (1);"),
            ["INSERT INTO t VALUES (1);"],
        )

    def test_multiple_statements(self) -> None:
        text = "SELECT * FROM a; INSERT INTO b VALUES (1);"
        self.assertEqual(
            split_statements(text),
            ["SELECT * FROM a;", " INSERT INTO b VALUES (1);"],
        )

    def test_ignores_empty_semicolon_only_statements(self) -> None:
        text = "SELECT * FROM a;; INSERT INTO b VALUES (1);"
        self.assertEqual(
            split_statements(text),
            ["SELECT * FROM a;", " INSERT INTO b VALUES (1);"],
        )

    def test_string_literal_semicolon_not_boundary(self) -> None:
        text = "INSERT INTO t VALUES ('a;b'); SELECT 1;"
        self.assertEqual(
            split_statements(text),
            ["INSERT INTO t VALUES ('a;b');", " SELECT 1;"],
        )

    def test_unterminated_tail_returned(self) -> None:
        text = "SELECT * FROM t"
        self.assertEqual(split_statements(text), ["SELECT * FROM t"])

    def test_unterminated_tail_after_valid(self) -> None:
        text = "SELECT * FROM t; SELECT 1"
        self.assertEqual(
            split_statements(text),
            ["SELECT * FROM t;", " SELECT 1"],
        )

    def test_newlines_between_statements(self) -> None:
        text = "SELECT 1;\nINSERT INTO t VALUES (2);"
        self.assertEqual(
            split_statements(text),
            ["SELECT 1;", "\nINSERT INTO t VALUES (2);"],
        )


class ParseInsertTests(unittest.TestCase):
    def test_invalid_unicode_numeric_character_returns_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            parse("INSERT INTO t VALUES (²);")

    def test_oversized_integer_returns_parse_error_not_raw_value_error(self) -> None:
        with self.assertRaises(ParseError):
            parse("INSERT INTO t VALUES (" + "1" * 5000 + ");")

    def test_insert_position_values(self) -> None:
        result = parse("INSERT INTO students VALUES (1, 'minsu', 20);")
        self.assertEqual(
            result,
            Insert(table="students", columns=None, values=[1, "minsu", 20]),
        )

    def test_insert_column_values(self) -> None:
        result = parse("INSERT INTO students (id, name, seat) VALUES (2, 'cheolsu', 21);")
        self.assertEqual(
            result,
            Insert(table="students", columns=["id", "name", "seat"], values=[2, "cheolsu", 21]),
        )

    def test_keywords_case_insensitive(self) -> None:
        result = parse("insert into students values (1, 'minsu');")
        self.assertEqual(result.table, "students")
        self.assertEqual(result.values, [1, "minsu"])

    def test_string_with_escaped_quote(self) -> None:
        result = parse("INSERT INTO t VALUES ('O''Brien');")
        self.assertEqual(result.values, ["O'Brien"])

    def test_string_with_comma_space_semicolon(self) -> None:
        result = parse("INSERT INTO t VALUES ('O''Brien, 민;수');")
        self.assertEqual(result.values, ["O'Brien, 민;수"])

    def test_signed_integer(self) -> None:
        result = parse("INSERT INTO t VALUES (-42, +7);")
        self.assertEqual(result.values, [-42, 7])

    def test_missing_semicolon(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES (1)")

    def test_unclosed_string(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES ('open);")

    def test_extra_tokens_after_values(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES (1) EXTRA;")

    def test_missing_into(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT t VALUES (1);")

    def test_missing_values(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t (id) (1);")

    def test_duplicate_columns(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t (id, id) VALUES (1, 2);")

    def test_mismatched_column_value_count(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t (id, name) VALUES (1);")

    def test_cr_in_string_literal(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES ('a\rb');")

    def test_lf_in_string_literal(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES ('a\nb');")

    def test_crlf_in_string_literal(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES ('a\r\nb');")

    def test_multiple_statements_rejected(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("INSERT INTO t VALUES (1); SELECT * FROM t;")


class ParseSelectTests(unittest.TestCase):
    def test_select_star(self) -> None:
        result = parse("SELECT * FROM students;")
        self.assertEqual(result, Select(table="students", columns=None, where=None))

    def test_select_columns(self) -> None:
        result = parse("SELECT name, seat FROM students;")
        self.assertEqual(
            result,
            Select(table="students", columns=["name", "seat"], where=None),
        )

    def test_select_where_integer(self) -> None:
        result = parse("SELECT * FROM students WHERE id = 1;")
        self.assertEqual(
            result,
            Select(table="students", columns=None, where=("id", 1)),
        )

    def test_select_where_string(self) -> None:
        result = parse("SELECT * FROM students WHERE name = 'minsu';")
        self.assertEqual(
            result,
            Select(table="students", columns=None, where=("name", "minsu")),
        )

    def test_select_projected_where(self) -> None:
        result = parse("SELECT name, seat FROM students WHERE id = 3;")
        self.assertEqual(
            result,
            Select(table="students", columns=["name", "seat"], where=("id", 3)),
        )

    def test_keywords_case_insensitive(self) -> None:
        result = parse("select * from students where id = 1;")
        self.assertEqual(result.table, "students")

    def test_missing_semicolon(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM t")

    def test_missing_from(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * t;")

    def test_unsupported_operator(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM t WHERE id > 1;")

    def test_multiple_where_conditions(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM t WHERE id = 1 AND name = 'a';")

    def test_extra_tokens(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM t; SELECT * FROM t;")


class IdentifierTests(unittest.TestCase):
    def test_ascii_identifier_preserved(self) -> None:
        result = parse("SELECT * FROM My_Table_1;")
        self.assertEqual(result.table, "My_Table_1")

    def test_non_ascii_identifier_rejected(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM 학생;")

    def test_identifier_starts_with_digit_rejected(self) -> None:
        with self.assertRaises(ParserParseError):
            parse("SELECT * FROM 1table;")


class IntegrationSplitThenParseTests(unittest.TestCase):
    def test_multi_statement_batch(self) -> None:
        text = (
            "INSERT INTO students (name, id, seat) VALUES ('영희', 3, 22);"
            "SELECT name, seat FROM students WHERE id = 3;"
        )
        statements = split_statements(text)
        self.assertEqual(len(statements), 2)
        self.assertEqual(
            parse(statements[0]),
            Insert(table="students", columns=["name", "id", "seat"], values=["영희", 3, 22]),
        )
        self.assertEqual(
            parse(statements[1]),
            Select(table="students", columns=["name", "seat"], where=("id", 3)),
        )


if __name__ == "__main__":
    unittest.main()
