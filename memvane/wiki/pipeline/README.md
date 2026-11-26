# Wiki Pipeline

`.memvane/compile/wiki-pipeline.sqlite3` 단일 SQLite 원장, 단일 rename 카드 게시, 1회성 1.x → 2.0 마이그레이션을 소유한다.

- **단일 원장 (`store.py`, `schema.py`)**: `compile_jobs`, `verify_jobs`, `staged_cards`, `refresh_events`, `audit_events`, `cooldowns`, `pipeline_fences`, `publish_txns` 테이블을 트랜잭션(`BEGIN IMMEDIATE`)으로 관리한다. `staged_cards`는 `WHERE state IN ('reserved', 'held', 'manual') AND target_path = ?` 인덱스 조회로 `HOLD_SCAN_LIMIT` 없이 격리하고, 64 MiB(`STAGE_QUOTA_BYTES`) blob 합계 쿼타를 단일 트랜잭션에서 강제한다. 읽기 전용 연결은 `errors._query_only_uri`와 `PRAGMA query_only=ON`을 사용한다.
- **단일 rename 게시 (`publish.py`)**: `.publish-<txid>.tmp`(`O_CREAT|O_EXCL|O_NOFOLLOW`, `fsync`) 준비 → `publish_txns(state='publishing')` 커밋 → `.card-write.lock` → `.topical-publish.lock` → live 카드 `expected_target_sha` CAS → `rename_noreplace`/`os.replace` + `fsync(dir)` → `published` 커밋 순서로 실행한다. `recover_publishes`는 중단 시 temp inode·SHA와 live SHA를 대조해 roll-forward, `conflict_user_edited` roll-back, 또는 `published_user_edited`로 결정적 복구한다. 영속 비교는 `st_dev` 없이 `(st_ino, sha256)`만 사용한다.
- **1회성 1.x → 2.0 마이그레이션 (`migrate.py`)**: 기존 `.memvane/compile/*.jsonl`, `*.claimed.*`, `source-refresh-settled.json`, 쿨다운 파일, `.handoff/v1/*`, 위키 숨김 파일을 원장으로 이관하고 정리한다. 누수된 `copy-quota/allocated-*`는 무시해 `staged_cards` 실제 합계로 재산정하며, `.memvane/compile/retired-wiki/`는 보존한다.
