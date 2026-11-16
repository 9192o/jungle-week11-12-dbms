from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Insert:
    table: str
    columns: list[str] | None = None
    values: list[int | str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Select:
    table: str
    columns: list[str] | None = None
    where: tuple[str, int | str] | None = None


@dataclass(frozen=True, slots=True)
class Inserted:
    id: int


@dataclass(frozen=True, slots=True)
class Selected:
    columns: list[str]
    rows: list[list[int | str]]
