# Wiki 입력

편집 및 source discovery 결과를 compile 입력으로 전달한다.

구현: `external_edits.py`, `posttool.py`, `source_scan.py`, `wiki_backfill.py`, `wiki_compile_enqueue.py`, `wiki_discovery.py`, `wiki_source_intent.py`.

`source_scan.py`는 수집 root의 상한 있는 스캔과, sync 게시·설치 finalize가 함께 쓰는 원문 관측(`schema3_wiki_cards`: wiki root의 카드 경로, `schema3_sources`: raw root의 경로별 해시·길이·추적 상태)을 맡는다.
