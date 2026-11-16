from typing import ClassVar


class DBError(Exception):
    code: ClassVar[str] = "execution_error"


class ParseError(DBError):
    code: ClassVar[str] = "syntax_error"


class ConstraintError(DBError):
    code: ClassVar[str] = "constraint_error"


class StorageError(DBError):
    code: ClassVar[str] = "storage_error"


class TableUnavailableError(DBError):
    code: ClassVar[str] = "table_unavailable"
