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
import webbrowser
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
from full_replay import FullReplay, describe

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT.parent))

# Redis.py의 CLI 신호 등록은 Windows에서 지원되지 않는다. 같은 서버를 직접 시작한다.
REDIS_START = """
import asyncio
import sys
from contextlib import ExitStack, closing
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


def read_popularity(directory):
    with zipfile.ZipFile(directory / "pageviews.zip") as archive:
        return json.loads(archive.read("pageviews/popular-pages.json"))


def article_ids(path):
    with path.open(encoding="utf-8", newline="") as file:
        records = csv.reader(file)
        if next(records) != ["id:int", "title:string"]:
            raise ValueError("Expected id:int,title:string")
        for row in records:
            yield int(row[0])


def ids_at_positions(path, positions):
    remaining = set(positions)
    selected = {}
    with closing(article_ids(path)) as ids:
        for position, key in enumerate(ids):
            if position in remaining:
                selected[position] = key
                remaining.remove(position)
                if not remaining:
                    break
    return [selected[position] for position in positions]


def load_inputs(directory):
    path = directory / "articles.csv"
    popular = read_popularity(directory)
    with zipfile.ZipFile(directory / "pageviews.zip") as archive:
        for day in popular["dates"].values():
            with archive.open("pageviews/" + day["raw_file"]) as raw:
                if hashlib.file_digest(raw, "sha256").hexdigest() != day["raw_sha256"]:
                    raise ValueError("Pageviews file hash mismatch")
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if digest != popular["articles_csv_sha256"]:
        raise ValueError("articles.csv and Pageviews metadata do not match")

    count = 0
    maximum = 0
    for count, key in enumerate(article_ids(path), 1):
        maximum = max(maximum, key)
    if count < 1_000_000:
        raise ValueError("At least 1,000,000 articles required")
    positions = (0, count // 2, count - 1)
    comparison = tuple((count - 1) * part // 4 for part in range(5))
    keys = ids_at_positions(path, positions + comparison)
    return {
        "rows": count, "sha256": digest, "bytes": path.stat().st_size,
        "dump_date": popular["dump_date"], "positions": keys[:len(positions)],
        "comparison_ids": keys[len(positions):], "missing": maximum + 1,
        "overlap": popular["full_candidate_overlap"],
    }


def load_traffic(path, articles_sha256, *, include_requests=True):
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
    counts = dict.fromkeys(pilot["source_stats"], 0)
    previous = ("", -1)
    for sequence, item in enumerate(pilot["requests"], 1):
        date, hour, key = item["date"], item["hour_utc"], item["id"]
        if (item["seq"] != sequence or date not in pilot["source_stats"]
                or type(hour) is not int or not 0 <= hour < 24
                or type(key) is not int or key < 1 or (date, hour) < previous):
            raise ValueError(f"Invalid traffic request at sequence {sequence}")
        previous = (date, hour)
        counts[date] += 1
        if include_requests:
            requests.append({"group": date, "date": date, "hour_utc": hour, "id": key})
    if not counts or any(count != pilot["requests_per_date"] for count in counts.values()):
        raise ValueError("Traffic request count does not match metadata")
    summary = {
        "request_count": sum(counts.values()), "dates": counts, "seed": pilot["seed"],
        "hourly_rows": sum(day["hour_id_rows"] for day in pilot["source_stats"].values()),
        "hourly_sha256": hourly_sha256,
    }
    if include_requests:
        summary["requests"] = requests
    return summary


def prepare_ranking_files(path, directory):
    files = {}
    if not path.is_file():
        return files
    with ExitStack() as stack:
        archive = stack.enter_context(zipfile.ZipFile(path))
        source = stack.enter_context(archive.open("full-pageview-traffic/hourly-pageviews.csv"))
        rows = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8", newline=""))
        writers = {}
        for row in rows:
            hour = int(row["hour_utc"])
            if hour not in (9, 12, 20):
                continue
            slot = f'{row["date"]}T{hour:02d}'
            if slot not in writers:
                files[slot] = directory / f"{len(files)}.csv"
                handle = stack.enter_context(files[slot].open("w", encoding="utf-8", newline=""))
                writers[slot] = csv.writer(handle, lineterminator="\n")
            writers[slot].writerow((int(row["views"]), -int(row["id"])))
    return files


def read_top_rankings(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return heapq.nlargest(20, ((int(views), int(key)) for views, key in csv.reader(handle)))


def create_demo(directory, cache, port=8765):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from mini_dbms import Select
    from mini_dbms.api import create_app
    import random

    metadata = load_inputs(directory)
    articles_path = directory / "articles.csv"
    traffic_path = directory / "full-pageview-traffic.zip"
    full_path = directory / "full-replay.csv"
    full = None
    if full_path.is_file():
        metadata["full_replay"] = describe(full_path)
        full = FullReplay(full_path, directory / "full-replay-results.ndjson", port)
    ranking_files = {}
    app = create_app(directory, tables={"articles": "articles.csv"})
    # 데모는 읽기 전용이므로 SQL 쓰기가 가능한 공개 경로를 노출하지 않는다.
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != "/query"]
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        with tempfile.TemporaryDirectory(prefix="wiki-rankings-") as ranking_dir:
            async with original(application):
                index = application.state.catalog.get("articles").index
                for day in read_popularity(directory)["dates"].values():
                    if any(index.find(item["id"]) is None for item in day["candidates"]):
                        raise ValueError("Pageviews includes IDs absent from the CSV")
                ranking_files.clear()
                ranking_files.update(prepare_ranking_files(
                    traffic_path, Path(ranking_dir)
                ))
                metadata["ranking_slots"] = sorted(ranking_files)
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
        result = {**metadata, "dates": read_popularity(directory)["dates"]}
        traffic = load_traffic(traffic_path, metadata["sha256"], include_requests=False)
        if traffic is not None:
            result["traffic"] = traffic
        return result

    @app.post("/demo/rankings/reset")
    def reset_rankings():
        cache.clear("rank:")
        return {"ok": True}

    @app.get("/demo/rankings/{slot}")
    def ranking(slot: str, mode: str = "B"):
        if mode not in {"B", "C"}:
            raise HTTPException(400, "Expected mode B or C")
        if slot not in ranking_files:
            raise HTTPException(404, "Ranking hour not available")
        key = f"rank:{slot}"
        if mode == "C":
            try:
                result = cache.command("GET", key)
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "Redis ranking GET failed") from exc
            if result is not None:
                return JSONResponse(result, headers={"X-Aggregations": "0", "X-Cache": "hit"})
        result = []
        for views, identifier in read_top_rankings(ranking_files[slot]):
            key_id = -identifier
            selected = app.state.executor.execute(Select("articles", where=("id", key_id)))
            if not selected.rows:
                raise HTTPException(500, "Ranking article not found in DBMS")
            result.append({"id": key_id, "title": selected.rows[0][1], "views": views})
        if mode == "C":
            try:
                cache.command("SET", key, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "Redis ranking SET failed") from exc
        return JSONResponse(result, headers={"X-Aggregations": "1", "X-Cache": "miss" if mode == "C" else "n/a"})

    @app.get("/demo/workload")
    def workload():
        dates = read_popularity(directory)["dates"]
        groups = [day["candidates"][:10] for day in dates.values()]
        excluded = {item["id"] for day in dates.values() for item in day["candidates"]}
        rng = random.Random(42)
        requests = []
        for label, group in zip(("빈 캐시 시작", "첫 날짜 반복", "둘째 날짜 전환"),
                                (groups[0], groups[0], groups[1]), strict=True):
            background, = ids_at_positions(articles_path, (rng.randrange(metadata["rows"]),))
            while background in excluded:
                background, = ids_at_positions(articles_path, (rng.randrange(metadata["rows"]),))
            excluded.add(background)
            keys = [item["id"] for item in rng.choices(group, k=4)] + [background]
            rng.shuffle(keys)
            requests.extend({"group": label, "id": key} for key in keys)
        return requests

    @app.get("/demo/traffic-workload")
    def traffic_workload():
        traffic = load_traffic(traffic_path, metadata["sha256"])
        if traffic is None:
            raise HTTPException(404, "Full Pageviews traffic is unavailable")
        index = app.state.catalog.get("articles").index
        if any(index.find(item["id"]) is None for item in traffic["requests"]):
            raise HTTPException(500, "Traffic replay includes IDs absent from the CSV")
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
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--redis-dir", type=Path, default=ROOT.parent.parent / "mini-redis-js")
    args = parser.parse_args()
    redis_server = args.redis_dir.resolve() / "Redis.py"
    if not redis_server.is_file():
        parser.error(f"mini-redis-js main checkout not found: {redis_server}")
    import uvicorn

    class DemoServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and args.open_browser:
                host, port = self.servers[0].sockets[0].getsockname()[:2]
                url = f"http://{host}:{port}/"
                print(f"Demo ready: {url}", flush=True)
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

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
            print("Loading demo data and building the DBMS index. The browser opens after startup.",
                  flush=True)
            app = create_demo(DATA, Cache(cache_port), args.port)
            DemoServer(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False)).run()
        finally:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    main()
