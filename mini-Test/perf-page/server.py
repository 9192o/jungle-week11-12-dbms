"""조회 성능 비교 서버. full-replay.csv의 요청 순서대로 두 가지 비교를 수행하고 요청당 지연을 잰다.

비교 1 (인덱스): 문서 id로 제목 한 건을 찾는다.
* file        : articles.csv를 처음부터 한 줄씩 읽어 id가 같은 줄을 찾는다(인덱스 없음).
* dbms        : mini-DBMS API 서버에 `SELECT title FROM articles WHERE id = N;`을 보낸다(B+ Tree 인덱스).

비교 2 (캐시): 문서 하나의 제목과 조회 통계를 조인 쿼리로 가져온다.
* mysql       : 요청마다 MySQL에 조인 쿼리를 실행한다.
* mysql_redis : mini-Redis에서 먼저 찾고, 없을 때만 MySQL에 조인 쿼리를 실행한 뒤 결과를 넣는다(cache-aside).
                적중할 때마다 수명을 새로 준다(슬라이딩 만료).

mini-DBMS, mini-Redis, MySQL은 각자의 서버 프로세스로 띄우고 각자의 프로토콜로 호출한다.
mini-DBMS와 mini-Redis의 코드는 수정하지 않는다.

실행: .venv/bin/python server.py  →  http://127.0.0.1:8765
"""

from __future__ import annotations

import array
import asyncio
import collections
import csv
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

import mysql_db

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent
DBMS_DIR = Path(os.environ.get("MINI_DBMS_DIR", DATA_DIR.parent / "mini-DBMS" / "jungle-week11-12-dbms"))
REDIS_DIR = Path(os.environ.get("MINI_REDIS_DIR", DATA_DIR.parent / "mini-Redis"))
ARTICLES = DATA_DIR / "articles.csv"
REPLAY = DATA_DIR / "full-replay.csv"

HOST = "127.0.0.1"
PORT = int(os.environ.get("PERF_PORT", "8765"))
DBMS_PORT = int(os.environ.get("PERF_DBMS_PORT", "18001"))
REDIS_PORT = int(os.environ.get("PERF_REDIS_PORT", "16379"))

# 캐시 항목 수명(초)의 기본값. 페이지에서 측정마다 바꿀 수 있고, 0이면 만료 없이 넣는다.
# 리플레이 시각이 아니라 실제 경과 시간 기준이다.
CACHE_TTL_SECONDS = int(os.environ.get("PERF_CACHE_TTL", "60"))
MAX_TTL_SECONDS = 86_400

STARTUP_TIMEOUT = 180.0  # 초. 168만 행의 인덱스 구축과 MySQL 기동을 기다린다.
SERIES_BUCKETS = 60  # 진행 구간별 평균 지연을 그릴 때의 구간 수
MAX_SAMPLES = 2000
DEL_BATCH = 500
# 인기 문서의 기준. 전체 조회 기록에서 요청 수가 많은 문서부터 더해 요청의 이 비중에 닿는 지점의
# 요청 수를 경계로 삼고, 그 이상 요청된 문서를 인기 문서로 본다.
POPULAR_REQUEST_SHARE = 0.8

MODES = ("file", "dbms", "mysql", "mysql_redis")

sys.path.insert(0, str(REDIS_DIR))
from resp import RespError, encode_command  # noqa: E402  mini-Redis의 RESP 인코더를 그대로 쓴다.


class DbmsClient:
    """mini-DBMS의 POST /query를 keep-alive 연결 하나로 호출하는 최소 HTTP 클라이언트."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls) -> DbmsClient:
        return cls(*await asyncio.open_connection(HOST, DBMS_PORT))

    async def title(self, article_id: int) -> str | None:
        """id로 제목을 조회한다. 없는 id면 None.

        Raises:
            RuntimeError: mini-DBMS가 오류로 답한 경우. 실패를 "없음"으로 숨기지 않는다.
        """
        body = json.dumps({"sql": f"SELECT title FROM articles WHERE id = {article_id};"}).encode()
        self.writer.write(
            b"POST /query HTTP/1.1\r\nHost: mini-dbms\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        )
        await self.writer.drain()
        head = await self.reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                length = int(value)
        payload = json.loads(await self.reader.readexactly(length))
        if not payload.get("success"):
            raise RuntimeError(f"mini-DBMS 오류: {payload.get('error')}")
        rows = payload["rows"]
        return rows[0][0] if rows else None

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass  # 이미 끊긴 연결을 닫는 중이라 더 할 일이 없다.


class SyncRedis:
    """mini-Redis에 블로킹 소켓으로 명령 하나를 보내고 응답 하나를 받는 최소 클라이언트.

    MySQL 드라이버(PyMySQL)가 블로킹 방식이라, 같은 스레드에서 같은 방식으로 재야 두 저장소의
    지연을 같은 조건으로 비교할 수 있다. mini-Redis의 RespClient는 asyncio 전용이라 쓰지 못한다.
    """

    def __init__(self) -> None:
        self._socket = socket.create_connection((HOST, REDIS_PORT))
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._reader = self._socket.makefile("rb")

    def call(self, *args: bytes | str | int) -> bytes | str | int | None:
        self._socket.sendall(encode_command(*args))
        line = self._reader.readline()
        if not line.endswith(b"\r\n"):
            raise ConnectionError("mini-Redis가 연결을 닫았습니다.")
        kind, rest = line[:1], line[1:-2]
        if kind == b"$":
            length = int(rest)
            return None if length < 0 else self._reader.read(length + 2)[:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b":":
            return int(rest)
        if kind == b"-":
            raise RespError(rest.decode())
        raise ConnectionError(f"알 수 없는 응답 형식: {kind!r}")

    def close(self) -> None:
        self._reader.close()
        self._socket.close()


class State:
    def __init__(self) -> None:
        self.ready = False
        self.stage = "시작하는 중"
        self.error: str | None = None
        self.replay = array.array("i")
        self.article_count = 0
        self.distinct_ids = 0
        self.popular = bytearray()  # 문서 id → 인기 문서면 1
        self.popular_min_count = 0
        self.popular_docs = 0
        self.popular_request_share = 0.0
        self.dbms_startup_seconds = 0.0
        self.mysql_version = ""
        # 이 서버가 Redis에 넣은 적이 있는 문서. 캐시를 비울 때와, 미스가 "처음 요청"인지 "만료"인지 가릴 때 쓴다.
        self.cached_ids: set[int] = set()
        self.lock = asyncio.Lock()  # 측정은 한 번에 하나만. 겹치면 서로의 지연을 오염시킨다.
        self.children: list[subprocess.Popen] = []
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="mini-perf-"))


state = State()


class Measurement:
    """한 번의 측정에서 모으는 값. 방식마다 채우는 방법만 다르고 집계는 같다."""

    def __init__(self) -> None:
        self.latencies = array.array("d")
        self.hits = bytearray()  # 캐시를 쓰는 방식에서만 채운다. 요청마다 적중이면 1.
        self.groups = bytearray()  # 잰 요청마다 인기 문서면 1
        self.misses = {"new": 0, "expired": 0}
        self.answers: list[str] = []  # 표본 위치에서 얻은 답. 방식끼리 대조한다.
        self.wall_started = time.perf_counter()


def _scan_file(article_id: int) -> tuple[str | None, float]:
    """articles.csv를 처음부터 읽어 id가 같은 줄의 제목과 걸린 시간을 돌려준다.

    인덱스가 없을 때 할 수 있는 가장 단순한 조회다. 매번 파일을 새로 열고, 찾으면 바로 멈춘다.
    시간을 스레드 안에서 재서 스레드 전환 비용이 지연에 섞이지 않게 한다.
    """
    prefix = b"%d," % article_id
    started = time.perf_counter()
    title = None
    with ARTICLES.open("rb") as stream:
        stream.readline()  # 헤더
        for line in stream:
            if line.startswith(prefix):
                title = next(csv.reader([line.decode("utf-8").rstrip("\r\n")]))[1]
                break
    return title, time.perf_counter() - started


def _load_dataset() -> None:
    ids = array.array("i")
    with REPLAY.open("rb") as stream:
        stream.readline()
        for line in stream:
            ids.append(int(line[line.rindex(b",") + 1 :]))
    state.replay = ids
    counts = collections.Counter(ids)
    state.distinct_ids = len(counts)
    covered = 0
    for requests in sorted(counts.values(), reverse=True):
        covered += requests
        if covered >= len(ids) * POPULAR_REQUEST_SHARE:
            state.popular_min_count = requests
            break
    popular = bytearray(max(counts) + 1)
    for article_id, requests in counts.items():
        if requests >= state.popular_min_count:
            popular[article_id] = 1
    state.popular = popular
    state.popular_docs = sum(popular)
    state.popular_request_share = sum(r for r in counts.values() if r >= state.popular_min_count) / len(ids)
    with ARTICLES.open("rb") as stream:
        state.article_count = sum(chunk.count(b"\n") for chunk in iter(lambda: stream.read(1 << 20), b"")) - 1


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((HOST, port)) == 0


async def _wait_for(port: int, process: subprocess.Popen, name: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not _port_open(port):
        if process.poll() is not None:
            raise RuntimeError(f"{name} 프로세스가 시작 중 종료됐습니다. 로그: {state.runtime_dir / (name + '.log')}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name}가 {STARTUP_TIMEOUT:.0f}초 안에 준비되지 않았습니다.")
        await asyncio.sleep(0.2)


def _spawn(name: str, command: list[str], cwd: Path) -> subprocess.Popen:
    log = (state.runtime_dir / f"{name}.log").open("wb")
    process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    state.children.append(process)
    return process


def _set_stage(text: str) -> None:
    state.stage = text


def _prepare_mysql_data() -> None:
    mysql_db.ensure_loaded(_set_stage)
    connection = mysql_db.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            state.mysql_version = cursor.fetchone()[0]
    finally:
        connection.close()


async def _prepare() -> None:
    """세 서버를 띄우고 리플레이를 메모리에 올린다. 실패하면 이유를 /api/info로 보여 준다."""
    try:
        for port, name in ((DBMS_PORT, "mini-DBMS"), (REDIS_PORT, "mini-Redis"), (mysql_db.PORT, "MySQL")):
            if _port_open(port):
                raise RuntimeError(f"{name}용 포트 {port}가 이미 사용 중입니다. 이전 실행의 프로세스가 남아 있는지 확인하세요.")
        mysqld = mysql_db.find_mysqld()
        if mysqld is None:
            raise RuntimeError("mysqld를 찾을 수 없습니다. `brew install mysql`로 설치하거나 PERF_MYSQLD로 경로를 지정하세요.")

        started = time.perf_counter()
        python = sys.executable
        dbms = _spawn("mini-DBMS", [python, str(HERE / "dbms_server.py"), str(DBMS_DIR), str(DATA_DIR), str(DBMS_PORT)], HERE)
        # RDB·AOF가 mini-Redis 저장소 안에 생기지 않도록 임시 디렉터리를 준다. 자동 저장은 끈다.
        redis = _spawn(
            "mini-Redis",
            [python, "Redis.py", "--port", str(REDIS_PORT), "--dir", str(state.runtime_dir / "redis"), "--save-interval", "0"],
            REDIS_DIR,
        )
        loading = asyncio.create_task(asyncio.to_thread(_load_dataset))

        _set_stage("MySQL을 띄우는 중")
        await asyncio.to_thread(mysql_db.initialize, mysqld)
        mysql = _spawn("MySQL", mysql_db.server_command(mysqld), HERE)
        await _wait_for(mysql_db.PORT, mysql, "MySQL")
        await asyncio.to_thread(_prepare_mysql_data)

        _set_stage("mini-DBMS가 인덱스를 만드는 중")
        await _wait_for(REDIS_PORT, redis, "mini-Redis")
        await _wait_for(DBMS_PORT, dbms, "mini-DBMS")
        state.dbms_startup_seconds = time.perf_counter() - started
        _set_stage("조회 기록을 읽는 중")
        await loading
        state.ready = True
        _set_stage("준비 완료")
    except Exception as exc:  # 어떤 이유로든 준비에 실패하면 페이지에서 원인을 볼 수 있어야 한다.
        state.error = f"{type(exc).__name__}: {exc}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    preparing = asyncio.create_task(_prepare())
    try:
        yield
    finally:
        preparing.cancel()
        for child in state.children:
            child.terminate()
        for child in state.children:
            try:
                child.wait(timeout=30)  # mysqld는 버퍼를 디스크에 내리고 끝나므로 여유를 둔다.
            except subprocess.TimeoutExpired:
                child.kill()
        shutil.rmtree(state.runtime_dir, ignore_errors=True)


app = FastAPI(title="mini 성능 비교", lifespan=lifespan)


def _sample_indices(count: int, samples: int) -> list[int]:
    """요청 0..count-1에서 고르게 뽑은 위치. 같은 비교의 방식끼리 같은 위치의 답을 서로 대조한다."""
    samples = min(samples, count)
    return [(k * count) // samples for k in range(samples)]


async def _measure_file(ids: array.array, sample_idx: list[int], progress: dict) -> Measurement:
    # 한 건에 수십 ms라 전체를 돌리면 몇 시간이 걸린다. 고르게 뽑은 표본만 실제로 잰다.
    result = Measurement()
    progress["total"] = len(sample_idx)
    for done, index in enumerate(sample_idx, 1):
        title, seconds = await asyncio.to_thread(_scan_file, ids[index])
        result.latencies.append(seconds)
        result.groups.append(state.popular[ids[index]])
        result.answers.append(title or "")
        progress["done"] = done
    return result


async def _measure_dbms(ids: array.array, count: int, sample_idx: list[int], progress: dict) -> Measurement:
    result = Measurement()
    progress["total"] = count
    sample_set = set(sample_idx)
    dbms = await DbmsClient.connect()
    try:
        for index in range(count):
            article_id = ids[index]
            started = time.perf_counter()
            title = await dbms.title(article_id)
            result.latencies.append(time.perf_counter() - started)
            result.groups.append(state.popular[article_id])
            if index in sample_set:
                result.answers.append(title or "")
            progress["done"] = index + 1
    finally:
        await dbms.close()
    return result


def _measure_join(
    ids: array.array,
    count: int,
    sample_idx: list[int],
    use_cache: bool,
    cold: bool,
    ttl: int,
    progress: dict,
    cancelled: threading.Event,
) -> Measurement:
    """MySQL 조인 쿼리를, 캐시 없이 또는 mini-Redis를 앞에 두고 실행한다. 작업 스레드에서 돈다.

    PyMySQL이 블로킹이라 이벤트 루프에서 직접 부르면 진행 상황 전송이 멈춘다. 그래서 측정 전체를
    스레드 하나에서 블로킹 소켓으로 돌리고, 취소는 요청마다 cancelled를 확인해 반영한다.
    """
    result = Measurement()
    progress["total"] = count
    sample_set = set(sample_idx)
    connection = mysql_db.connect()
    redis = SyncRedis() if use_cache else None
    try:
        cursor = connection.cursor()
        if redis is not None and cold:
            keys = [b"stats:%d" % article_id for article_id in state.cached_ids]
            for start in range(0, len(keys), DEL_BATCH):
                redis.call("DEL", *keys[start : start + DEL_BATCH])
            state.cached_ids.clear()
        result.wall_started = time.perf_counter()
        for index in range(count):
            if cancelled.is_set():
                break
            article_id = ids[index]
            started = time.perf_counter()
            if redis is None:
                stats = mysql_db.fetch_stats(cursor, article_id)
            else:
                key = b"stats:%d" % article_id
                cached = redis.call("GET", key)
                if cached is None:
                    stats = mysql_db.fetch_stats(cursor, article_id)
                    if stats is not None:
                        value = json.dumps(stats, ensure_ascii=False)
                        redis.call("SET", key, value, *(("EX", ttl) if ttl else ()))
                    # 넣은 적이 있는데 없다면 수명이 끝난 것이다.
                    result.misses["expired" if article_id in state.cached_ids else "new"] += 1
                    state.cached_ids.add(article_id)
                    result.hits.append(0)
                else:
                    assert isinstance(cached, bytes)
                    stats = json.loads(cached)
                    if ttl:
                        # 접근할 때마다 수명을 새로 준다(슬라이딩 만료). 계속 요청되는 문서는 남고
                        # 한동안 요청이 없는 문서만 만료된다. GET은 수명을 건드리지 않아서 따로 보낸다.
                        redis.call("EXPIRE", key, ttl)
                    result.hits.append(1)
                    progress["hits"] += 1
            result.latencies.append(time.perf_counter() - started)
            result.groups.append(state.popular[article_id])
            if index in sample_set:
                result.answers.append(json.dumps(stats, ensure_ascii=False, sort_keys=True))
            progress["done"] = index + 1
    finally:
        connection.close()
        if redis is not None:
            redis.close()
    return result


async def _measure(mode: str, count: int, samples: int, cold: bool, ttl: int, progress: dict) -> dict:
    ids = state.replay
    sample_idx = _sample_indices(count, samples)
    if mode == "file":
        measured = await _measure_file(ids, sample_idx, progress)
    elif mode == "dbms":
        measured = await _measure_dbms(ids, count, sample_idx, progress)
    else:
        cancelled = threading.Event()
        try:
            measured = await asyncio.to_thread(
                _measure_join, ids, count, sample_idx, mode == "mysql_redis", cold, ttl, progress, cancelled
            )
        except asyncio.CancelledError:
            cancelled.set()  # 스레드는 강제로 멈출 수 없으므로 다음 요청에서 스스로 그만두게 한다.
            raise
    elapsed = time.perf_counter() - measured.wall_started
    result = await asyncio.to_thread(_summarize, mode, count, measured, elapsed)
    if mode == "mysql_redis":
        result |= {"cold": cold, "ttl_s": ttl}
    return result


def _summarize(mode: str, count: int, measured: Measurement, elapsed: float) -> dict:
    latencies, hits, groups = measured.latencies, measured.hits, measured.groups
    total = len(latencies)
    ordered = sorted(latencies)
    mean = sum(latencies) / total

    def percentile(q: float) -> float:
        return ordered[min(total - 1, int(total * q))] * 1000

    # 인기 문서와 비인기 문서를 따로 집계한다. 캐시는 다시 요청되는 문서에서만 이득을 보므로
    # 전체 평균만으로는 어느 쪽에서 빨라지고 어느 쪽에서 느려지는지 보이지 않는다.
    by_group: dict[str, dict | None] = {}
    for name, flag in (("popular", 1), ("unpopular", 0)):
        picked = sorted(seconds for seconds, group in zip(latencies, groups) if group == flag)
        if not picked:
            by_group[name] = None
            continue
        entry = {
            "measured": len(picked),
            "mean_ms": sum(picked) / len(picked) * 1000,
            "p50_ms": picked[len(picked) // 2] * 1000,
            "p95_ms": picked[min(len(picked) - 1, int(len(picked) * 0.95))] * 1000,
        }
        if hits:
            entry["hit_rate"] = sum(hit for hit, group in zip(hits, groups) if group == flag) / len(picked)
        by_group[name] = entry

    buckets = min(SERIES_BUCKETS, total)
    series = []
    for bucket in range(buckets):
        low, high = bucket * total // buckets, (bucket + 1) * total // buckets
        point = {"x": high * count // total, "ms": sum(latencies[low:high]) / (high - low) * 1000}
        if hits:
            point["hit_rate"] = sum(hits[low:high]) / (high - low)
        series.append(point)

    result = {
        "type": "result",
        "mode": mode,
        "count": count,
        "measured": total,
        "estimated": total < count,  # 표본만 잰 경우 전체 소요 시간은 평균 × 요청 수로 추정한 값이다.
        "elapsed_s": elapsed,
        "total_s": mean * count,
        "throughput": 1 / mean,
        "mean_ms": mean * 1000,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1] * 1000,
        "series": series,
        "groups": by_group,
        "sample_count": len(measured.answers),
        "sample_digest": hashlib.sha1("\n".join(measured.answers).encode()).hexdigest()[:12],
    }
    if hits:
        result |= {
            "hits": sum(hits),
            "misses": total - sum(hits),
            "hit_rate": sum(hits) / total,
            "miss_new": measured.misses["new"],
            "miss_expired": measured.misses["expired"],
        }
    return result


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/info")
async def info() -> dict:
    return {
        "ready": state.ready,
        "stage": state.stage,
        "error": state.error,
        "busy": state.lock.locked(),
        "articles": state.article_count,
        "replay": len(state.replay),
        "distinct_ids": state.distinct_ids,
        "dbms_startup_s": state.dbms_startup_seconds,
        "mysql_version": state.mysql_version,
        "cache_ttl_s": CACHE_TTL_SECONDS,
        "popular_min_count": state.popular_min_count,
        "popular_docs": state.popular_docs,
        "popular_request_share": state.popular_request_share,
    }


@app.get("/api/run")
async def run(
    mode: str, count: int = 100_000, samples: int = 200, cold: bool = True, ttl: int = CACHE_TTL_SECONDS
) -> StreamingResponse:
    """한 방식을 측정하며 진행 상황과 결과를 한 줄에 JSON 하나씩(NDJSON) 흘려보낸다.

    samples는 file 방식에서 실제로 재는 요청 수이자 방식끼리 답을 대조하는 위치의 수다.
    cold와 ttl은 mysql_redis 방식에서만 쓴다. ttl은 캐시 항목의 수명(초)이고 0이면 만료 없이 넣는다.
    클라이언트가 연결을 끊으면(중지 버튼) 측정도 취소한다.
    """
    if not state.ready:
        raise HTTPException(503, state.error or "아직 준비 중입니다.")
    if mode not in MODES:
        raise HTTPException(400, f"mode는 {', '.join(MODES)} 중 하나여야 합니다.")
    if not 1 <= count <= len(state.replay):
        raise HTTPException(400, f"count는 1 이상 {len(state.replay)} 이하여야 합니다.")
    if not 1 <= samples <= MAX_SAMPLES:
        raise HTTPException(400, f"samples는 1 이상 {MAX_SAMPLES} 이하여야 합니다.")
    if not 0 <= ttl <= MAX_TTL_SECONDS:
        raise HTTPException(400, f"ttl은 0 이상 {MAX_TTL_SECONDS} 이하여야 합니다.")
    if state.lock.locked():
        raise HTTPException(409, "다른 측정이 진행 중입니다.")

    async def stream():
        async with state.lock:
            progress = {"type": "progress", "mode": mode, "done": 0, "total": 0, "hits": 0}
            task = asyncio.create_task(_measure(mode, count, samples, cold, ttl, progress))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.2)
                    yield json.dumps(progress) + "\n"
                try:
                    yield json.dumps(task.result(), ensure_ascii=False) + "\n"
                except Exception as exc:  # 측정 실패를 성공처럼 보이지 않게 그대로 알린다.
                    yield json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n"
            finally:
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
