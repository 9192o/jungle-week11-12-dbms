"""한국어 위키백과 CSV의 선형 조회, 인덱스, 캐시 비교."""

import argparse
import asyncio
import csv
import hashlib
import heapq
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from full_replay import FullReplay, describe

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT.parent))

# Redis.py의 CLI 신호 등록은 Windows에서 지원되지 않는다. 같은 서버를 직접 시작한다.
REDIS_START = """
import asyncio
import sys
from pathlib import Path
from Redis import Config, RedisServer

async def run():
    server = RedisServer(Config(port=0, dir=Path(sys.argv[1]), appendonly=True, appendfsync="everysec", save_interval=60))
    await server.start()
    print(f"READY {server.port}", flush=True)
    await asyncio.Event().wait()

asyncio.run(run())
"""


def encode(*parts):
    fields = [str(part).encode("utf-8") for part in parts]
    return b"*%d\r\n" % len(fields) + b"".join(
        b"$%d\r\n" % len(field) + field + b"\r\n" for field in fields
    )


class Cache:
    def __init__(self, port):
        self.port = port
        self.connection = None
        self.reader = None
        self.lock = threading.RLock()
        self.keys = set()

    def clear(self, prefix="wiki:"):
        with self.lock:
            keys = sorted(key for key in self.keys if key.startswith(prefix))
            for start in range(0, len(keys), 512):
                batch = keys[start:start + 512]
                self.command("DEL", *batch)
                self.keys.difference_update(batch)

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def command(self, *parts):
        with self.lock:
            try:
                if self.connection is None:
                    self.connection = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                    self.reader = self.connection.makefile("rb")
                if parts[0] == "SET":
                    self.keys.add(parts[1])
                self.connection.sendall(encode(*parts))
                line = self.reader.readline()
                if line == b"+OK\r\n":
                    return True
                if line == b"+PONG\r\n":
                    return "PONG"
                if line == b"$-1\r\n":
                    return None
                if line.startswith(b":"):
                    return int(line[1:])
                if line.startswith(b"$"):
                    size = int(line[1:])
                    body = self.reader.read(size)
                    if len(body) == size and self.reader.read(2) == b"\r\n":
                        return json.loads(body)
                raise OSError("Invalid RESP reply")
            except (OSError, ValueError):
                self.close()
                raise


def load_inputs(directory):
    path = directory / "articles.csv"
    with zipfile.ZipFile(directory / "pageviews.zip") as archive:
        popular = json.loads(archive.read("pageviews/popular-pages.json"))
        for day in popular["dates"].values():
            raw = archive.read("pageviews/" + day["raw_file"])
            if hashlib.sha256(raw).hexdigest() != day["raw_sha256"]:
                raise ValueError("Pageviews file hash mismatch")
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if digest != popular["articles_csv_sha256"]:
        raise ValueError("articles.csv and Pageviews metadata do not match")
    # 실험 위치는 ID 숫자가 아닌 원본 CSV의 행 순서에 따라 고른다.
    with path.open(encoding="utf-8", newline="") as file:
        records = csv.reader(file)
        if next(records) != ["id:int", "title:string"]:
            raise ValueError("Expected id:int,title:string")
        ids = [int(row[0]) for row in records]
    if len(ids) < 1_000_000:
        raise ValueError("At least 1,000,000 articles required")
    return {
        "rows": len(ids), "sha256": digest, "bytes": path.stat().st_size,
        "dump_date": popular["dump_date"], "positions": [ids[i] for i in (0, len(ids)//2, len(ids)-1)],
        "comparison_ids": [ids[(len(ids) - 1) * part // 4] for part in range(5)],
        "missing": max(ids) + 1, "dates": popular["dates"],
        "overlap": popular["full_candidate_overlap"]
    }, ids


def load_traffic(path, articles_sha256):
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as archive:
        pilot = json.loads(archive.read("full-pageview-traffic/pilot-replay.json"))
        with archive.open("full-pageview-traffic/hourly-pageviews.csv") as hourly:
            hourly_sha256 = hashlib.file_digest(hourly, "sha256").hexdigest()
    if pilot["articles_csv_sha256"] != articles_sha256:
        raise ValueError("Traffic replay belongs to a different articles CSV")
    if pilot["popularity_source_sha256"] != hourly_sha256:
        raise ValueError("Hourly Pageviews CSV hash mismatch")
    requests = []
    previous = ("", -1)
    for sequence, item in enumerate(pilot["requests"], 1):
        date, hour, key = item["date"], item["hour_utc"], item["id"]
        if (item["seq"] != sequence or date not in pilot["source_stats"]
                or type(hour) is not int or not 0 <= hour < 24
                or type(key) is not int or key < 1 or (date, hour) < previous):
            raise ValueError(f"Invalid traffic request at sequence {sequence}")
        previous = (date, hour)
        requests.append({"group": date, "date": date, "hour_utc": hour, "id": key})
    counts = {date: sum(item["date"] == date for item in requests) for date in pilot["source_stats"]}
    if not counts or any(count != pilot["requests_per_date"] for count in counts.values()):
        raise ValueError("Traffic request count does not match metadata")
    return {
        "request_count": len(requests), "dates": counts, "seed": pilot["seed"],
        "hourly_rows": sum(day["hour_id_rows"] for day in pilot["source_stats"].values()),
        "hourly_sha256": hourly_sha256, "requests": requests,
    }


def load_ranking_hours(path):
    hours = {}
    if not path.is_file():
        return hours
    with zipfile.ZipFile(path) as archive, archive.open("full-pageview-traffic/hourly-pageviews.csv") as source:
        rows = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8", newline=""))
        for row in rows:
            hour = int(row["hour_utc"])
            if hour in (9, 12, 20):
                slot = f'{row["date"]}T{hour:02d}'
                hours.setdefault(slot, []).append((int(row["views"]), -int(row["id"])))
    return hours


def create_demo(directory, cache, port=8765):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from mini_dbms import Select
    from mini_dbms.api import create_app
    import random

    metadata, ids = load_inputs(directory)
    full_path = directory / "full-replay.csv"
    full = None
    if full_path.is_file():
        metadata["full_replay"] = describe(full_path)
        full = FullReplay(full_path, directory / "full-replay-results.ndjson", port)
    traffic = load_traffic(directory / "full-pageview-traffic.zip", metadata["sha256"])
    ranking_hours = load_ranking_hours(directory / "full-pageview-traffic.zip")
    ranking_titles = {}
    metadata["ranking_slots"] = sorted(ranking_hours)
    if traffic is not None:
        metadata["traffic"] = {key: value for key, value in traffic.items() if key != "requests"}
    app = create_app(directory, tables={"articles": "articles.csv"})
    # 데모는 읽기 전용이므로 SQL 쓰기가 가능한 공개 경로를 노출하지 않는다.
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != "/query"]
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            index = application.state.catalog.get("articles").index
            for day in metadata["dates"].values():
                if any(index.find(item["id"]) is None for item in day["candidates"]):
                    raise ValueError("Pageviews includes IDs absent from the CSV")
            if traffic is not None and any(index.find(item["id"]) is None for item in traffic["requests"]):
                raise ValueError("Traffic replay includes IDs absent from the CSV")
            ranking_ids = {-key for rows in ranking_hours.values() for _, key in heapq.nlargest(20, rows)}
            for key in ranking_ids:
                selected = application.state.executor.execute(Select("articles", where=("id", key)))
                ranking_titles[key] = selected.rows[0][1]
            metadata["index_build_ms"] = application.state.catalog.index_build_seconds * 1000
            try:
                cache.command("PING")
                yield
            finally:
                if full is not None:
                    full.stop()
                    if full.job is not None:
                        await asyncio.to_thread(full.job.join, 15)
                cache.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(CORSMiddleware, allow_origins=["null"], allow_methods=["GET", "POST"], allow_headers=["content-type"], expose_headers=["X-Db-Lookups","X-Csv-Lookups","X-Csv-Rows","X-Cache","X-Redis-Error","X-Aggregations"])

    @app.get("/")
    def home():
        return FileResponse(ROOT / "index.html")

    @app.get("/demo/status")
    def status():
        return metadata

    @app.post("/demo/rankings/reset")
    def reset_rankings():
        cache.clear("rank:")
        return {"ok": True}

    @app.get("/demo/rankings/{slot}")
    def ranking(slot: str, mode: str = "B"):
        if mode not in {"B", "C"}:
            raise HTTPException(400, "Expected mode B or C")
        if slot not in ranking_hours:
            raise HTTPException(404, "Ranking hour not available")
        key = f"rank:{slot}"
        if mode == "C":
            try:
                result = cache.command("GET", key)
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "Redis ranking GET failed") from exc
            if result is not None:
                return JSONResponse(result, headers={"X-Aggregations": "0", "X-Cache": "hit"})
        # 시간대별 입력은 두 경로에서 공유한다. 직접 조회는 요청마다 순위를 계산한다.
        top = heapq.nlargest(20, ranking_hours[slot])
        result = [{"id": -identifier, "title": ranking_titles[-identifier], "views": views}
                  for views, identifier in top]
        if mode == "C":
            try:
                cache.command("SET", key, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "Redis ranking SET failed") from exc
        return JSONResponse(result, headers={"X-Aggregations": "1", "X-Cache": "miss" if mode == "C" else "n/a"})

    @app.get("/demo/workload")
    def workload():
        groups = [day["candidates"][:10] for day in metadata["dates"].values()]
        excluded = {item["id"] for day in metadata["dates"].values() for item in day["candidates"]}
        rng = random.Random(42)
        requests = []
        for label, group in zip(("빈 캐시 시작", "첫 날짜 반복", "둘째 날짜 전환"),
                                (groups[0], groups[0], groups[1]), strict=True):
            background = rng.choice(ids)
            while background in excluded:
                background = rng.choice(ids)
            excluded.add(background)
            keys = [item["id"] for item in rng.choices(group, k=4)] + [background]
            rng.shuffle(keys)
            requests.extend({"group": label, "id": key} for key in keys)
        return requests

    @app.get("/demo/traffic-workload")
    def traffic_workload():
        if traffic is None:
            raise HTTPException(404, "Full Pageviews traffic is unavailable")
        return traffic["requests"]

    @app.post("/demo/full-replay")
    def start_full_replay(sample: bool = False):
        if full is None:
            raise HTTPException(404, "Full replay CSV is unavailable")
        rows = metadata["full_replay"]["rows"]
        limit = min(240, rows) if sample else rows
        if not full.start(limit, cache.clear):
            raise HTTPException(409, "A full replay is already running")
        return full.snapshot()

    @app.get("/demo/full-replay")
    def full_replay_status():
        return full.snapshot() if full is not None else {"status": "unavailable"}

    @app.post("/demo/full-replay/stop")
    def stop_full_replay():
        if full is not None:
            full.stop()
        return {"ok": True}

    @app.get("/demo/full-replay/results")
    def full_replay_results():
        if full is None or not full.output.is_file():
            raise HTTPException(404, "No full replay results")
        return FileResponse(full.output, filename="full-replay-results.ndjson")

    @app.post("/demo/reset")
    def reset():
        cache.clear()
        return {"ok": True}

    @app.get("/articles/{key}")
    def article(key: int, mode: str = "B", replay_scope: str | None = None):
        if mode not in {"A", "B", "C"}:
            raise HTTPException(400, "Expected mode A, B or C")
        if replay_scope and (full is None or full.scope != replay_scope):
            raise HTTPException(400, "Invalid replay scope")
        cache_key = f"replay:{replay_scope}:{key}" if replay_scope else f"wiki:{key}"
        read_rows = 0
        redis_error = ""
        outcome = "n/a"
        result = None
        db_calls = 1
        if mode == "C":
            try:
                result = cache.command("GET", cache_key)
                outcome = "miss" if result is None else "hit"
            except (OSError, ValueError):
                outcome = "error"
                redis_error = "GET"
        if result is not None:
            db_calls = 0
        else:
            def count():
                nonlocal read_rows
                read_rows += 1
            selected = app.state.executor.execute(Select("articles", where=("id", key)),
                                                  use_index=mode != "A", on_csv_row=count)
            if selected.rows:
                identifier, title = selected.rows[0]
                result = {"id": identifier, "title": title}
                if mode == "C" and not redis_error:
                    try:
                        cache.command("SET", cache_key, json.dumps(result, ensure_ascii=False))
                    except (OSError, ValueError):
                        redis_error = "SET"
        return JSONResponse(result if result is not None else {"detail": "Article not found"},
                            status_code=200 if result is not None else 404,
                            headers={"X-Db-Lookups": str(db_calls),
                                     "X-Csv-Lookups": str(int(mode == "A" or read_rows > 0)),
                                     "X-Csv-Rows": str(read_rows), "X-Cache": outcome,
                                     "X-Redis-Error": redis_error})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--redis-dir", type=Path, default=ROOT.parent.parent / "mini-redis-js")
    args = parser.parse_args()
    redis_server = args.redis_dir.resolve() / "Redis.py"
    if not redis_server.is_file():
        parser.error(f"mini-redis-js main checkout not found: {redis_server}")
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="wiki-mini-redis-") as redis_data:
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", REDIS_START, redis_data],
            cwd=redis_server.parent, stdout=subprocess.PIPE,
        )
        try:
            ready = process.stdout.readline().split()
            if len(ready) != 2 or ready[0] != b"READY":
                raise RuntimeError("mini-redis-js cache process failed to start")
            cache_port = int(ready[1])
            print(f"mini-redis-js: {redis_server} on {cache_port}", flush=True)
            uvicorn.run(create_demo(DATA, Cache(cache_port), args.port), host="127.0.0.1",
                        port=args.port, access_log=False)
        finally:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    main()
