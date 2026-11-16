# 구현 및 검증 기록

검증일: 2026-11-16. 실행 환경: Python 3.14.4, Linux WSL2 x86_64.
초기 저장소는 요구사항·설계·테스트 문서만 있었으며, 기존 실행 코드와 테스트는 없었습니다.

## 검증 명령

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/basedpyright mini_dbms
.venv/bin/python -m mini_dbms.benchmark --rows 1000000 --repeats 3
```

최종 회귀 테스트는 **193개 통과**, 타입 검사는 **오류 0개, 경고 0개**입니다.
두 명령을 연결한 실행의 종료 코드는 0이었습니다.
테스트는 실제 임시 CSV와 로컬 Uvicorn 서버를 사용하며, 동시 실행 순서는 이벤트·장벽으로 제어합니다.
최종 독립 게이트 검토도 APPROVE이며 차단 사항은 없었습니다.
게이트 보고서는 `.omo/evidence/1bb118fd-c1fd-445a-814b-ee48cfb2c0ec-gate-review.md`에 있습니다.
이 보고서는 로컬 에이전트 자료이므로 `.omo/`와 함께 Git 공개 대상에서 제외합니다.

## 명세와 검증 근거

| 요구사항 | 구현 | 검증 근거 |
| --- | --- | --- |
| 여러 SQL, 문자열 경계, 전체 입력 소비, 오류 후 계속 실행 | `parser.py`, `__main__.py` | `test_parser.py`, `test_cli.py`; 이스케이프 따옴표 뒤 세미콜론과 닫히지 않은 꼬리 구간 포함 |
| 스키마·타입·열 검증, 투영과 동등 비교 | `storage.py`, `executor.py` | `test_storage.py`, `test_executor.py`, `test_integrity.py` |
| CSV 영속성·UTF-8·한 줄 레코드·경로 제한 | `storage.py`, `catalog.py` | 실제 CLI 재실행, CR/LF 거부, CSV 인용·UTF-8 손상, 외부 심볼릭 링크 보존 검사 |
| B+ Tree 삽입·검색·분할·중복 거부 | `bptree.py` | `test_bptree.py`: 정렬·역순·고정 seed 순서, 구분 키·자식 범위·동일 리프 깊이 |
| 정확한 바이트 오프셋과 재구축 | `storage.py`, `catalog.py` | LF·CRLF·한글·마지막 개행 없는 파일 검사, 실제 별도 서버 프로세스 재시작 |
| 테이블별 자동 ID·파일·인덱스 일관성 | `executor.py` | 음수 최대 ID, 명시 ID, 중복, 부분 쓰기·flush·트리 변경 후 실패·재구축 실패 검사 |
| 파서와 CSV의 문자열 왕복 | 공통 파서·실행기·저장소 | `test_roundtrip.py`: 고정 seed 60개 문자열과 ID가 첫 열이 아닌 스키마 |
| 인덱스·강제 선형·비인덱스 조회 동등성 | `executor.py`, `benchmark.py` | 소규모 회귀와 실제 100만 건 측정; 앞·중간·끝·미등록 ID |
| 공통 엔진을 사용하는 HTTP API | `api.py`, `server.py` | `test_api.py`: 실제 HTTP, JSON 오류, SQL 길이 경계, 다중 문장 무변경, 트리 경유, 동시 삽입 |
| 제한 스레드 풀·포화·재사용·취소 | `pool.py`, `api.py` | `test_concurrency.py`, `test_api_cancellation.py`; 요청 취소 후 실행 중 작업의 슬롯 유지 |
| 공유 읽기·쓰기 우선·테이블 독립성 | `locks.py`, `executor.py` | `test_locks.py`, `test_isolation.py`; 쓰기와 인덱스 등록 사이 조회 대기, 서로 다른 seek 위치 교차 |
| 실패 후 락·슬롯 해제와 테이블 격리 | 실행기·락·풀 | 읽기/쓰기 실패 후 다음 작업, 복구 실패 시 해당 테이블만 차단하는 회귀 테스트 |

구현 전에 각 담당 작업의 테스트를 작성하고 미구현 실패를 확인했습니다.
통합 중 발견한 문자열 경계, 공통 INSERT 인자 순서, 인용된 CSV 필드의 CR,
큰 문자열 재읽기, JSON 배열 요청 오류도 실패를 재현한 뒤 수정했습니다.

중복 키 거부를 의도적으로 무력화한 격리 실행에서는
`BPlusTreeDuplicateTests.test_duplicate_key_raises_value_error`가 실패했습니다.
이 검사는 메모리 안에서만 교체했으며 저장소의 구현 파일은 변경하지 않았습니다.

## 실제 서버·CLI 시나리오

임시 테이블 디렉터리에 샘플 헤더를 복사하고 아래 명령으로 포트 0의 서버를 시작했습니다.

```sh
.venv/bin/python -m mini_dbms.server --tables <임시 디렉터리> --port 0
```

| 작업 | 관찰 결과 |
| --- | --- |
| `INSERT INTO students (name, seat) VALUES ('O''Brien, 민;수', 20);` | HTTP 200, `{"success":true,"id":1}` |
| `SELECT * FROM students WHERE id = 1;` | HTTP 200, `[[1,"O'Brien, 민;수",20]]` |
| INSERT와 SELECT를 한 요청에 전달 | HTTP 400 `syntax_error`, 해당 INSERT는 CSV에 없음 |
| JSON 배열 `[]` 전달 | HTTP 400, 동일한 `bad_request` 오류 구조 |
| SIGTERM으로 정상 종료 | `Application shutdown complete`와 `Finished server process` 확인 |
| 별도 CLI에서 `examples/select.sql` 실행 | 동일한 값이 CSV 형식으로 출력됨 |
| 새 서버 프로세스로 동일 디렉터리 재시작 | 기존 ID 1 조회 성공 |
| 재시작 후 자동 ID INSERT | HTTP 200, ID 2 발급 |

수동 QA 서버 두 개는 모두 검증 후 정상 종료했습니다. 저장소의 샘플 테이블은 수정하지 않았습니다.

## 성능 측정 조건

100만 개 INSERT SQL을 고정 seed로 생성·파싱하고 실제 실행기로 삽입합니다.
행 수를 확인한 후 새 카탈로그로 인덱스를 재구축합니다.
앞·중간·끝·없는 ID마다 세 가지 조회를 3회 실행하여 결과를 대조하고 중앙값을 기록합니다.
최종 수치는 `benchmark-results.json`에 보관합니다.

데이터/SQL 준비, 삽입, 인덱스 구축, 순수 조회, 결과 출력을 분리합니다.
조회에는 파일 I/O와 디코딩이 포함됩니다. 출력은 임시 UTF-8 파일에 대한 CSV 직렬화와
flush를 측정하며 터미널 렌더링이나 임시 파일 생성 시간은 포함하지 않습니다.
OS 캐시는 비우지 않았으며 특정 속도 배율을 합격 조건으로 사용하지 않았습니다.

## 채택한 기본값과 한계

기본값은 `README.md`에 기록했습니다. 테이블은 디렉터리의 CSV stem으로 등록하고,
API는 로컬 단일 프로세스·SQL 스레드 4개·대기 슬롯 16개·최대 SQL 10,000자를 사용합니다.
오류 코드는 일관된 JSON 구조로 반환하며 입력/SQL 오류는 400, 저장소 오류는 500,
풀 포화와 테이블 사용 불가는 503입니다.

브라우저 호출도 시도했으나 Chrome/Chromium 실행 파일이 없어 수행하지 못했습니다.
실제 HTTP 클라이언트 검증으로 명세의 “브라우저 또는 외부 클라이언트” 기준을 확인했습니다.
언어 서버 연결 도구는 가상환경의 실행 파일을 찾지 못해 동일한 basedpyright의 CLI 검사로 대체했습니다.
실행 환경 이외의 Python 버전·운영체제에서는 테스트하지 않았습니다.
개발 도구 basedpyright는 사용자가 이후 선택에 일반 기본값을 적용하라고 한 지시에 따라
코어의 타입 경계를 검사하기 위해 가상환경에 추가했으며 `requirements-dev.txt`에 기록했습니다.

별도 게이트 검토에서 문서화된 검사 범위를 테스트 코드까지 확장한
`basedpyright mini_dbms tests`는 테스트 코드의 타입 오류 55건을 보고했습니다.
주로 unittest 픽스처의 동적 초기화, 테스트 더블의 타입과 결과 타입 좁히기에 관한 진단입니다.
코어 타입 검사와 193개 실행 테스트는 통과했지만, 테스트 코드 전체의 정적 타입 정비는
수행하지 않았습니다. 진단을 무시하거나 억제하는 설정은 추가하지 않았습니다.

동일 CSV에 대한 다중 프로세스 동시 쓰기, 외부 파일 수정, 강제 종료·전원 장애 복구,
DDL·UPDATE·DELETE·인증·WAL·다중 문장 트랜잭션은 문서상 범위 밖입니다.

## 하위 에이전트 작업 기록

| 역할 | 실제 노출 모델 | 작업 ID | 상태 |
| --- | --- | --- | --- |
| 문서·계약 분석 | DeepSeek V4.1 Flash | `st_01a1e2d5` | 완료 |
| 파서·공통 모델 구현 | Kimi K2.7 Code | `st_01a1e2d6` | 완료 |
| B+ Tree 구현 | Kimi K2.7 Code | `st_01a1e2d7` | 완료 |
| 저장소·카탈로그·실행기 구현 | Kimi K2.7 Code | `st_01a1e2d8` | 완료 |
| API·스레드 풀 구현 | Kimi K2.7 Code | `st_01a1e2d9` | 완료 |
| 최종 게이트 검토 | DeepSeek V4.1 Flash | `st_01a1e2fd` | 완료: APPROVE, 차단 사항 없음 |

주 에이전트가 통합 결함 수정, CLI·서버 진입점·성능 도구, 추가 격리/복구 테스트,
전체 회귀·타입 검사, 실제 서버/CLI QA와 최종 검토를 수행했습니다.
