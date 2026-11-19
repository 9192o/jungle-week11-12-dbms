"""mini-DBMS API 서버를 articles 테이블 하나만 등록해 띄운다.

`python -m mini_dbms.server --tables <dir>`은 디렉터리의 모든 CSV를 테이블로 읽는데, mini-Test에는
타입 헤더가 없는 full-replay.csv가 함께 있어 시작에 실패한다. 그래서 mini-DBMS가 공개한
`create_app(..., tables=...)`로 등록할 파일을 직접 지정한다. mini-DBMS 코드는 수정하지 않는다.

실행: python dbms_server.py <mini-DBMS 경로> <테이블 디렉터리> <포트>
"""

import sys

import uvicorn

dbms_dir, table_dir, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, dbms_dir)

from mini_dbms.api import create_app  # noqa: E402

uvicorn.run(
    create_app(table_dir, tables={"articles": "articles.csv"}),
    host="127.0.0.1",
    port=port,
    log_level="warning",
)
