"""일반 회귀 테스트와 분리해 실행하는 CSV 조회 성능 비교."""

import argparse
import csv
import json
import platform
import random
import statistics
import tempfile
from pathlib import Path
from time import perf_counter
from typing import TypedDict

from mini_dbms.catalog import Catalog
from mini_dbms.executor import Executor
from mini_dbms.models import Insert, Select, Selected
from mini_dbms.parser import parse


class QueryTiming(TypedDict):
    indexed_seconds: float
    linear_id_seconds: float
    linear_field_seconds: float


class BenchmarkReport(TypedDict):
    rows: int
    repeats: int
    seed: int
    file_bytes: int
    preparation_seconds: float
    insertion_seconds: float
    index_build_seconds: float
    output_seconds: float
    results_equal: bool
    queries: dict[str, QueryTiming]
    environment: str
    cache_conditions: str


class _Options(argparse.Namespace):
    rows: int = 1_000_000
    repeats: int = 3
    directory: Path | None = None


def run_benchmark(
    directory: Path, *, rows: int = 1_000_000, repeats: int = 3
) -> BenchmarkReport:
    """실제 INSERT 경로로 데이터를 만든 뒤 같은 조건의 결과와 중앙값을 비교한다."""
    if rows < 1 or repeats < 1:
        raise ValueError("rows and repeats must be positive")
    started = perf_counter()
    directory.mkdir(parents=True, exist_ok=True)
    table = directory / "benchmark.csv"
    with table.open("x", encoding="utf-8", newline="") as stream:
        _ = stream.write("id:int,name:string,seat:int\n")
    executor = Executor(Catalog(directory, {"benchmark": "benchmark.csv"}))
    targets = {"front": 1, "middle": (rows + 1) // 2, "end": rows, "missing": rows + 1}
    wanted = set(targets.values())
    expected: dict[int, list[int | str]] = {}
    preparation_seconds = perf_counter() - started
    insertion_seconds = 0.0
    generator = random.Random(20261116)
    for identifier in range(1, rows + 1):
        started = perf_counter()
        values: list[int | str] = [
            identifier,
            f"학생_{generator.randrange(1_000_000)}",
            identifier,
        ]
        command = parse(
            f"INSERT INTO benchmark VALUES ({identifier}, '{values[1]}', {identifier});"
        )
        assert isinstance(command, Insert)
        if identifier in wanted:
            expected[identifier] = values
        preparation_seconds += perf_counter() - started
        started = perf_counter()
        _ = executor.execute(command)
        insertion_seconds += perf_counter() - started

    started = perf_counter()
    catalog = Catalog(directory, {"benchmark": "benchmark.csv"})
    index_build_seconds = perf_counter() - started
    executor = Executor(catalog)
    with table.open("rb") as stream:
        actual_rows = sum(1 for _ in stream) - 1
    if actual_rows != rows:
        raise AssertionError(f"expected {rows} rows, found {actual_rows}")

    queries: dict[str, QueryTiming] = {}
    output_samples: list[float] = []
    for label, identifier in targets.items():
        id_query = Select("benchmark", None, ("id", identifier))
        field_query = Select("benchmark", None, ("seat", identifier))
        expected_result = Selected(
            ["id", "name", "seat"],
            [expected[identifier]] if identifier in expected else [],
        )
        indexed_samples: list[float] = []
        linear_samples: list[float] = []
        field_samples: list[float] = []
        for _ in range(repeats):
            started = perf_counter()
            indexed = executor.execute(id_query)
            indexed_samples.append(perf_counter() - started)
            started = perf_counter()
            linear = executor.execute(id_query, use_index=False)
            linear_samples.append(perf_counter() - started)
            started = perf_counter()
            field = executor.execute(field_query)
            field_samples.append(perf_counter() - started)
            if not indexed == linear == field == expected_result:
                raise AssertionError(f"query results differ at {label}")
            with tempfile.TemporaryFile(
                mode="w+", encoding="utf-8", newline=""
            ) as output:
                started = perf_counter()
                writer = csv.writer(output, lineterminator="\n")
                writer.writerows([expected_result.columns])
                writer.writerows(expected_result.rows)
                output.flush()
                output_samples.append(perf_counter() - started)
        queries[label] = {
            "indexed_seconds": statistics.median(indexed_samples),
            "linear_id_seconds": statistics.median(linear_samples),
            "linear_field_seconds": statistics.median(field_samples),
        }
    return {
        "rows": actual_rows,
        "repeats": repeats,
        "seed": 20261116,
        "file_bytes": table.stat().st_size,
        "preparation_seconds": preparation_seconds,
        "insertion_seconds": insertion_seconds,
        "index_build_seconds": index_build_seconds,
        "output_seconds": statistics.median(output_samples),
        "results_equal": True,
        "queries": queries,
        "environment": f"Python {platform.python_version()} / {platform.platform()}",
        "cache_conditions": (
            "OS cache not cleared; repeated queries after index rebuild; "
            "includes file I/O and decoding; output is CSV serialization and flush "
            "to a temporary UTF-8 file, not terminal rendering; preparation includes "
            "SQL generation and parsing; preparation and INSERT excluded from query times"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = argparse.ArgumentParser(description=__doc__)
    _ = arguments.add_argument("--rows", type=int, default=1_000_000)
    _ = arguments.add_argument("--repeats", type=int, default=3)
    _ = arguments.add_argument("--directory", type=Path)
    options = arguments.parse_args(argv, namespace=_Options())
    if options.directory is None:
        with tempfile.TemporaryDirectory(prefix="mini-dbms-benchmark-") as directory:
            report = run_benchmark(
                Path(directory), rows=options.rows, repeats=options.repeats
            )
    else:
        report = run_benchmark(
            options.directory, rows=options.rows, repeats=options.repeats
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
