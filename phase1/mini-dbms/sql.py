import re
from dataclasses import dataclass


class SqlError(ValueError):
    pass


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple[str, ...] | None
    values: tuple[int | str, ...]


@dataclass(frozen=True)
class Select:
    table: str
    columns: tuple[str, ...] | None
    condition: tuple[str, int | str] | None


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<int>[+-]?[0-9]+)|(?P<string>'[^'\r\n]*(?:''[^'\r\n]*)*')"
    r"|(?P<symbol>[(),;*=])"
)


def statements(source):
    start = index = 0
    quoted = False
    while index < len(source):
        char = source[index]
        index += 1
        if char == "'":
            if quoted and source[index:index + 1] == "'":
                index += 1
            else:
                quoted = not quoted
        elif char == ";" and not quoted:
            if source[start:index - 1].strip():
                yield source[start:index]
            start = index

    if source[start:].strip():
        yield source[start:]


def tokens(statement):
    offset = 0
    while offset < len(statement):
        match = TOKEN.match(statement, offset)
        if match is None:
            raise SqlError(f"unexpected character {statement[offset]!r}")
        offset = match.end()
        if match.lastgroup != "space":
            yield match.lastgroup, match.group()


class Parser:
    def __init__(self, statement):
        self.tokens = iter(tokens(statement))
        self.current = next(self.tokens, None)

    def accept(self, spelling):
        if self.current is None:
            return False
        kind, value = self.current
        if kind not in ("name", "symbol") or value.upper() != spelling:
            return False
        self.current = next(self.tokens, None)
        return True

    def expect(self, spelling):
        if not self.accept(spelling):
            raise SqlError(f"expected {spelling}")

    def name(self):
        if self.current is None or self.current[0] != "name":
            raise SqlError("expected a table or column name")
        _, value = self.current
        self.current = next(self.tokens, None)
        return value

    def literal(self):
        if self.current is None:
            raise SqlError("expected an integer or a string")
        kind, value = self.current
        if kind == "string":
            result = value[1:-1].replace("''", "'")
        elif kind == "int":
            try:
                result = int(value)
            except ValueError as exc:
                raise SqlError("integer literal is too large") from exc
        else:
            raise SqlError("expected an integer or a string")
        self.current = next(self.tokens, None)
        return result

    def list_of(self, item):
        values = [item()]
        while self.accept(","):
            values.append(item())
        return tuple(values)

    def insert(self):
        self.expect("INTO")
        table = self.name()
        columns = None
        if self.accept("("):
            columns = self.list_of(self.name)
            self.expect(")")
        self.expect("VALUES")
        self.expect("(")
        values = self.list_of(self.literal)
        self.expect(")")
        return Insert(table, columns, values)

    def select(self):
        columns = None if self.accept("*") else self.list_of(self.name)
        self.expect("FROM")
        table = self.name()
        condition = None
        if self.accept("WHERE"):
            column = self.name()
            self.expect("=")
            condition = (column, self.literal())
        return Select(table, columns, condition)


def parse(statement):
    parser = Parser(statement)
    if parser.accept("INSERT"):
        command = parser.insert()
    elif parser.accept("SELECT"):
        command = parser.select()
    else:
        raise SqlError("expected INSERT or SELECT")
    parser.expect(";")
    if parser.current is not None:
        raise SqlError("unexpected tokens after the statement")
    return command
