# 💿 mini-dbms

> **UTF-8 CSV를 영구 저장소로 사용하는 학습용 DBMS입니다.**
> 

SQL 파서와 실행기를 CLI와 FastAPI에서 공유하며, 직접 구현한 메모리 B+ Tree로 정수 ID를 조회합니다.

# 📀 설치

> **Python 3.12 이상에서 다음 명령어를 실행합니다.**
> 
> 
> ```bash
> uv venv .venv
> uv pip install --python .venv/bin/python -r requirements.txt
> ```
> 
- `uv`가 없다면 Python의 `venv`와 `pip`로 같은 가상환경을 준비할 수 있습니다.
- CLI, 코어 테스트, 성능 측정에는 Python 표준 라이브러리만 필요합니다.
- API 실행과 API 테스트에는 `requirements.txt`의 패키지가 필요합니다.

# ⁉️ 사용법

## 임베디드 라이브러리

`from mini_dbms import Catalog, Executor, Insert, Select, Selected, DBError`가
공개 진입점입니다. `parse`, `split_statements`와 구체적인 DB 오류도 내보냅니다.
이 import는 HTTP 앱이나 CLI를 시작하지 않으며 외부 패키지를 요구하지 않습니다.
정수 열은 Python 공개 API에서도 `bool`을 받지 않습니다. 잘못된 타입은 파일 쓰기 전에 거부합니다.
테이블 스키마와 파일 준비는 소비 앱의 책임이고, 실행기는 예매·좌석을 모릅니다.
`Catalog.get()`이 반환하는 상태의 인덱스·락을 소비 앱에서 변경하지 마세요.

```python
from pathlib import Path
from mini_dbms import Catalog, Executor, Insert, Select

executor = Executor(Catalog(Path("tables")))
executor.execute(Insert("students", values=[42, "민수", 20]))
result = executor.execute(Select("students", where=("id", 42)))
```

`tests/test_public_api.py`는 다른 도메인의 CSV로 공개 API를 호출하며, 별도
디렉터리와 외부 패키지가 없는 Python 실행에서도 삽입·중복 거부·재시작 조회를 검증합니다.

## CLI

> **다음 명령어를 실행하여 SQL 파일을 입력합니다.**
> 
> 
> ```bash
> .venv/bin/python -m mini_dbms examples/query.sql --tables tables
> ```
> 
- 테이블은 미리 준비한 디렉터리의 CSV 파일로 등록합니다. 기본 디렉터리는 `./tables`이고,
`students.csv`는 `students` 테이블입니다.
- SQL로 테이블이나 파일을 생성하지 않습니다.
- 프로그램에서 `Catalog(directory, {"students": "students.csv"})`로 매핑을 지정할 수도 있습니다.
등록 경로는 심볼릭 링크를 해소한 뒤 테이블 디렉터리 내부여야 합니다.
- 첫 줄은 `id:int,name:string,seat:int`처럼 이름과 타입을 담습니다. 타입은 `int`,
`string`을 지원하며 `id:int`가 반드시 하나 있어야 합니다.
- 각 레코드는 물리적 한 줄이고, 문자열의 쉼표와 큰따옴표는 CSV 인용 규칙으로 저장합니다. 필드 내부 CR/LF는 거부합니다. 예제 SQL을 다시 실행하면 새 자동 ID로 행이 추가됩니다.
- 지원되는 명령어는 `INSERT`, `SELECT` 입니다.
    
    ```sql
    INSERT INTO students VALUES (42, '민수', 20);
    INSERT INTO students (name, seat) VALUES ('O''Brien, 민;수', 21);
    SELECT * FROM students;
    SELECT name, seat FROM students WHERE id = 42;
    ```
    
- 키워드는 대소문자를 구분하지 않고 식별자는 원래 표기를 사용합니다. 정수와 작은따옴표 문자열, 단일 `WHERE column = literal`, 출력 열 선택을 지원합니다.
- 각 SQL 문장은 세미콜론으로 끝나야 합니다. 문자열 내부 세미콜론은 구분하지 않습니다.
ID 자동 부여는 명시 열 목록에서 `id`만 생략했을 때 적용합니다.
- **SELECT 결과는 헤더를 포함한 CSV로 `stdout`에 출력합니다**.
- **INSERT 성공 시 CLI 출력은 없습니다**.
- 오류는 문장 순번과 함께 `stderr`에 출력하고 다음 문장을 계속 실행합니다. 전체 성공은 종료 코드 0, 입력 오류나 문장 하나 이상 실패는 1입니다.

## API

> **기본적인 API 사용 예시는 다음과 같습니다**.
> 
> 
> ```bash
> .venv/bin/python -m mini_dbms.server --tables tables
> curl -s http://127.0.0.1:8000/query \
>   -H 'Content-Type: application/json' \
>   -d '{"sql":"SELECT * FROM students WHERE id = 42;"}'
> ```
> 
- `POST /query`는 문자열 필드 `sql`로 SQL 한 문장만 받습니다.
    
    ```json
    {"success": true, "columns": ["id", "name", "seat"], "rows": [[42, "민수", 20]]}
    ```
    
- INSERT 응답은 `{"success":true,"id":42}`, 오류 응답은 `{"success":false,"error":{"code":"…","message":"…"}}` 형태입니다.
- 다중 문장 요청은 첫 문장도 실행하지 않고 거부합니다. OpenAPI 문서는 로컬 서버의 `/docs`에서 확인할 수 있습니다.
- 선택한 기본 설정:
    
    
    | 설정 | 기본값 |
    | --- | --- |
    | 주소 / 포트 | `127.0.0.1:8000` |
    | 서버 프로세스 | 1개 |
    | SQL 작업 스레드 | 4개 (`--workers`) |
    | 대기 슬롯 | 16개 (`--queue-size`) |
    | 최대 SQL 길이 | 10,000자 (`--max-sql-length`) |
    | 포화 정책 | 실행 중 작업 + 대기 슬롯 초과 시 HTTP 503 |
    | 테이블 락 | 쓰기 대기 우선, 여러 읽기 동시 허용 |
    | B+ Tree 차수 | 64 |
- 서버 시작 시 CSV를 검증하고 모든 인덱스를 재구축합니다.
- 파일 추가와 인덱스 등록은 테이블 쓰기 락 안에서 수행하며, 실패하면 이전 EOF와 인덱스를 복원합니다.
    - 복구를 확인할 수 없으면 해당 테이블만 사용 불가로 표시합니다.
    - 읽기마다 별도 파일 핸들을 사용하므로 동시에 실행되는 조회의 seek 위치는 독립적입니다.
    - 이 락은 **단일 프로세스의 스레드 사이**에서만 유효합니다. 같은 CSV에 여러 서버·CLI 프로세스를 동시에 쓰지 마세요. 실행 중 외부에서 CSV를 수정하는 사용도 지원하지 않습니다. 인증 없는 개발·테스트용 API이며, 정상 재시작 시 CSV에서 복구합니다.

**강제 종료·전원 장애에 대한 내구성이나 WAL 복구, DDL, UPDATE, DELETE, 다중 문장 트랜잭션은 구현 범위에 포함되지 않았습니다.**

# 🙏 테스트

> **다음 명령어를 실행하여 테스트를 시작합니다.**
> 
> 
> ```bash
> .venv/bin/python -m unittest discover -s tests -v
> ```
> 
- 타입 검사는 `requirements-dev.txt`를 설치한 뒤 실행합니다.
    
    ```bash
    uv pip install --python .venv/bin/python -r requirements-dev.txt
    .venv/bin/basedpyright mini_dbms
    ```
    
- 실제 임시 CSV, 별도 CLI 프로세스, 로컬 HTTP 서버와 이벤트 기반 경합 시나리오를 사용합니다. 대규모 성능 측정은 일반 회귀 테스트와 분리합니다.
    
    ```bash
    .venv/bin/python -m mini_dbms.benchmark --rows 1000000 --repeats 3
    ```
    
- 기본적으로 **임시 디렉터리에 실제 INSERT 경로로 데이터를 넣고 측정 후 삭제합니다**. 보관하려면 `--directory benchmark-data/run-1`을 지정하세요.
- 기존 `benchmark.csv`는 덮어쓰지 않습니다. 고정 seed로 데이터와 INSERT SQL을 생성·파싱하고 행 수를 확인한 다음,인덱스를 다시 구축합니다.
- 앞/중간/끝/미등록 ID마다 인덱스 조회, 같은 ID의 강제 선형 조회, 비인덱스 필드 조회의 결과를 대조하고 반복 측정 중앙값을 JSON으로 출력합니다.
- 데이터 준비, INSERT, 인덱스 구축, 순수 조회, CSV 결과 직렬화 시간을 따로 기록합니다.
    - 조회 시간에는 파일 I/O와 디코딩을 포함하고 터미널 출력은 포함하지 않습니다.
    - 출력 시간은 임시 UTF-8 파일에 CSV를 직렬화하고 flush하는 시간이며 파일 생성 시간은 제외합니다.
    - OS 캐시를 비우지 않는 조건이며 특정 속도 배율을 합격 기준으로 삼지 않습니다.

상세 명세는 `docs/REQUIREMENTS.md`, 설계는 `docs/ARCHITECTURE.md`, 검증 기준은 `tests/TEST_ARCHITECTURE.md`에 있습니다. 실행한 검증과 기본값 결정 기록은 `docs/VERIFICATION.md`, 측정 원본은 `docs/benchmark-results.json`에서 확인할 수 있습니다.
