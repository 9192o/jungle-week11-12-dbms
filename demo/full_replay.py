"""전체 요청 CSV를 메모리에 올리지 않고 HTTP 조회로 재생한다."""

import csv
import hashlib
import http.client
import json
import threading
import time
from collections import Counter
from itertools import islice
from uuid import uuid4


def read_requests(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["seq", "timestamp_utc", "id"]:
            raise ValueError("Expected seq,timestamp_utc,id")
        previous = ""
        for seq, row in enumerate(reader, 1):
            timestamp, key = row["timestamp_utc"], int(row["id"])
            if int(row["seq"]) != seq or key < 1 or timestamp < previous or not timestamp.endswith("Z"):
                raise ValueError(f"Invalid replay row {seq}")
            previous = timestamp
            yield seq, timestamp, key


def describe(path):
    dates = Counter(timestamp[:10] for _, timestamp, _ in read_requests(path))
    if not dates:
        raise ValueError("Full replay CSV is empty")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"rows": sum(dates.values()), "dates": dict(dates), "sha256": digest}


class FullReplay:
    def __init__(self, path, output, port):
        self.path, self.output, self.port = path, output, port
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.job = None
        self.scope = None
        self.state = {"status": "idle"}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, limit, clear_cache):
        with self.lock:
            if self.job is not None and self.job.is_alive():
                return False
            self.scope = uuid4().hex
            self.cancelled.clear()
            order = ("B", "C")
            self.state = {"status": "running", "limit": limit, "order": order, "modes": {}, "points": []}
            self.job = threading.Thread(target=self._run, args=(limit, self.scope, clear_cache, order), daemon=True)
            self.job.start()
            return True

    def stop(self):
        self.cancelled.set()

    def _report(self, mode, counts, point=None):
        with self.lock:
            self.state["modes"] = {**self.state["modes"], mode: dict(counts)}
            if point is not None:
                self.state["points"] = [*self.state["points"], point]

    def _run(self, limit, scope, clear_cache, order):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hashes = {}
        status, error = "complete", None
        try:
            with self.output.open("w", encoding="utf-8", newline="\n") as file:
                for mode in order:
                    elapsed_start = time.perf_counter()
                    digest = hashlib.sha256()
                    counts = dict(done=0, hits=0, misses=0, errors=0, db=0, csv=0, rows=0, ms=0.0)
                    for seq, timestamp, key in islice(read_requests(self.path), limit):
                        if self.cancelled.is_set():
                            status = "cancelled"
                            break
                        start = time.perf_counter()
                        connection.request("GET", f"/articles/{key}?mode={mode}&replay_scope={scope}")
                        reply = connection.getresponse()
                        body = reply.read()
                        ms = (time.perf_counter() - start) * 1000
                        if reply.status not in (200, 404):
                            raise RuntimeError(f"HTTP {reply.status} at request {seq}")
                        digest.update(str(reply.status).encode() + b":" + hashlib.sha256(body).digest())
                        cache = reply.getheader("X-Cache")
                        redis_error = reply.getheader("X-Redis-Error")
                        counts["done"] += 1
                        counts["hits"] += cache == "hit"
                        counts["misses"] += cache == "miss"
                        counts["errors"] += bool(redis_error) or reply.status != 200
                        counts["ms"] += ms
                        row = {"mode": mode, "sequence": seq, "timestamp_utc": timestamp, "id": key,
                               "status": reply.status, "cache": cache, "redis_error": redis_error, "ms": ms,
                               "elapsed_ms": (start - elapsed_start) * 1000}
                        for field, header in (("db", "X-Db-Lookups"), ("csv", "X-Csv-Lookups"), ("rows", "X-Csv-Rows")):
                            row[field] = int(reply.getheader(header, "0"))
                            counts[field] += row[field]
                        file.write(json.dumps(row, separators=(",", ":")) + "\n")
                        if seq == 1 or seq == limit or seq % max(1, limit // 100) == 0:
                            file.flush()
                            point = {"mode": mode, "sequence": seq, "round": 1,
                                     **{name: counts[name] for name in ("db","csv","rows")},
                                     "ms": counts["ms"] / seq}
                            self._report(mode, counts, point)
                        elif seq % 200 == 0:
                            self._report(mode, counts)
                    self._report(mode, counts)
                    hashes[mode] = digest.hexdigest()
                    if status == "cancelled":
                        break
            if status == "complete" and len(set(hashes.values())) != 1:
                raise RuntimeError("Replay response bodies differ between modes")
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            connection.close()
            try:
                clear_cache(f"replay:{scope}:")
            except OSError as exc:
                status, error = "failed", str(exc)
            with self.lock:
                self.scope = None
                self.state = {**self.state, "status": status, "error": error}
