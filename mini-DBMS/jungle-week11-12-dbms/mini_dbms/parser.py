from mini_dbms.errors import ParseError
from mini_dbms.models import Insert, Select


def split_statements(text: str) -> list[str]:
    statements: list[str] = []
    start = 0
    in_string = False
    i = 0

    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        if ch == "'":
            in_string = True
            i += 1
            continue

        if ch == ";":
            statement = text[start : i + 1]
            if statement.strip() != ";":
                statements.append(statement)
            start = i + 1
        i += 1

    if start < len(text):
        tail = text[start:]
        if tail.strip():
            statements.append(tail)

    return statements


def _tokenize(statement: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    length = len(statement)

    while i < length:
        ch = statement[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "'":
            value = ""
            i += 1
            while i < length:
                ch = statement[i]
                if ch in ("\r", "\n"):
                    raise ParseError("newline in string literal")
                if ch == "'":
                    if i + 1 < length and statement[i + 1] == "'":
                        value += "'"
                        i += 2
                        continue
                    i += 1
                    break
                value += ch
                i += 1
            else:
                raise ParseError("unterminated string literal")
            tokens.append(("STRING", value))
            continue

        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch == "_":
            j = i
            while j < length and (
                ("a" <= statement[j] <= "z")
                or ("A" <= statement[j] <= "Z")
                or ("0" <= statement[j] <= "9")
                or statement[j] == "_"
            ):
                j += 1
            tokens.append(("IDENT", statement[i:j]))
            i = j
            continue

        if "0" <= ch <= "9" or (
            ch in "+-" and i + 1 < length and "0" <= statement[i + 1] <= "9"
        ):
            j = i
            if statement[j] in "+-":
                j += 1
            while j < length and "0" <= statement[j] <= "9":
                j += 1
            tokens.append(("INT", statement[i:j]))
            i = j
            continue

        if ch in "(),;=*":
            tokens.append((ch, ch))
            i += 1
            continue

        raise ParseError(f"unexpected character: {ch!r}")

    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens: list[tuple[str, str]] = tokens
        self.pos: int = 0

    def _peek(self) -> tuple[str, str] | None:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _consume(self, expected_type: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ParseError("unexpected end of statement")
        if expected_type is not None and token[0] != expected_type:
            raise ParseError(f"expected {expected_type}, got {token[0]}")
        self.pos += 1
        return token

    def _match_keyword(self, keyword: str) -> bool:
        token = self._peek()
        if token is not None and token[0] == "IDENT" and token[1].upper() == keyword:
            self.pos += 1
            return True
        return False

    def _expect_keyword(self, keyword: str) -> None:
        if not self._match_keyword(keyword):
            raise ParseError(f"expected {keyword}")

    def _parse_identifier_list(self) -> list[str]:
        identifiers: list[str] = []
        while True:
            token = self._consume("IDENT")
            identifiers.append(token[1])
            token = self._peek()
            if token is None or token[0] != ",":
                break
            _ = self._consume(",")
        return identifiers

    def _parse_value(self) -> int | str:
        kind, value = self._consume()
        match kind:
            case "STRING":
                return value
            case "INT":
                try:
                    return int(value)
                except ValueError as error:
                    raise ParseError("integer literal exceeds supported size") from error
            case _:
                raise ParseError("expected literal value")

    def _parse_value_list(self) -> list[int | str]:
        values: list[int | str] = []
        while True:
            values.append(self._parse_value())
            token = self._peek()
            if token is None or token[0] != ",":
                break
            _ = self._consume(",")
        return values

    def parse_insert(self) -> Insert:
        self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        table_token = self._consume("IDENT")
        table = table_token[1]

        columns = None
        token = self._peek()
        if token is not None and token[0] == "(":
            _ = self._consume("(")
            columns = self._parse_identifier_list()
            _ = self._consume(")")

        self._expect_keyword("VALUES")
        _ = self._consume("(")
        values = self._parse_value_list()
        _ = self._consume(")")

        if columns is not None:
            if len(columns) != len(values):
                raise ParseError("column count and value count mismatch")
            if len(set(columns)) != len(columns):
                raise ParseError("duplicate column name")

        return Insert(table=table, columns=columns, values=values)

    def parse_select(self) -> Select:
        self._expect_keyword("SELECT")

        columns = None
        token = self._peek()
        if token is not None and token[0] == "*":
            _ = self._consume("*")
        else:
            columns = self._parse_identifier_list()

        self._expect_keyword("FROM")
        table_token = self._consume("IDENT")
        table = table_token[1]

        where = None
        if self._match_keyword("WHERE"):
            column_token = self._consume("IDENT")
            _ = self._consume("=")
            where = (column_token[1], self._parse_value())

        return Select(table=table, columns=columns, where=where)

    def parse_statement(self) -> Insert | Select:
        token = self._peek()
        if token is None:
            raise ParseError("empty statement")
        if token[0] != "IDENT":
            raise ParseError("expected INSERT or SELECT")

        result: Insert | Select
        match token[1].upper():
            case "INSERT":
                result = self.parse_insert()
            case "SELECT":
                result = self.parse_select()
            case _:
                raise ParseError(f"unsupported statement: {token[1]}")
        _ = self._consume(";")
        if self._peek() is not None:
            raise ParseError("unexpected tokens after statement")
        return result


def parse(statement: str) -> Insert | Select:
    if not statement.strip().endswith(";"):
        raise ParseError("statement must end with semicolon")
    return _Parser(_tokenize(statement)).parse_statement()
