"""성능 비교용 MySQL 인스턴스: 전용 데이터 폴더, 스키마, 적재, 조인 쿼리.

시스템에 서비스로 등록된 MySQL을 쓰지 않는다. perf-page/.mysql 아래에 데이터 폴더를 따로 만들고
측정 서버가 mysqld를 자식 프로세스로 띄웠다가 함께 내린다. 다른 MySQL 설정이나 데이터에 손대지 않기 위해서다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent
RUNTIME = HERE / ".mysql"
DATADIR = RUNTIME / "data"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PERF_MYSQL_PORT", "13306"))
DATABASE = "mini_perf"
# 적재를 다시 해야 하는 변경(스키마, 원본 파일 교체)이 생기면 이 값을 올린다.
LOAD_VERSION = "1"

# 문서 하나의 제목과 조회 통계. articles와 pageviews를 문서 id로 조인하고 집계한다.
# pageviews에는 외래 키 열(article_id)의 인덱스만 있어서, 조회 시각의 최소·최대를 구하려면
# 그 문서의 조회 기록 행을 모두 읽어야 한다. 그래서 자주 조회된 문서일수록 쿼리가 비싸다.
JOIN_SQL = (
    "SELECT a.title, COUNT(*), MIN(p.viewed_at), MAX(p.viewed_at) "
    "FROM articles a JOIN pageviews p ON p.article_id = a.id "
    "WHERE a.id = %s GROUP BY a.id, a.title"
)

_SCHEMA = (
    "CREATE TABLE articles ("
    " id INT NOT NULL PRIMARY KEY,"
    " title VARCHAR(512) NOT NULL"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE pageviews ("
    " seq INT NOT NULL PRIMARY KEY,"
    " viewed_at DATETIME(3) NOT NULL,"
    " article_id INT NOT NULL,"
    " KEY idx_article (article_id)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    "CREATE TABLE load_state (version VARCHAR(16) NOT NULL PRIMARY KEY)",
)


def find_mysqld() -> str | None:
    """mysqld 실행 파일을 찾는다. PATH에 없으면 Homebrew의 기본 위치를 본다."""
    explicit = os.environ.get("PERF_MYSQLD")
    if explicit:
        return explicit
    found = shutil.which("mysqld")
    if found:
        return found
    for candidate in ("/opt/homebrew/opt/mysql/bin/mysqld", "/usr/local/opt/mysql/bin/mysqld"):
        if Path(candidate).exists():
            return candidate
    return None


def initialize(mysqld: str) -> None:
    """데이터 폴더가 없으면 만든다. root 계정은 비밀번호 없이 생기고, 서버는 127.0.0.1에서만 받는다.

    Raises:
        RuntimeError: 초기화에 실패한 경우. 반쯤 만들어진 폴더는 지워 다음 시작 때 다시 시도하게 한다.
    """
    if (DATADIR / "mysql.ibd").exists():
        return
    shutil.rmtree(DATADIR, ignore_errors=True)
    RUNTIME.mkdir(exist_ok=True)
    done = subprocess.run(
        [mysqld, "--initialize-insecure", f"--datadir={DATADIR}"], capture_output=True, text=True
    )
    if done.returncode != 0:
        shutil.rmtree(DATADIR, ignore_errors=True)
        raise RuntimeError(f"MySQL 데이터 폴더 초기화 실패: {done.stderr.strip()[-500:]}")


def server_command(mysqld: str) -> list[str]:
    return [
        mysqld,
        f"--datadir={DATADIR}",
        f"--port={PORT}",
        f"--bind-address={HOST}",
        f"--socket={RUNTIME / 'mysql.sock'}",
        f"--pid-file={RUNTIME / 'mysqld.pid'}",
        "--mysqlx=OFF",
        # 원본 CSV를 서버가 직접 읽어 적재할 수 있도록 mini-Test 폴더만 허용한다.
        f"--secure-file-priv={DATA_DIR}",
        # 두 테이블과 인덱스가 모두 메모리에 올라오는 크기. 파일시스템·mini-DBMS와 마찬가지로
        # 디스크 읽기가 끼지 않는 조건에서 비교하려는 것이다.
        "--innodb-buffer-pool-size=1G",
        # 읽기만 측정하므로 복제용 로그는 끄고, 적재가 빨리 끝나도록 커밋마다 디스크에 내리지 않는다.
        "--skip-log-bin",
        "--innodb-flush-log-at-trx-commit=2",
    ]


def connect(database: str | None = DATABASE) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=HOST, port=PORT, user="root", password="", database=database, charset="utf8mb4", autocommit=True
    )


def ensure_loaded(report: Callable[[str], None]) -> None:
    """두 CSV가 테이블에 들어 있는지 확인하고, 없으면 적재한다(처음 한 번, 1분 안팎).

    적재가 끝난 뒤에만 load_state에 표시를 남긴다. 도중에 중단됐다면 표시가 없으므로
    다음 시작 때 데이터베이스를 지우고 처음부터 다시 넣는다.
    """
    connection = connect(database=None)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW DATABASES LIKE %s", (DATABASE,))
            if cursor.fetchone():
                cursor.execute(f"SHOW TABLES IN {DATABASE} LIKE 'load_state'")
                if cursor.fetchone():
                    cursor.execute(f"SELECT 1 FROM {DATABASE}.load_state WHERE version = %s", (LOAD_VERSION,))
                    if cursor.fetchone():
                        return
            cursor.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
            cursor.execute(f"CREATE DATABASE {DATABASE} DEFAULT CHARACTER SET utf8mb4")
            cursor.execute(f"USE {DATABASE}")
            for statement in _SCHEMA:
                cursor.execute(statement)
            report("MySQL에 문서 168만 건을 넣는 중")
            # ESCAPED BY '': 제목에 역슬래시가 들어 있어, 이스케이프 문자로 해석되면 글자가 사라진다.
            # IFNULL: 제목이 글자 그대로 NULL인 문서가 있는데, LOAD DATA는 따옴표 없는 NULL을 빈 값으로 읽는다.
            cursor.execute(
                f"LOAD DATA INFILE %s INTO TABLE articles CHARACTER SET utf8mb4 "
                f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' ESCAPED BY '' "
                f"LINES TERMINATED BY '\\n' IGNORE 1 LINES (id, @title) SET title = IFNULL(@title, 'NULL')",
                (str(DATA_DIR / "articles.csv"),),
            )
            report("MySQL에 조회 기록 298만 건을 넣는 중")
            cursor.execute(
                f"LOAD DATA INFILE %s INTO TABLE pageviews "
                f"FIELDS TERMINATED BY ',' LINES TERMINATED BY '\\n' IGNORE 1 LINES "
                f"(seq, @viewed, article_id) "
                f"SET viewed_at = STR_TO_DATE(@viewed, '%%Y-%%m-%%dT%%H:%%i:%%s.%%fZ')",
                (str(DATA_DIR / "full-replay.csv"),),
            )
            cursor.execute("ANALYZE TABLE articles, pageviews")
            cursor.fetchall()
            cursor.execute("INSERT INTO load_state VALUES (%s)", (LOAD_VERSION,))
    finally:
        connection.close()


def fetch_stats(cursor: pymysql.cursors.Cursor, article_id: int) -> dict | None:
    """조인 쿼리를 실행해 응답으로 내보낼 형태(dict)로 돌려준다. 없는 문서면 None."""
    cursor.execute(JOIN_SQL, (article_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    title, views, first, last = row
    return {
        "title": title,
        "views": views,
        "first": first.isoformat(timespec="milliseconds"),
        "last": last.isoformat(timespec="milliseconds"),
    }
