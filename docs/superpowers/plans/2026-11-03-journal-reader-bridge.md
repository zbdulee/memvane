# Journal reader bridge 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 승인된 기존 agent 한 명씩 실행하고 부모가 검사를 직렬 소유한다.

**Goal:** 번들 업그레이드가 쓰기를 막는 동안에도 bridge를 로드한 호스트가 독립적으로 증명된 같은 설치의 게시 인덱스를 읽게 한다.

**Architecture:** installer가 첫 fence 전에 v5 journal에 읽기 endpoint·attestation·selection을 함께 봉인한다. 호스트는 검증된 checkout과 endpoint를 수명 잠금 아래 해석하고, 요청별 SQLite snapshot에 현재 게시 세대를 pin한다. PR5의 운영 선택은 v6이고 schema3는 별도 합성 store에서 같은 reader 인터페이스를 입증한다.

**Tech Stack:** Python 3.9+ 표준 라이브러리, SQLite/FTS5, POSIX flock, 기존 shell/Hermes adapter, Python fixture를 감싸는 Node test runner.

**Spec:** `docs/plans/2026-11-01-memvane-db-git-blob-model.md` §§8–14. 기준 릴리스 `6437108`(0.43.37), 이 계획의 코드 기준 `ebde1ea`. 이전 checkpoint의 checkout 경로나 호스트 상태를 실행 입력으로 쓰지 않는다.

## Global Constraints

- bundle/install epoch, reader epoch, data generation, sidecar publish revision은 각각 별도다. route의 기존 generation은 bundle 값이다. PR5는 route v4·store_binding_id를 배포하지 않는다.
- 기본 생성은 journal v4다. v3/v4의 기존 read/CAS·현행 host fallback과 v6 writer 계약을 유지한다. schema3 선택, migration/catch-up, 새 worktree writer는 각각 후속 gate를 갖는다.
- 읽기 증명에는 현재 checkout·private path·scope·store incarnation·봉인된 trust가 모두 필요하다. candidate commitment만으로 owner, writer, 학습, 모델 시작 권한을 발행하지 않는다.
- §14의 같은 프로젝트/common Git dir, cold view만 preparing, committed deletion 전 hold, lexical 유지·점진 backfill, 기존 카드 보존, bridge rollback 조건을 적용한다. GC·unregister·rollback DB 삭제·대체 chunk를 도입하지 않는다.
- 보호 파일 `core/laya_local_embed_provider.py`, `core/laya_local_embed_worker.mjs`, `hooks/hooks-codex.json`의 diff는 0이다. 엔진 subbundle ARTIFACTS 목록·provider equivalence 및 기존 resource admission도 유지한다. engine 단독 import가 full-plugin 전용 새 모듈을 필수로 요구하지 않게 한다.
- 합성 RED/GREEN은 canonical realpath 임시 HOME·state·runtime에서만 실행한다. `F <file...>`는 `npm run test:focused -- <file...>`이다. 부모가 전역 guard·파일 병렬도 1·유한 timeout 아래 직접 실행한다. hold/pressure가 멈춘 검사는 settled 확인 뒤 정상 가드로 재개한다.
- 각 태스크는 지정 파일만 stage한다. 이전 태스크와 같은 파일을 다룰 때는 직렬 인계한다. 코드 없는 assertion/import 실패만으로 동작 결함을 확정하지 않고 해당 base에서 정상 진입점을 통과한 실패를 보관한다.

## Review Focus

1. old cache가 설치 과정에서 사라지고 new cache가 같은 경로에서 재추출되어도 봉인된 old 자료와 독립적으로 승인된 new 실행 사본만 읽기 권한을 얻는다(T1·T6).
2. 검색 시작 중 sidecar 첫 생성·shadow 삭제·exclusion 추가·head 교체가 겹쳐도 한 시점의 결과만 보이며 정상 게시 검색을 빈 성공으로 바꾸지 않는다(T3·T4).
3. 구 writer가 conversion 직전에 journal을 열었거나 수명 잠금 획득 전에 경로가 교체되었더라도 오래된 FD로 쓰거나 다른 install을 읽지 않는다(T1·T2).
4. activate 직후 프로세스 종료, stopped fence, U1 반복 retarget 중에는 검증된 읽기가 계속되고 섞인 metadata와 위조 intent는 거절된다(T6·T7).
5. 호스트가 분류 후 fence를 만나거나 cached companion으로 실행되어도 자신이 읽은 endpoint를 증명하며 production data schema 전환 증거와 설치 manifest를 구분한다(T7·T8).

---

## 구현 경계와 선행 증거

운영 전환 가능 여부는 다음 표로 추적한다. 아래 숫자는 해당 합성 관측이고 PR5 완료 증거가 아니다.

| 항목 | 확인된 범위 | 후속 gate |
|---|---|---|
| GB1 | A settings 공백·policy·폴더·wiki inode 변경 뒤 기존 binding 거절. 재등록은 일반적으로 `candidate_io_failed`, policy 불일치는 `worktree_policy_mismatch`. B 검색 1행·공유 embedding set 1개 유지 | PR6가 changed binding을 복사하기 전 명시적 rebind/거절, PR7 전 기존 view 읽기·policy 전환·내용 재사용 검증. 전체 store 폐기 근거 없음 |
| GB2 | capture/head 32, pin/search/exact 64, hybrid 128 Git spawns. 1카드 writer lock 관측 1회 publish 490.5ms, prepare 715.8/1239.5ms | PR6 writer부터 Git·전체 proof를 짧은 CAS와 분리. PR7 전에 1,800장/10 views p95 view 50ms·lexical 150ms·hybrid 2.5s, provider 2s, CAS 목표 50ms/상한 200ms 측정 |
| GB3 | schema3 rollback journal, 실제 reader 중 writer commit `candidate_busy`, 해제 후 성공. -wal/-shm 거절 | PR6 concurrent shadow writer 전에 호환 형식/sidecar 안전성·snapshot 동시성 해결. PR5 schema3 descriptor에는 rollback 능력만 표시 |
| GB4 | `generation_seal_vectors_invalid` trigger 3회 후 slots/FTS 1→4, 348160→425984B. 기존 head·lexical 동일, 정상 retry는 slot 5 | PR6 migration writer의 **선행 조건**으로 동일 작업의 미봉인 stage 재사용 또는 명시적 유계 admission과 crash/retry 검사. 자동 삭제로 해결하지 않는다 |
| GB5 | 빈 body는 admission에서 `blob_invalid`. 유효 body의 0/1025 chunks·provider 예외·callback 뒤 만료는 2회 모두 cursor 0, 뒤 member 미도달, lexical 유지 | 첫 shadow vector fill 전에 member별 실패·재시도 상한과 후속 member 진행·완전 coverage 증명. PR7 선택 전에 lexical 연속성 확인 |
| GB6 | base 8건 중 4건 RED, `ebde1ea`의 reader/generation/vector 포함 4파일 focused 통과 | T5에서 실제 무결성 reason과 독립 deadline 대조. 통합 full·릴리스는 T8 |

## Task 1. v5 journal의 명시 전환과 독립 CAS

**Files:** Modify `core/laya_install_journal.py`, `core/laya_journal_v4.py`, `hooks/run-hook`, `core/laya_host_response.py`, `hermes_adapter/core_bridge.py`, `scripts/release_coordination.py`. Create `core/laya_journal_reader.py`, `core/laya_bridge_readiness.py`, `test/fixtures/laya-journal-reader-checks.py`, `test/laya-journal-reader.test.mjs`. hook 변경은 v4 readiness만이며 실제 검색 연결은 T7이다. 기존 v4·engine subbundle 검사도 수행한다.

**Consumes:** `inspect_journal(journal_path)`, `host_copies(root=None, home=None)`, `_row(db)`, `verify_selected_route_read_only(decision)`. T1에서 정상 steady selected 설치의 full installer proof로 encapsulated seal을 발급하고 T6에서 old cache 소실 경로를 추가한다.

**Produces:** `seal_current_reader(state: Path, project: Path, *, executing_root: Path) -> InstallerSeal`; `convert_reader_v5(state: Path, project: Path, *, installer: InstallerSeal, readiness: BridgeReadiness) -> ReaderSelection`; `read_selection(journal: Path) -> ReaderSelection`; `cas_reader(journal: Path, *, expected_epoch: int, expected_endpoint: str, target: VerifiedEndpoint, transition_id: str) -> ReaderSelection`; `bridge_preflight(payload: dict, *, host: str, executing_root: Path) -> dict`; `check_bridge_preflight(receipts: list, *, release_commit: str, expected_hosts: frozenset) -> BridgeReadiness`. T1에서 정의하는 불변 내부 타입 `ReaderSelection(epoch, preferred_endpoint, previous_endpoint, transition_id)`, `InstallerSeal`, `BridgeReadiness`, `VerifiedEndpoint`는 실제 proof·파일·DB·host 실행 검증 후에만 발급한다. caller dict/공개 생성자는 발급 경로가 아니다. T6은 같은 InstallerSeal 타입의 old-cache 부재 발급 경로를 추가한다.

- [ ] RED 작성. 새 journal은 계속 v4. active v3/v4 fixture는 명시 변환 후에만 v5이며 중단 지점마다 complete old/new 한쪽이다. bundle CAS가 reader epoch를 움직이지 않고 reader CAS가 bundle epoch·newWriteSeen을 바꾸지 않는지 검사한다. exact replay만 no-op, ABA·다른 install·scope·schema·endpoint digest·동일 transition의 다른 target은 거절한다.
- [ ] RED 확인. `F test/laya-journal-reader.test.mjs test/laya-journal-v4.test.mjs`. 별도 임시 install에 **실제 릴리스 6437108 코드**와 legacy Hermes/이전 id 판독기를 놓고 old CAS, selected sync/compile/upgrade 진입으로 v5를 읽힌다. `BEGIN IMMEDIATE` 전 open된 경쟁 FD도 재현한다. `journal_schema_unknown` 등 기존 거절, 변경 SQL·queued work·provider 0을 확인한다. 현재 코드에 version만 위장한 대역은 증거에서 제외한다.
- [ ] 구현. 활성 fresh-wiki만 대상이다. private install/operation 및 기존 write owner를 quiesce하고 설치 사본의 v5 판독 능력과 알려진 사용 host의 실제 v4 preflight를 함께 검증한 때 explicit conversion한다. v4에서도 기존 등록 hook의 readiness challenge(nonce·host/session·executing root·release commit/manifest)만 처리한다. 이 응답은 인덱스 읽기/writer 권한이 아니다. trusted QA collector가 자신이 시작한 실제 host process tree와 등록 hook 실행을 관측해 private 영수증을 수집한다. check_bridge_preflight 입력은 그 observer 기록의 참조이며 ownership·challenge·실행 provenance·commit을 독립 확인한다. caller가 선언한 dict 필드는 증거가 아니다. 수동 core 실행·caller stdin·모델이 되풀이한 nonce·manifest 존재만으로 발급하지 않는다. 사용 Claude/Codex와 실제 사용으로 등록된 Hermes를 expected_hosts에 넣는다. prepare 뒤 첫 fence 직전에 같은 실행 사본·manifest·nonce·10분 freshness를 재검증하며 만료 시 실제 challenge를 다시 수집한다. 전역 모든 stale session의 부재는 주장하지 않고 구 코드의 실제 기존 exit/reason과 무변경 거절을 검증한다. T8 v5 검색 postflight는 별도 영수증이다. v3는 기존 정확한 v4 전환을 먼저 완료한 뒤 재검증한다. 미지원/확인 불능 host·legacy Hermes는 `reader_hosts_pending`, pending 이전 세대는 기존 recovery부터 끝낸다. v5 입력을 기존 v4 converter가 downgrade하지 않도록 한다.
- [ ] 같은 SQLite transaction에서 정확한 v5 schema·불변 `read_endpoints`·원본 canonical attestation bytes·독립 immutable selection 근거·초기 reader_selection(epoch=1,previous=NULL)·user_version을 기록하고 FULL sync한다. endpoint ID는 정규화한 install/project dev/ino·scope·format/path/DB incarnation·schema capability·checkout policy 및 settings·catalog/binding/sidecar 위치·private parent identity·profile/attestation SHA·creation epoch의 domain-separated digest다. head/gen·cache 경로의 현재 존재는 endpoint identity에 넣지 않는다. immutable row 변경 거절, 실제 table/trigger 정의·참조·digest·단일행 검증. 초기 bundle epoch/row·write flag 보존. T1에서 install ID 기반 stable transition lock을 v4 transaction 전에 준비하며, journal inode 자체를 lock으로 사용하지 않는다. v5 읽기는 validated private parent 안의 install과 selection을 기준으로 새 이미지 inode를 검증한다. v6→v6만 PR5 production CAS 대상으로 허용한다.
- [ ] v5 후속 journal 변경은 stable per-install transition lock 아래 완전한 이전 이미지에서 private sibling SQLite 이미지로 복제→변경 CAS/recheck→commit/close→파일 fsync→원자 replace→부모 fsync한다. 기존 `_cas`/relocate/restore/retarget/mark-write도 같은 v5 writer만 사용한다. 읽기 연결은 한 완성 이미지에서 열고 live rollback recovery용 rw fallback을 호출하지 않는다. 최초 v4→v5 transaction은 기존 inode에서 구 writer의 transaction 안 version 재검사를 보장한다. conversion 중 hot v4 rollback은 installer가 fence 전에 수습한다. 정상 v5 변경마다 reader가 hot journal을 수리해야 하는 설계는 제외한다.
- [ ] GREEN. T1 focused에 fsync 전후 실제 child kill, concurrent CAS·재시도, 임시 파일 위조·크기 상한·파일 누수, v3/v4 stable read/write·하위 호환을 포함한다. conversion 실패는 fence를 시작하지 않는다.
- [ ] Commit `feat(journal): seal reader endpoints and separate reader selection`.


## Task 2. Reader 수명과 실제 store 이동

**Files:** Create `core/laya_reader_lease.py`, `test/fixtures/laya-reader-lease-checks.py`, `test/laya-reader-lease.test.mjs`. Modify `core/laya_state_move.py`, `core/laya_project_dir_move.py`, `core/laya_bundle_upgrade.py`, `core/laya_journal_reader.py`.

**Consumes:** T1 `read_selection(journal)`, `cas_reader(...)`, 기존 state/project move intent·recovery.

**Produces:** `reader_lifetime(install_id: str, *, exclusive: bool) -> ContextManager[int]`; `resolve_endpoint(project: Path, *, action: str, host_root: Path) -> ContextManager[VerifiedEndpoint]`; `stage_move_reader(intent: dict, *, installer: InstallerSeal) -> MoveReadSeal`. MoveReadSeal은 T2의 검증된 내부 타입이다. 검증된 install ID만 받으며 lease가 필요한 파일 경로를 payload/env에서 받지 않는다.

- [ ] RED 작성/관측. `F test/laya-reader-lease.test.mjs test/laya-state-move.test.mjs test/laya-project-dir-move.test.mjs`. 독립 프로세스의 로드된 reader가 있는 상태에서 state rename 및 binding replacement가 defer하고 inode·head·metadata가 바뀌지 않아야 한다. reader 종료/강제 종료 후 move/resume이 완료되어야 한다.
- [ ] 구현. 기존 UID 상태 resolver와 검증된 test runtime 격리 규칙을 사용하는, 이동되는 state/project 밖의 canonical private install registry에 install ID별 안정된 0600 lock을 conversion writer가 미리 생성·fsync한다. reader는 O_RDONLY/no-create, 양쪽 모두 NOFOLLOW·regular·uid/mode/nlink·부모 chain·이름과 FD dev/ino를 flock 뒤 재검사한다. 최초 resolve→SH 획득→journal 재확인→모든 DB close→unlock. T2는 T1의 steady seal을 검증하고 T6이 법적인 fence transition 증명을 추가한다. endpoint CAS 뒤 신규 요청만 새 endpoint로 가며 이전 query는 기존 SH에서 끝난다.
- [ ] mover는 install/operation 뒤 lifetime EX를 비차단 획득한 뒤 journal transition→store 순서로 들어간다. state `_switch` 전체 rename/sidecar rebind와 project `_move_folder/_rebind_catalog/_swap_store`부터 selection 재봉인·fsync/verify까지 포함한다. 충돌 시 `reader_lifetime_busy`를 move deferral로 처리한다. 이전/목표 경로·inode를 durable move intent에 포함하고 journal relocation/rebind와 v6 successor endpoint 선택을 한 v5 commit에 묶는다. crash 후에는 exact move prefix에서만 새 경로를 해석하고 재개한다. 다른 경로를 검색해 발견한 DB는 수락하지 않는다.
- [ ] **이동 중 읽기 재료.** EX만으로 crash 뒤 검색이 복구된다고 보지 않는다. 첫 catalog rebind/binding 교체 전에 writer owner를 제외한 상태에서 이전 catalog/binding/완료 sidecar의 일관된 versioned tuple을 준비·검증·fsync한다. 변경될 원본을 유일한 읽기 재료로 남기지 않는다. 새 metadata와 필요한 sidecar path rebase는 별도 stage에서 준비한다. 복사·hash·긴 rebind는 lifetime EX 밖이며 기존 writer owner 배타는 유지한다. 바뀌지 않는 sidecar는 불변성을 증명해 공유할 수 있다.
- [ ] 첫 물리 변경 전에 journal에 `MoveReadSeal(move_id, retained_endpoint, old_generation, exclusion_epoch, old_tuple_digest, new_tuple_digest, path_aliases, prefix_digest)`와 retained reader 선택을 봉인한다. 이 pin은 이동 transition에만 적용하며 정상 endpoint의 head를 영구 고정하지 않는다. base head가 새 세대로 진전해도 CAS 전 reader는 보존한 old catalog/binding/sidecar와 old generation을 함께 사용한다. 추가 exclusion 변경은 seal을 다시 증명하지 않는 한 금지한다. rename/adopt·state 이동의 old/new 경로·dev/ino·settings/policy와 완료 항목 alias를 exact move prefix로 검증한다. 사라진 경로의 stat을 만들거나 임의 새 root를 수락하지 않는다. sidecar path 재배치는 metadata/body/trust/profile parity와 collision 검증을 요구한다.
- [ ] 최종 물리 게시와 완결된 new tuple 선택만 짧은 EX에서 수행한다. canonical 경로로 stage를 atomic promotion할 때도 retained old tuple은 보존한다. `_move_folder`, catalog 처리, `_swap_store` 직후 `stores`, `published`, 모든 metadata prefix와 journal CAS 직후에 mover를 실제 종료하고 **worker 재개 전** fresh reader로 같은 corpus를 검색한다. CAS 전 retained old, CAS 후 new가 각각 완결되어야 한다. 정상 EX 대기·중단 뒤 lock 해제·rename/adopt 양쪽을 포함한다. 재개 완료를 검색의 선행 조건으로 삼거나 이전 버전을 자동 삭제하지 않는다.
- [ ] restore/retire·abandon·shared cleanup·future replace의 실제 자원 목록을 대조한다. 참조 endpoint 또는 보유 lease의 파일·WAL/SHM을 제거할 경로는 EX와 참조 거절이 필요하다. pointer만 retire하거나 unpublished temp만 버리는 기존 동작은 별도 GC 권한을 얻지 않는다. reader가 먼저 install/operation lock을 기다리는 역순 경로 금지.
- [ ] GREEN. 양쪽 rename 중 실제 child stop/resume·새 inode 경쟁·symlink/hardlink·foreign lock·새 파일 생성 0을 검증한다.
- [ ] Commit `feat(reader): coordinate endpoint lifetime with store moves`.

## Task 3. 여러 v6 DB의 게시 시점과 writer receipt

**Files:** Create `core/laya_reader_snapshot.py`, `test/fixtures/laya-reader-snapshot-checks.py`, `test/laya-reader-snapshot.test.mjs`. Modify `core/laya_incremental_docs.py`, `core/laya_index_a.py`, `core/laya_product_sync_worker.py`, `core/laya_index_capacity.py`, `core/laya_source_catalog.py`. 공통 v6 엔진에는 선택적 경계만 두고 full-plugin 경로에서 gate를 제공한다.

**Consumes:** T2 lifetime SH, 기존 `LayaIndexA.publish`·`exclude`, `GenerationBindingStore.put`, `LayaSourceCatalog._publish`, `publish_document`·`preserve_previous_head`·`retire_missing_documents`.

**Produces:** `capture_v6(endpoint: VerifiedEndpoint, *, deadline: float) -> ContextManager[V6Snapshot]`; `publish_cut(install_id: str, *, writer: bool, deadline: float) -> ContextManager[None]`; `record_publish(db: sqlite3.Connection, *, component: str, before: str, after: str, change_digest: str) -> int`. snapshot은 base/catalog/binding/존재하는 completed sidecar의 RO 연결과 head·seal·scope·profile·exclusion epoch·각 revision을 담는다.

- [ ] RED 작성. 실제 두 connections/processes에서 A→B head publish, 같은 path의 sidecar replacement/removal, 새 excluded hash, sidecar 최초 생성, binding·catalog 진행을 snapshot의 각 open 사이에 끼운다. 결과는 실제 old 또는 new effective view와 일치해야 한다. 정상 검색에서 shadow된 옛 row 재등장·혼합 span·빈 성공을 모두 실패시킨다.
- [ ] RED 관측. `F test/laya-reader-snapshot.test.mjs test/laya-incremental-docs.test.mjs test/laya-product-sync-worker.test.mjs`. 동일한 head에서 sidecar만 바뀐 경우와 crash/replay를 포함해 head 이름만으로 revision을 추정하는 구현을 검출한다.
- [ ] 구현. **PR5 writer가** v5 설치에서 query에 보이는 변경과 같은 component transaction 안에 증가 revision과 durable receipt(이전/이후 head 또는 sidecar revision·component identity·change digest)를 기록한다. base는 기존 `schema_meta`에 component별 prefix와 20자리 0-padding revision을 붙인 새 key를 INSERT한다. 동일 writer transaction에서 PK prefix range의 DESC LIMIT 1로 직전 receipt를 읽어 다음 revision을 결정한다. 값은 불변 canonical receipt다. UPDATE/DELETE/기존 key 재삽입 금지 trigger와 v6 static schema를 유지한다. sidecar는 writer가 확장한 private append-only receipt 표를 쓴다. revision 0의 실제 absence/empty와 기존 completed 데이터를 구분하고 최초 문서의 receipt와 문서 공개는 같은 commit이다. 신규 v5 baseline은 이전 발행물을 기준 revision 0으로 검증해 봉인한다. sidecar 미생성→생성은 writer gate에서 처리한다. 같은 데이터 replay/rollback은 revision 불변이다. PR6이 migration ID·target cursor·cross-store ACK 및 누락 시 full mapping 비교를 소유한다.
- [ ] head CAS/exclusion과 sidecar insert/update/delete/preserve의 실제 mutation 모든 경로를 위 계약에 연결한다. binding·catalog는 게시된 binding digest·scope/incarnation·scan seal과 실제 snapshot의 identity로 묶는다. 기존 writer 회수 전에는 coordinated publication 가능으로 표시하지 않는다. 이전 release의 이미 시작된 publish는 기존 owner/install/operation 배타 아래 먼저 마무리시킨다.
- [ ] **공통시점 선택.** 검증된 파일마다 snapshot 연결과 독립된 감시 연결을 열고, 먼저 모든 감시 연결의 autocommit `PRAGMA data_version`과 named identity/absence를 수집한다. 같은 파일별 snapshot 연결에서 `BEGIN` 후 실제 SELECT로 모든 read snapshot을 고정한다. 모든 pin 이후 각 감시 연결에서 *그 연결 자신이* pin 전후 본 data_version/identity를 비교한다(연결 간 숫자 비교 금지). 마지막 pin 후에도 모두 같으면 모든 snapshot이 동시에 존재한 시점이 있다. head↔seal↔binding↔catalog 식별과 sidecar meta/profile·revision·base exclusions를 그 snapshot에서 검증한다. 불일치하면 모두 닫고 한 번 재시도한다.
- [ ] 경합이 지속되면 남은 예산을 확보해 미리 생성한 installation publish-cut SH 아래 한 번 고정한다. bridge writer는 위 자원들의 *가시적 게시/최초 파일 등장* 때만 EX를 잡고, query는 모든 SELECT가 snapshot을 잡자마자 cut을 해제한다. publisher는 EX를 SQL write transaction보다 먼저 잡고, return 전 해제한다. scan·hash·모델은 밖에서 수행한다. 짧은 cut에서 변경되는 published catalog/binding 부분과 긴 비공개 stage를 구분한다. 장시간 EX 보유/매회 전량 재검증을 동시성 해결책으로 삼지 않는다. cut 보유자가 중단되면 안정된 old DB snapshot을 재확인하는 optimistic 경로도 열어 둔다. 유효한 pinned snapshot은 이후 head가 진전해도 반환한다. 증명 실패는 `reader_snapshot_changed` 또는 원래 손상 코드이며 empty·다른 view로 바꾸지 않는다.
- [ ] **물리 연결 경계.** 안정된 UID registry에 component별 `physical_window(endpoint, component, *, exclusive: bool, deadline: float)`와 `mutation_window(endpoint, component, *, exclusive: bool, deadline: float)`를 writer가 미리 만든다. reader는 WAL/SHM 존재 검사 **전** physical SH를 잡고 SQLite connection close까지 보유한다. 모든 참여 writer의 open/close·checkpoint/reset·모드 변경은 physical EX를 거친다. close는 SQL transaction·publish-cut·mutation을 먼저 해제하고 EX를 기다린다. 대기 중 connection 소유를 보존하며 GC/finalizer로 몰래 닫거나 종료 완료로 기록하지 않는다. 모든 실제 writer 경로와 구 owner quiesce를 확인하지 못하면 이 능력을 발급하지 않는다.
- [ ] 순서는 lifetime → component ID 순 mutation(필요 시) → component ID 순 physical → publish-cut이다. writer open은 transaction 전 짧은 physical EX를 마친 뒤 해제한다. 모든 write BEGIN은 mutation SH, 가시적 publish는 추가 publish-cut EX를 잡는다. close는 SQL/논리 잠금 해제 뒤 별도의 physical EX에서 한다. checkpoint/reset은 mutation EX→physical EX 순서이며 reader가 남으면 유한 defer한다. reader fallback은 같은 순서로 physical SH 뒤 짧게 publish-cut SH를 사용한다.
- [ ] WAL은 실제 committed WAL까지 `mode=ro`·`query_only=ON`으로 읽고 content DB의 rw/create·DDL·DML·checkpoint·모드 전환은 없다. 기존 SHM에 대한 SQLite 자체 동시성 접근은 application DB 쓰기와 구분하되 **새 sidecar 생성은 금지**한다. 검사 직후 마지막 writer close/reset barrier에서도 생성 0과 같은 게시 결과를 확인한다. WAL/SHM이 모두 없으면 모든 writer의 mutation EX와 physical SH를 **연결 종료까지** 보유하고 clean checkpoint·bytes 불변을 증명한 경우만 `mode=ro&immutable=1`을 허용한다. 동시 쓰기 가능성·미참여 writer가 있으면 immutable을 거절하며 live WAL을 무시하지 않는다. reader 중 commit→close, 최초 WAL 생성, writer 비정상 종료와 rollback binding snapshot도 실제 대조군에 넣는다.
- [ ] GREEN. T3 focused와 `test/laya-index-capacity.test.mjs`, `test/laya-source-catalog.test.mjs`에서 실제 barrier·rollback·WAL 미체크포인트 데이터·연속 publisher·owner 회수·bounded latency를 확인한다. writer receipt와 실제 commit 일치 여부를 별도 연결에서 비교한다.
- [ ] Commit `feat(reader): bind published v6 snapshots and revisions`.

## Task 4. 쓰지 않는 v6 reader와 기존 검색 동등성

**Files:** Create `core/laya_v6_reader.py`, `test/fixtures/laya-v6-reader-checks.py`, `test/laya-v6-reader.test.mjs`. Modify `core/laya_index_a.py`, `core/laya_index_vector.py`, `core/laya_index_capacity.py`, `core/laya_source_catalog.py`, `core/laya_incremental_docs_query.py`, `core/laya_host_response.py`.

**Consumes:** T3 `capture_v6(endpoint, *, deadline)`; 기존 `query_selected_v2`, `CandidateQuery.verify_indexed_rows`, `verify_generation_seal`, `_verified_wiki_row`, sidecar shadow/fusion/returned-row 검사.

**Produces:** `query_v6_snapshot(snapshot: V6Snapshot, *, prompt: str, decision: dict, provider: Optional[Callable], deadline: float) -> ReaderResult`; `query_incremental_snapshot(prompt: str, decision: dict, proof: dict, provider: Optional[Callable], *, base: sqlite3.Connection, side: Optional[sqlite3.Connection], deadline: float) -> tuple`. `ReaderResult(store_format: str, endpoint_id: str, reader_epoch: int, data_generation: Optional[str], rows: list[dict], semantic_status: str)`를 정의하며 검증된 span/body evidence는 rows에 보존한다.

- [ ] RED 작성/관측. `F test/laya-v6-reader.test.mjs test/laya-incremental-docs.test.mjs test/laya-index-vector.test.mjs`. 읽기 전후 pins·journal/DB 내용·파일명·mode·schema·newWriteSeen 비교, read SQL authorizer와 파일 open 관측으로 content DB의 rw/create·DDL/DML·체크포인트·raw/wiki 본문 reread를 검출한다. 기존 SHM의 SQLite 동시성 접근과 application write를 구분하고 새 sidecar 생성은 별도로 검출한다. 단순 query_only 뒤 constructor write를 놓치지 않는다.
- [ ] 구현. writable `LayaIndexA.__init__`, DB pin/release, capacity/catalog/binding 생성자, nested BEGIN을 거치지 않는다. 기존 검증·rank SQL을 caller-owned 연결과 검증된 gen에 작동하는 공통 내부 함수로 추출한다. 메모리 token은 snapshot/connection·scope·gen·rev_epoch에 귀속시키고 release/close 후 재사용을 거절한다. 기존 writer용 token/API는 그대로 둔다. sidecar wrapper의 기존 호출은 유지하면서 bridge는 `query_incremental_snapshot`에 **같은 base와 side**를 넘긴다.
- [ ] capacity가 있으면 pinned binding·root dev/ino·catalog·FTS/seal·exclusions·profile·returned index bytes를 재검증한다. wiki-only scope·skipPaths·source trust/spans·한글/CJK·exact episode·shadow-before-LIMIT·warm hybrid 결과를 기존 published-query corpus와 대조한다. warm peer/recipe 증명 실패면 같은 pin의 lexical을 유지한다. 읽기 경로에서 selector 학습·selector heal·sync offer·cold start를 호출하지 않는다.
- [ ] GREEN. 0개 completed sidecar, sidecar에만 있는 결과, 제외된 shadow와 base에 같은 hash가 있는 경우, capacity 누락/변조, 프롬프트 도중 publish를 각각 구분한다. 실제 기존 공개 검색과 같은 정렬·body evidence/span을 요구한다.
- [ ] Commit `feat(reader): serve v6 recall from read-only in-memory pins`.

## Task 5. schema3 판독 capability와 selection 차단

**Files:** Create `core/laya_reader_dispatch.py`, `test/fixtures/laya-reader-capabilities-checks.py`, `test/laya-reader-capabilities.test.mjs`. Modify `core/laya_blob_store.py`, `core/laya_view_store.py`, `test/fixtures/laya-checkout-view-checks.py`.

**Consumes:** T4 `query_v6_snapshot(...)`; schema3 `ViewStore.pin_view(handle)`, `search_hybrid(pin, fts_query, query_vector, limit, *, ...)`, `release(pin)`.

**Produces:** `reader_capabilities() -> frozenset[str]` with `v6-ro-v1`, `schema3-ro-v1`; `ViewStore.open_reader(path, *, repository, scope_id) -> ViewStore`; `query_endpoint(endpoint: VerifiedEndpoint, *, checkout: Path, prompt: str, query_vector: Optional[list], deadline: float) -> ReaderResult`. 입력 vector도 approved profile과 증명한 caller 경계에서만 받는다. schema3 descriptor는 실제 candidate 파일 검증으로만 발급하며 production CAS는 그 store_format을 거절한다.

- [ ] RED 작성/관측. `F test/laya-reader-capabilities.test.mjs test/laya-view-reader.test.mjs test/laya-view-review-regressions.test.mjs`. 합성 형식별 endpoint로 capture/pin/lexical/exact/hybrid/close를 실제로 통과시키고 unknown schema·다른 repo·다른 checkout·위조 binding·schema3 production 선택을 거절시킨다.
- [ ] 구현. schema3 자체 reader-only factory는 첫 ro preflight 후 rw reopen을 하지 않고, capture/pin의 읽기 단계만 허용한다. `_session(write=True)`와 쓰기 side API는 `reader_write_forbidden`, read query_only는 끝까지 ON이다. schema3의 rollback/sidecar·동일 capture 검증·GB6 reason 보존·commitment-only 표시는 유지한다. live selector/owner와 candidate 간 import는 단 하나의 명명된 reader adapter에서 필요한 read API만 허용한다. 기존 격리 검사를 callable 경로별로 보강해 operational migration/write/import 우회를 막는다.
- [ ] GREEN. shadow fixture만 schema3 descriptor를 internal 검증기로 발급한다. hook/route/journal override·환경변수·public CLI로 생성할 수 없어야 한다. `cas_reader`가 유효한 schema3 store도 `reader_format_not_selectable`로 거절하는지 검사한다.
- [ ] Commit `feat(reader): verify dual-format read capability with v6-only selection`.

## Task 6. Installer bootstrap와 fence에서의 독립 증명

**Files:** Modify `core/laya_active_upgrade.py`, `core/laya_bundle_upgrade.py`, `core/laya_install_journal.py`, `core/laya_journal_reader.py`, `core/laya_reader_lease.py`. Test `test/fixtures/laya-active-upgrade-checks.py`, `test/fixtures/laya-bundle-auto-upgrade-checks.py`, `test/fixtures/laya-journal-reader-checks.py`.

**Consumes:** `_old_state(project, state, journal, ledger=None, *, prior_intent=None)`, `_prepare(...)`, actual `review_local_provider`→`reattest`→`adopt_host_loaded_plugin`, T1 conversion/CAS·T2 lifetime·T4 v6 proof.

**Produces:** `seal_upgrade_reader(project: Path, state: Path, *, executing_root: Path) -> InstallerSeal`; `prove_reader_context(project: Path, state: Path, *, executing_root: Path, selection: ReaderSelection) -> VerifiedEndpoint`. 경로는 trusted registry·실제 executing bundle에서 도출하고 caller가 건넨 proof dict·provider SHA만으로 발급하지 않는다.

- [ ] RED 작성/관측. `F test/laya-active-upgrade.test.mjs test/laya-bundle-auto-upgrade.test.mjs test/laya-journal-reader.test.mjs`. old cache를 실제 임시 tree에서 삭제하거나 같은 경로에 새 inode로 재추출한다. new installer가 정상 adoption 가능한 경우, unrelated new commit·provider byte 변경·old marker/attestation/backup 변조를 각각 분리한다.
- [ ] 구현. steady/full-proof conversion과 old-cache 부재 bootstrap 두 입구를 둔다. 후자는 active journal의 selected identity·route/attestation SHA·기존 sealed FID rows, active ledger·marker·reviewed route·build identity·scope/store counts·current checkout을 서로 대조한다. 기존 target installer review/equivalence/full adoption와 host-layout/commit proof를 **별도** 검증한다. old full proof나 사라진 파일의 현재 stat을 요구하거나 만들어 내지 않는다. verified old read seal은 writer proof로 승격할 수 없다.
- [ ] 순서는 installer candidate 증명→기존 `_prepare` durable backup/intent→v5 endpoint·attestation·selection 및 intent commitment commit/fsync→재확인→최초 `_fence`다. restore 전용 old proof는 여전히 필요하다. 이미 실행 중인 pre-bridge v3/v4 pending은 기존 recovery로 정착시킨 뒤 전환한다. stale v4의 upgrade intent를 새 스키마 tuple인 양 해석하지 않는다.
- [ ] v1은 old/target·journalBefore·committed intent digest에서 **실제 전환 순서의 prefix**를 생성한다. active old→fenced/wait→stage pending→attestation→reviewed route→project route→ledger→marker→activate→ledger active 순서다. v2 normalize/switch는 기존 `_validate_recovery/_recovery_state`의 bounded descriptor, journal epoch/selected/pending, before/after prefix·backup/counts·newWriteSeen을 사용한다. retarget 때 원래 read seal을 유지하되 새 descriptor digest와 직전/다음 legal tuple을 fsync/CAS로 연결한다. 무관한 old/new 조합이나 historical digest allowlist는 `reader_transition_invalid`다.
- [ ] 정상 비-fence 상태의 current executing owner/companion, fence 중 old/new approved executing host를 각각 증명한다. 정착 시 같은 store/profile을 유지한 provider reattestation은 v5 bundle activation과 같은 commit에 authenticated successor proof를 기록한다. endpoint 변동이 없으면 reader epoch 불변이다. intent archive 뒤에도 settled proof는 journal에서 검증된다. store/path/incarnation 변경은 T2의 별도 endpoint CAS로만 처리한다. read-only warm peer에 쓰는 provider data는 현재 증명한 동일 recipe에서만 도출한다.
- [ ] GREEN. pre-fence·socket wait·모든 metadata prefix·activate 직후·unfence 전후·stopped/future mtime·normalize-descriptor/route·switch-descriptor/cas/metadata-0..4/activate에서 child를 실제 중단해 동시 fresh reader를 돌린다. 동일 게시 corpus의 답과 writer 거절을 확인하고 `finish_activated`/restore/forward-only/repeated retarget 이후에도 검사한다. tamper, replay, reordered journal, QMD-origin, older host, legacy Hermes·companion·pending 복구는 기존 제한대로여야 한다.
- [ ] Commit `feat(upgrade): retain proven readers across bundle fences`.

## Task 7. 실제 host 분류·응답·전환 재시도

**Files:** Modify `core/laya_index_host_route.py`, `core/laya_fresh_wiki_origin.py`, `core/laya_host_response.py`, `hooks/run-hook`, `hermes_adapter/core_bridge.py`. Test `test/fixtures/laya-host-response-checks.py`, `test/hermes-plugin-adapter.test.mjs`, `test/hook-structure.test.mjs`; Create `test/laya-reader-bridge-hosts.test.mjs`.

**Consumes:** T2 `resolve_endpoint(...)`·T6 `prove_reader_context(...)`·T5 `query_endpoint(...)`; 기존 host payload 렌더링·SessionStart 경고·편집 gate·observer 계약.

**Produces:** `serve_endpoint_recall(host: str, payload: dict, *, cwd: str) -> dict`. classify-hook 새 출력 `reader`는 recall 전용 dispatch 힌트다. authority는 core entry가 실제 payload cwd를 canonicalize한 뒤 재증명한다.

- [ ] RED 작성/관측. `F test/laya-reader-bridge-hosts.test.mjs test/laya-host-response.test.mjs test/hermes-plugin-adapter.test.mjs test/hook-structure.test.mjs`. 실제 shell dispatcher→standalone Python과 Hermes register/pre_llm→classify subprocess→core serve 양쪽을 사용한다. 분류 시 fence, 분류 직후 fence, query/retry/catch 직전 전환, stopped fence에서 corpus sentinel과 host별 JSON·exit를 확인한다.
- [ ] 구현. v5 bridge recall을 `settle_decision/request_recovery`의 wait/wake보다 먼저 분기한다. `select_route_settled/select_hook_payload`·CLI classify·shell case·core main·`_serve_selected_recall/_recall_retry_decision`·예외의 `_upgrade_recall_notice`까지 같은 reader 증명을 재사용한다. in-flight pin은 lifetime 안에서 완료하고, pin 전 route 경쟁은 once re-resolve한다. 정상 current의 기존 full-proof selected 인퍼런스/학습·writer 경로를 유지한다. 분류는 v5라는 사실만으로 모든 healthy recall을 fence handler로 강제하지 않으며, reader-only는 증명된 fence/drift/기존 read-only 경계에 적용한다. fence·drift 읽기만 read-only이고 실패 시 정상 empty·legacy/main fallback으로 변환하지 않는다.
- [ ] Hermes `_laya_route_state/recall_context`가 새 상태를 보존하고 새 handler를 호출하게 한다. pre-edit gate·post-tool observer·SessionStart 복구/경고는 기존 action proof로 분리한다. linked worktree의 기존 v6 main-read 표기를 유지하며 자체 published view receipt를 만들지 않는다. 실제 Gemini hook 설치·host parity는 주장하지 않는다.
- [ ] GREEN. 두 shell host와 Hermes에서 동일 증명 corpus·cold lexical·warm-only semantic 및 원래의 거절을 단언한다. v5 fence recall에서 recovery request/sync/index/compile wake·learning/selector-heal·provider start·`cas_mark_new_write` 호출 0. healthy v6 normal owner recall, worktree·preview·blocked·legacy 결과와 non-recall actions도 비교한다.
- [ ] Commit `feat(hooks): serve verified recall during writer fences`.

## Task 8. 설치 capability와 실제 release 증거

**Files:** Modify `scripts/release_coordination.py`, `test/fixtures/release-coordination-checks.py`, `test/plugin-manifest.test.mjs`, `AGENTS.md`, `CLAUDE.md`, `docs/development-view-candidate.md`, `REPORT.md`. Release metadata는 `package.json`, `plugin.json`, `plugin.yaml`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` 7개다.

**Consumes:** T1의 실제 v4 readiness preflight와 T1~7 실제 entrypoint, `reader_capabilities()`, 기존 `PreparedWorker.finish(self)`의 owner/kind/terminal 검사와 release QA. **Produces:** `check_loaded_bridge(receipts: list, *, release_commit: str, expected_hosts: frozenset) -> dict`. receipt는 배포 검증 수집기가 실제 host 호출을 관측해 기록하고 reader에게 쓰기 권한을 주지 않는다.

- [ ] RED 작성/관측. `F test/release-coordination.test.mjs test/plugin-manifest.test.mjs`. manifest·installed-cache 존재만 제출, 같은 host 2건, 이전 commit/동일 경로 재추출, companion을 owner로 오인, synthetic direct-core subprocess를 actual host로 제출하는 경우를 거절한다.
- [ ] 구현. T1 preflight와 별도인 postflight collector가 각 Claude/Codex 실제 등록 hook의 v5 검색을 실제 host 요청으로 실행한 증거에 fresh challenge, host/session·실제 dispatcher/loaded plugin root·release commit/manifest·owner/companion proof·endpoint ID/reader epoch·format·served generation·반환 sentinel 결과를 연결한다. private QA capture와 기존 진단만 쓰고 사용자의 prompt/카드 원문이나 임의 receipt payload를 저장하지 않는다. 안전한 QA 설치에서 v4 preflight와 conversion 후 실제 v5 reader branch를 각각 태워야 한다. 실제 host 테스트와 review/advisor 호출을 구분하며 synthetic corpus·유한 출력/호출로 한정한다. host 실행이 불가능하면 missing receipt를 보고하고 release gate를 열지 않는다.
- [ ] 7개 manifest를 base .37보다 높은 같은 release version으로 올린다(경쟁 bump가 없으면 0.43.38). `REPORT.md`에 실행한 RED/GREEN·GB 미해결·PR6/7 제약·미측정 상태를 기록한다. 개발 문서는 실제 capability, fence 동작, sidecar/rollback/권한 범위를 현재형으로 설명한다.
- [ ] GREEN. 부모가 focused 인접 suite, `npm run test:guard-unit`, canonical 임시 HOME `npm test`를 순차 완료한다. exit·passed·settled와 정확한 pass/fail/skip 수를 기록한다. 보호3 diff 0·7버전 일치·v4 default·운영 schema3 선택 0을 확인한다.
- [ ] Commit `feat(release): verify loaded reader bridge on both hosts`. 정확한 최종 HEAD에서 모든 검사 종료 후 clean detached 독립 Astra high 읽기 전용 리뷰와 **실제 동작하는 Agy** 리뷰를 직렬 실행한다. 모델/설정/exit·판정·blocking ID를 기록한다. 중간 수정이 있으면 최종 HEAD에 필요한 검사/리뷰를 다시 묶는다. 별도 최종 CI의 정확한 commit과 성공 check를 확인한다.
- [ ] 승인된 기존 release 절차로 설치·호스트 전환 후 실제 9 owner + 9 companion, proof 18, strict recall 18, selected runtime·release resident/embed·no-pycache와 Claude/Codex reader entry 영수증을 별도로 검증한다. 합성 fence crash 시험과 실제 운영 receipt를 구분한다. full/CI/리뷰/운영 어느 하나라도 미충족이면 그 상태 그대로 보고한다. migration parity·schema3 selection·PR7 성능/reader-ready는 PR5 완료로 표시하지 않는다.
