# View와 blob store

읽기 검증 영수증은 view id별 파일에 보관하며 다른 view의 영수증을
덮어쓰지 않는다. recall 이벤트의 `pinVerified`는 영수증 재사용이면
`receipt`, 전체 검증이면 `full`이다. pin의 같은 세대와 DB 정체성을
검증하며 영수증이 없거나 맞지 않으면 전체 검증을 수행한다.

- `checkout_view.py` — caller가 보관하는 저장소·워크트리 등록 JSON과 Git checkout capture API. 경로·dev/ino, ref·HEAD·index·operation을 재검증하고 view identity를 계산한다. view store가 binding·capture를 검증할 때 쓴다. recall pin과 sync 게시가 이 경로로 checkout을 관측한다. API와 증명 범위는 `docs/development-checkout-capture.md` 참고. 관측 한 번은 checkout을 두 번 읽어 대조하고 git 자식 12개를 띄운다(`--version` 확인은 그중 한 번). macOS의 `/usr/bin/git` shim이 넘기는 실제 git 경로를 그 capture의 fingerprint가 증명했으면 그 capture 안에서는 shim을 거치지 않고 그 경로를 실행한다. 등록(`register_*`)과 fingerprint가 서지 않는 capture는 PATH의 `git`을 쓴다.
- `blob_store.py` / `blob_schema.py` / `blob_values.py` — schema3 view store의 내용 층인 private Git-blob 형식의 wiki admission·불변 body/recipe/profile/set·metadata provenance. 원문 body API는 없다. `put_fingerprint`·`put_attestation`에 넘기는 지문·receipt는 호출자의 증거 주장이다. schema3 sync 게시는 `ingest_attested_card`를 쓴다: 저장소가 카드 frontmatter를 recall의 신뢰 규칙(`legacy_card_evidence`)으로 직접 판정해 통과한 카드에만 `legacy_unbound` 지문의 `legacy_verified` attestation을 붙이고, 현재 HEAD를 과거 compile commit으로 쓰지 않는다. 구조 제약과 의미 검증, API·상한은 `docs/development-blob-candidate.md` 참고.
- `blob_lease.py` / `blob_fill.py` — view store의 private content flock과 불변 complete set 채우기. schema3 sync의 vector fill이 이 경로로 content set을 재사용한다(두 view가 공유하는 content는 한 번만 임베딩). 캐시는 읽기 전용이고 callback은 SQLite transaction 밖에서 실행한다. claim은 협력적 발급 객체·보유 FD 계약이며 운영 owner 권한이나 임의 동일 UID FD 조작에 대한 보안 경계가 아니다. API는 `docs/development-blob-candidate.md` 참고.
- `view_fill.py` / `fill_schema.py` — capture·manifest·recipe/profile에 묶인 durable fill cursor와 불변 대상 metadata. content set 뒤 별도 cursor CAS를 commit하며 완료 재호출은 쓰기·fence·provider가 없다. 계획은 본문 전체 대신 길이 metadata를 보관한다. schema3 sync owner가 view별 vector fill에 쓴다.
- `view_store.py` / `view_reader.py` / `view_schema.py` / `view_values.py` / `view_fts.py` / `view_vectors.py` — schema3 view store의 capture-bound 불변 세대·same-view epoch CAS·세대별 FTS와 pinned lexical·exact reader. 벡터 coverage는 lexical-only 또는 동일 recipe/profile의 전체 eligible member 참조다. seal은 recipe/profile과 member blob을 봉인하고, 벡터 바이트는 set digest와 행 digest로 읽을 때 검증한다(손상된 바이트는 서비스하지 않는다). hybrid는 같은 pin의 lexical 결과를 유지하고 semantic work/deadline만 defer한다. seal과 검색 결과는 구조적 commitment이며 운영 권한을 증명하지 않는다. 운영 권한은 journal의 schema3 endpoint record가 정한다. recall(`reader_dispatch.query_endpoint`), sync 게시(`product_sync_worker`), write-time dedup retrieval(`wiki_compile_retrieval`)이 쓴다. API·상한·실패 의미는 `docs/development-view-candidate.md` 참고.
- `journal_reader.py` — 설치 journal의 reader endpoint 기록과 선택, view pin 및 epoch floor 증명을 담당한다. sync owner의 `resettle_selection`은 설정 또는 vector spec 변경에 맞춰 선택을 재봉인한다.

## 공개 API (`api.py`)

macOS exact semantic 검색은 `view/vector_accel.py`의 지연 로드 Accelerate
커널과 `view/view_accel_search.py`의 대량 읽기를 사용한다. pin의 member만
읽고 모든 행의 모양·유한성·norm, 상위 60장의 set·행 digest를 검증한다.
최종 점수는 기존 Python 함수로 계산한다. 게시 시점에는 전체 set을 검증한다.
`MEMVANE_VECTOR_ACCEL=off`나 커널 부재에서는 모든 set을 검증하는 Python
경로와 기존 float 연산량 상한을 사용한다.

다른 도메인은 `memvane.store.api`의 함수만 쓴다. `open_store`가 돌려준 열린 view store의 공개 메서드(`capture_view`, `head`, `pin_view`, `release`, `prepare_generation`, `publish_generation`, `ingest_attested_card`, `fill_view`, `fill_status`, `search_exact`, `vectors`, `close` 등)는 그대로 부른다. 그 밖에 store의 `_` 멤버나 store 테이블(`store_meta`, `worktrees`, `s_*`, `embedding_sets`, `chunk_sets` 등)이 필요한 읽기·쓰기는 아래 함수로 한다.

`search`·`search_exact`·`search_hybrid`는 경로 제외(`excluded_paths`)와 함께 `excluded_cards`(`[상대 경로, blob sha]` 256쌍 이하)를 받는다. pin한 세대의 행과 쌍으로 대조하므로 같은 경로라도 sha가 다른 카드는 후보로 남고, 제외된 카드는 한도를 쓰지 않으며 exact lane은 그 벡터를 읽지 않는다(지운 적격 카드 수는 `vectorExcluded`). `search_exact`의 `query_vector`는 벡터 하나 또는 1~4개의 목록이고, 목록이면 한 번의 읽기에서 카드별 최고 점수를 낸다.

lexical-first recall은 `search_exact`에 승인된 provider의 `partial_recipe_id`와 `profile_id`를 넘긴다. 같은 pin의 적격 카드 SHA에 해당하는 chunk·embedding set의 개수가 같고 chunk 행의 digest가 머리글과 일치하는 blob만 기존 exact 검색에 참여한다. 게시 seal은 쓰지 않는다.

native lane의 `search_exact`는 store 파일 옆 `vector-cache/`의 vector arena(`memvane/vectorcache`)를 먼저 읽는다. arena가 덮은 set은 매핑한 행에서 점수를 내고, 덮지 못한 set만 store에서 대량으로 읽어 카드별 최고 점수에서 합친다. store 읽기 상한(`MAX_ROWS` 40,000행·`MAX_BYTES` 128MiB)은 그 덮지 못한 행에만 걸리고, 넘으면 `vector_cache_building`(recall의 `deferred_cache_building`, lexical 유지)이다. arena 쪽의 실패(파일 없음·정체성 불일치·digest나 crc가 맞지 않는 레코드·값이 유효하지 않은 행·reader의 예외)는 그 set 또는 전체를 store 읽기로 돌릴 뿐 검색을 실패시키지 않는다. `embedding_set_invalid`는 store에서 읽은 바이트에서만 난다. 상위 후보는 지금처럼 store에서 다시 읽어 채점하고, 그 점수가 arena 점수와 1e-4 넘게 다르면 store 점수를 쓴다. 보고 필드: `vectorSource`(`arena`·`store`·`python`), `vectorMissing`(arena가 답했을 때 store에서 읽은 행 수), `vectorCache`(`ok`·`building`·`absent`·`disabled`·`inconsistent`). `MEMVANE_VECTOR_ACCEL=store`는 arena를 열지 않고(`disabled`) 상한 밖은 지금의 `vector_work_exceeded`다. coverage는 blob 256개씩 묶어 읽는다(`view_accel_search.heads`). Python lane은 질의 벡터 수만큼 work가 늘지 않게 예산이 허락하는 앞쪽 벡터만 쓴다. arena를 쓰는 것은 sync owner이고(`memvane/sync/vector_cache.py`) store는 그 writer에게 살아 있는 set 목록(`arena_sets`)과 검증한 set 바이트(`arena_set`)를 준다.

- `application/store_ops.py` — 수명과 정체성(`observe_binding`: 열거나 만들고 워크트리 하나를 관측한 뒤 닫는다, `open_store`, `store_identity`), store 헤더·워크트리 행 읽기(`read_header`: store 파일을 직접 열어 한 query_only 스냅샷으로 `store_meta`와 워크트리 행을 읽고 `types.STORE_HEADER_KEYS` dict나 None을 돌려준다. `recover=True`는 hot journal을 rollback한다. 파일 정체 대조는 호출자가 앞뒤로 한다), pinned 읽기(`pinned_vector`, `vector_profile`, `eligible_members`, `worktree_wiki_root`), `skipPaths` 판정(`admitted_paths`).
- `application/view_sync.py` — sync owner의 view pass가 쓰는 store 읽기·쓰기(`read_view_baseline`, `head_same`, `compile_evidence_stored`, `linked_worktree_id`, `embedding_sets_missing`, `embedding_coverage`, `fill_plan`, `put_source_fingerprints`). 무엇을 넣고 언제 게시할지와 journal fence는 sync가 정한다.
- 값 함수 — checkout capture(`capture_view`, `capture_matches`), provider vector spec(`provider_spec`, `register_provider_spec`, `qmd_first_pass`, `query_values`, `profile`), blob 값(`regular`, `digest`, `markdown_title`, `provenance_digests`, `relative`).

route(층 2)는 store(층 3)보다 아래 층이라 `index_host_route._store_identity`의 `store_meta` 읽기는 이 api를 쓰지 않는다. 그래서 `store_meta`의 `singleton`(값 1인 한 행)·`incarnation`·`scope_id` 열 이름과 뜻은 route와의 계약이고, store가 바꾸려면 route도 같이 바꾼다.
