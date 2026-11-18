"""Compare indexed and sequential lookups on a temporary Phase 2 table."""

import argparse
import csv
import json
import platform
import random
import sys
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mini-dbms"))

from sql import parse
from storage import CsvStorage


def prepare_queries(path, row_count, seed):
    numbers = random.Random(seed)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for key in range(1, row_count + 1):
            output.write(
                f"INSERT INTO users (name, seat) VALUES ('사용자_{key}', "
                f"{numbers.randrange(1, 100_001)});\n"
            )


def open_storage(directory):
    storage = CsvStorage(directory)
    failures = storage.build_indexes()
    if failures:
        raise RuntimeError(f"index build failed: {failures}")
    return storage


def timed_lookup(storage, command, repeats, *, force_linear=False):
    samples = []
    expected = None
    for _ in range(repeats):
        start = perf_counter_ns()
        result = storage.execute(command, force_linear=force_linear)
        samples.append(perf_counter_ns() - start)
        if expected is not None and result != expected:
            raise RuntimeError("repeated queries returned different results")
        expected = result
    return expected, median(samples)


def timed_output(result, path, repeats):
    samples = []
    for _ in range(repeats):
        start = perf_counter_ns()
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(result.columns)
            writer.writerows(result.rows)
        samples.append(perf_counter_ns() - start)
    return median(samples)


def benchmark(directory, rows, repeats, seed):
    tables = directory / "tables"
    tables.mkdir()
    table_path = tables / "users.csv"
    table_path.write_bytes(b"record_id:int,name:string,seat:int\n")
    sql_path = directory / "insert.sql"

    start = perf_counter_ns()
    prepare_queries(sql_path, rows, seed)
    preparation_ns = perf_counter_ns() - start

    insert_storage = open_storage(tables)
    start = perf_counter_ns()
    with sql_path.open(encoding="utf-8") as source, insert_storage.batch_writes():
        for statement in source:
            insert_storage.execute(parse(statement))
    insertion_ns = perf_counter_ns() - start
    del insert_storage

    with table_path.open("rb") as table_file:
        actual_rows = sum(1 for _ in table_file) - 1
    if actual_rows != rows:
        raise RuntimeError(f"expected {rows} data rows, found {actual_rows}")

    start = perf_counter_ns()
    storage = open_storage(tables)
    rebuild_ns = perf_counter_ns() - start
    if storage.tables["users"].max_id != rows:
        raise RuntimeError("rebuilt index has an unexpected maximum ID")

    cases = {}
    for label, key in (
        ("first", 1),
        ("middle", (rows + 1) // 2),
        ("last", rows),
        ("missing", rows + 1),
    ):
        id_query = parse(f"SELECT record_id, name, seat FROM users WHERE record_id = {key};")
        name = f"사용자_{key}"
        name_query = parse(f"SELECT record_id, name, seat FROM users WHERE name = '{name}';")
        indexed, indexed_ns = timed_lookup(storage, id_query, repeats)
        sequential, sequential_ns = timed_lookup(storage, id_query, repeats, force_linear=True)
        by_name, by_name_ns = timed_lookup(storage, name_query, repeats)
        if indexed != sequential or indexed != by_name:
            raise RuntimeError(f"query results differ for case {label}")
        if label == "missing":
            if indexed.rows:
                raise RuntimeError("missing key returned a row")
        elif len(indexed.rows) != 1 or indexed.rows[0][:2] != (key, name):
            raise RuntimeError(f"unexpected result for key {key}")
        cases[label] = {
            "record_id": key,
            "result_rows": len(indexed.rows),
            "indexed_median_ns": indexed_ns,
            "same_id_sequential_median_ns": sequential_ns,
            "name_sequential_median_ns": by_name_ns,
            "csv_output_median_ns": timed_output(indexed, directory / "result.csv", repeats),
        }

    return {
        "rows": rows,
        "csv_bytes": table_path.stat().st_size,
        "seed": seed,
        "repeats": repeats,
        "runtime": f"Python {platform.python_version()} on {platform.platform()}",
        "cache": "OS cache left in place after CSV reads and index rebuild",
        "query_scope": "execute includes file reads and UTF-8 decoding; SQL parsing and CSV output excluded",
        "sql_generation_ns": preparation_ns,
        "sql_parse_and_insert_ns": insertion_ns,
        "index_rebuild_ns": rebuild_ns,
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error("--rows and --repeats must be positive")
    with TemporaryDirectory() as temporary:
        result = benchmark(Path(temporary), args.rows, args.repeats, args.seed)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
