# Wiki 작업 공간

compile 경로, 작업 원장과 mutation lock을 다룬다.

구현: `compile_paths.py`, `manifest_ledger.py`, `scan_state.py`, `wiki_activity_log.py`, `wiki_collection.py`, `wiki_mutation_lock.py`, `wiki_selected_laya.py`.

source·verify worker는 claim을 읽은 inode·prefix 지문과 미처리 행으로 반환을 확정한다. 큐 잠금 아래 `.handoff/v1/returns/`가 원행과 게시 준비 단계를 보관하고, 공용 큐와 같은 디렉터리의 고유 swap을 rename한다. 원행이 private에만 남는 단계가 있어 이 디렉터리는 재생 가능한 작업 데이터다. 새 worker와 idle 판정이 같은 복구 절차를 사용한다.
