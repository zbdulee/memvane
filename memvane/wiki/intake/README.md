# Wiki 입력

편집 및 source discovery 결과를 compile 입력으로 전달한다.

구현: `external_edits.py`, `posttool.py`, `source_scan.py`, `wiki_backfill.py`, `wiki_compile_enqueue.py`, `wiki_discovery.py`, `wiki_source_intent.py`.

`source_scan.py`는 수집 root의 상한 있는 스캔과, sync 게시·설치 finalize가 함께 쓰는 원문 관측(`schema3_wiki_cards`: wiki root의 카드 경로, `schema3_sources`: raw root의 경로별 해시·길이·추적 상태)을 맡는다.

같은 설치·컬렉션·경로·trigger의 편집은 claim 전 source 큐의 기존 위치에서 최신 행으로 교체한다. sync 행은 내용 해시로, PostToolUse 행은 원문 갱신 이벤트 ID로 재전송을 식별한다. 삭제·강제 재컴파일·공유 카드 대기·initial backfill 작업은 각각 구분한다. 큐 읽기 상한은 8 MiB이며 상한 밖에서는 새 작업을 추가한다.

같은 작업의 편집을 합칠 때 마지막 편집 정보에 미완료 시도의 `partialCompile`과 worker가 읽은 원문 SHA를 보존한다. worker는 claim 안에서도 같은 설치·작업 키로 행을 합친다.
