# Wiki 작업 공간

compile 경로, 작업 원장과 mutation lock을 다룬다.

구현: `compile_paths.py`, `ledger_queue.py`, `queue.py`, `queue_paths.py`, `scan_state.py`, `wiki_activity_log.py`, `wiki_collection.py`, `wiki_mutation_lock.py`, `wiki_selected_laya.py`.

source·verify 큐는 `wiki-pipeline.sqlite3`의 `compile_jobs`·`verify_jobs` 행이다. 큐 경로(`source-queue.jsonl`·`verify-queue.jsonl`)는 호출자가 넘기는 이름일 뿐 그 자리에 파일을 쓰지 않는다. `ledger_queue.claim`은 죽은 claim을 되돌린 뒤 queued 행을 전부 claim하고, worker는 끝내지 못한 행을 `finish`로 돌려준다. 재시도 한도를 넘은 행은 discard 기록 뒤에만 `dropped`가 된다. idle 판정(`queue_busy`)은 queued·claimed 행을 센다. dedup 쌍 큐는 아직 텍스트 큐로 `queue.py`의 파일 claim을 쓴다.
