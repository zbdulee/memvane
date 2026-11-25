# Wiki 입력

편집 및 source discovery 결과를 compile 입력으로 전달한다.

구현: `external_edits.py`, `posttool.py`, `source_scan.py`, `source_queue.py`, `wiki_backfill.py`, `wiki_compile_enqueue.py`, `wiki_discovery.py`, `wiki_source_intent.py`.

`source_scan.py`는 수집 root의 상한 있는 스캔과, sync 게시·설치 finalize가 함께 쓰는 원문 관측(`schema3_wiki_cards`: wiki root의 카드 경로, `schema3_sources`: raw root의 경로별 해시·길이·추적 상태)을 맡는다.

새 source 작업은 프로젝트의 private 큐에 둔다. 구 worker가 새 편집을 회수해 보이는 카드에 쓰는 경로는 열지 않는다. 같은 설치·컬렉션·경로·trigger의 편집은 claim 전 source 큐의 기존 위치에서 최신 행으로 교체한다. sync 행은 내용 해시로, PostToolUse 행은 원문 갱신 이벤트 ID로 재전송을 식별한다. 삭제·공유 카드 대기·initial backfill은 같은 작업의 재전송 토큰까지 일치할 때 합친다. 강제 재컴파일 행에는 상위 작업 ID가 없어 같은 내용이 여러 번 들어와도 횟수를 보존한다. 생산자는 원본과 준비한 이미지의 재검증에 합계 8 MiB, 4,096행, 행당 64 KiB까지 읽는다. 상한을 넘거나 마지막 LF가 없으면 원본을 보존하고 적재 실패를 반환한다.

source 큐의 새 이미지는 같은 부모 디렉터리에서 작성·동기화하고 길이와 SHA-256을 다시 읽어 확인한다. 기존 큐 잠금을 즉시 얻고 원본 파일의 신원이 일치하면 이미지를 한 번 교체하고 부모 디렉터리를 동기화한다. 다른 설치의 행과 식별할 수 없는 완전한 행은 원래 바이트를 유지한다.

- `wiki_discovery.drain_after_sync`는 discovery lock 아래에서 `unresolved_pending_refreshes_strict`를 락 없이 읽어 unresolved pending refresh가 있는 비삭제 경로의 중복 `post_sync_source` 적재를 건너뛰고 커서만 갱신한다(`strict` 읽기 실패 시에는 기존대로 적재).
- `wiki_compile_enqueue.enqueue_payload(..., pending_only=True, selected_route=...)`는 훅 경로에서 큐 파일과 `sync_owner`를 잡지 않고 `0.15s` non-blocking lock 상한으로 `record_pending_refresh(..., compact=False)`만 남긴다.
