# 조회 성능 비교 페이지

`full-replay.csv`의 요청 순서대로 두 가지 비교를 수행하고 요청당 지연을 잰다.

| 비교 | 방식 | 조회 경로 |
| --- | --- | --- |
| 1. 인덱스 | 파일시스템 | 요청마다 `articles.csv`를 첫 줄부터 읽어 id가 같은 줄을 찾는다 |
| | mini-DBMS | mini-DBMS API 서버에 `SELECT title FROM articles WHERE id = N;` (B+ Tree 인덱스) |
| 2. 캐시 | MySQL | 요청마다 조인 쿼리를 실행한다 |
| | MySQL + mini-Redis | mini-Redis `GET` → 없으면 조인 쿼리 실행 후 `SET` (cache-aside) |

비교 1은 문서 id로 제목 한 건을 찾고, 비교 2는 문서 하나의 제목과 조회 통계를 가져온다.

```sql
SELECT a.title, COUNT(*), MIN(p.viewed_at), MAX(p.viewed_at)
FROM articles a JOIN pageviews p ON p.article_id = a.id
WHERE a.id = ? GROUP BY a.id, a.title
```

## 폴더 배치

세 폴더가 한 폴더 안에 나란히 있어야 한다. 서버는 자기 위치를 기준으로 나머지를 찾으므로, 이 배치만
지키면 저장소를 어디에 두어도 된다.

```
Redis-DBMS-Test/
├── mini-DBMS/jungle-week11-12-dbms/   mini-DBMS
├── mini-Redis/                        mini-Redis
└── mini-Test/
    ├── articles.csv                   문서 168만 건 (id, title)
    ├── full-replay.csv                조회 기록 298만 건 (seq, timestamp_utc, id)
    └── perf-page/                     이 폴더
```

## 준비

아래 명령은 모두 이 폴더(`mini-Test/perf-page`)에서 실행한다.

`full-replay.csv`(113MB)는 Git LFS로 저장되어 있다. Git LFS 없이 clone했다면 파일이 몇 줄짜리 포인터로만
받아지므로, 설치한 뒤 실제 파일을 받는다.

```bash
brew install git-lfs && git lfs install && git lfs pull
```

MySQL은 Homebrew로 설치한다. 시스템 서비스로 등록할 필요는 없다.

```bash
brew install mysql
```

Python 패키지는 이 폴더 전용 가상환경에 둔다. 가상환경(`.venv/`)과 MySQL 데이터(`.mysql/`)는 저장소에
올리지 않으므로 clone한 뒤 한 번 만들어야 한다.

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn pymysql
```

## 실행

```bash
.venv/bin/python server.py
```

그다음 http://127.0.0.1:8765 를 연다. 서버가 mini-DBMS(포트 18001), mini-Redis(포트 16379), MySQL(포트 13306)을
자식 프로세스로 띄우고, 종료할 때 함께 내린다.

* 처음 실행할 때는 `.mysql/` 아래에 MySQL 데이터 폴더를 만들고 두 CSV를 넣는다(약 15초, 디스크 약 560MB).
  다음부터는 넣어 둔 데이터를 그대로 쓴다. 처음부터 다시 넣으려면 서버를 내리고 `.mysql/` 폴더를 지운다.
* mini-DBMS가 168만 행의 인덱스를 만드는 약 7초 동안은 페이지에 진행 단계가 표시된다.
* MySQL의 root 계정은 비밀번호 없이 만들어지고, 서버는 127.0.0.1에서만 접속을 받는다.

| 환경변수 | 기본값 |
| --- | --- |
| `MINI_DBMS_DIR` | `../../mini-DBMS/jungle-week11-12-dbms` |
| `MINI_REDIS_DIR` | `../../mini-Redis` |
| `PERF_PORT`, `PERF_DBMS_PORT`, `PERF_REDIS_PORT`, `PERF_MYSQL_PORT` | 8765, 18001, 16379, 13306 |
| `PERF_MYSQLD` | PATH의 `mysqld`, 없으면 Homebrew 기본 위치 |
| `PERF_CACHE_TTL` | 60 (초, 페이지 입력란의 기본값) |

## 구성

| 파일 | 역할 |
| --- | --- |
| `server.py` | 세 서버 기동, 측정, 진행 상황 스트리밍(`GET /api/run`), 페이지 제공 |
| `mysql_db.py` | MySQL 전용 데이터 폴더 초기화, 스키마, CSV 적재, 조인 쿼리 |
| `dbms_server.py` | mini-DBMS의 `create_app`에 `articles` 테이블만 등록해 띄우는 실행기 |
| `index.html` | 테스트 페이지 |

mini-DBMS와 mini-Redis의 코드는 수정하지 않고, 각자의 서버를 각자의 프로토콜(HTTP, RESP)로 호출한다.
mini-Redis의 RDB 등은 임시 디렉터리에 두었다가 종료 때 지운다.

## 측정 조건과 읽는 법

* 클라이언트 하나가 응답을 받은 뒤 다음 요청을 보낸다(동시 요청 없음). 지연은 요청 한 건의 왕복 시간이다.
* 모든 프로세스가 한 컴퓨터에서 돌고, 어느 방식도 디스크를 읽지 않는다. `articles.csv`는 OS 파일 캐시에,
  MySQL 데이터는 버퍼 풀(1GB)에 올라와 있다. mini-Redis는 메모리 한도 없이 띄운다.
* 비교 1과 비교 2는 조회 내용도 클라이언트도 달라서 서로의 수치를 직접 비교하지 않는다. 비교 1은 asyncio
  스트림으로, 비교 2는 블로킹 소켓(PyMySQL과 같은 방식의 RESP 클라이언트)으로 한 스레드에서 잰다.

### 비교 1

* 파일시스템은 한 건에 수십 ms라, 전체 요청에서 고르게 뽑은 표본(기본 200건)만 실제로 잰다.
  총 소요 시간은 평균 × 요청 수로 추정한 값이다.
* mini-DBMS의 지연은 대부분 HTTP·JSON 왕복이다. 같은 조회를 프로세스 안에서 직접 호출하면 약 0.04 ms다.

### 비교 2

* `pageviews`에는 `article_id` 인덱스만 있다. 조회 시각의 최소·최대를 구하려면 그 문서의 조회 기록 행을
  모두 읽어야 해서, 많이 조회된 문서일수록 쿼리가 비싸다(가장 많이 조회된 문서 약 26 ms, 드문 문서 약 0.2 ms).
* 캐시에는 쿼리 결과를 JSON으로 넣는다(`stats:N`). 적중은 왕복 2번(GET, EXPIRE. 수명이 0이면 GET 1번),
  미스는 3번(GET, 조인 쿼리, SET)이다.
* 캐시 항목은 `SET … EX 수명`으로 넣는다. 수명은 페이지의 "캐시 수명" 입력란에서 측정마다 정하고
  (기본 60초, 0이면 만료 없음), 실제 경과 시간 기준이다.
* 적중할 때마다 `EXPIRE`로 수명을 새로 준다(슬라이딩 만료). mini-Redis의 `GET`은 수명을 건드리지 않기 때문에
  따로 보낸다. 계속 요청되는 문서는 남고, 수명 동안 한 번도 요청되지 않은 문서만 만료된다.
* mini-Redis는 만료된 키를 바로 지우지 않는다. 다시 조회될 때 지우거나, 5초마다 표본을 뽑아 지운다.
* 미스는 처음 요청 / 만료로 나눠 센다. "캐시를 비우고 시작"을 끄면 이전 실행에서 채운 캐시를 그대로 쓴다.
* 인기 문서와 비인기 문서를 나눠서도 보여 준다. 전체 조회 기록에서 요청 수가 많은 문서부터 더해 요청의
  80%에 닿는 지점의 요청 수(7회)를 경계로 삼는다. 7회 이상 요청된 71,330건이 인기 문서이고, 요청된 문서의
  20.1%이지만 요청의 80.1%를 차지한다.

### 결과 대조

고르게 뽑은 위치에서 얻은 답을 같은 비교의 두 방식끼리 맞춰 보고 페이지에 표시한다. MySQL에 넣은 제목
168만 건은 원본 CSV와 전부 일치하는 것을 확인했다(제목이 글자 그대로 `NULL`인 문서, 따옴표·쉼표·역슬래시가
든 제목 포함).
