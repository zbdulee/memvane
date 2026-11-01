# MemVane DB의 Git blob·checkout view 모델

상태: 설계 제안. 사용자 확정 모델(2026-10-27)을 기준으로 하며 제품 구현은 포함하지 않는다.
코드 근거: 이 브랜치의 `b1106b2`, MemVane 0.43.19. 아래의 **현재**는 이 코드의 동작이고,
**목표**와 **제안**은 구현 계약이다. 성능 수치는 실측 결과가 아니라 검증할 예산이다.

## 1. 확정 조건과 설계 선택

사용자가 확정한 조건:

- 위키 카드의 내용 SHA를 키로 본문·chunk·vector를 공유한다. 브랜치·워크트리가 같거나
  달라도 내용이 같으면 같은 내용을 저장하고 임베딩을 재사용한다.
- raw 원문은 디스크/Git에 둔다. DB에는 경로, SHA, 브랜치, 기록 시 HEAD, 워크트리 지문만
  저장한다. raw 본문·chunk·vector·FTS를 DB에 넣지 않는다.
- `(worktree, branch)`별 view가 `path → sha`와 HEAD를 소유한다. 검색은 현재 checkout의
  view만 사용한다. 다른 checkout의 검색 결과를 대신 반환하지 않는다.
- 편집은 해당 view의 매핑만 이동한다. 기존 내용은 유지하고 실제로 새로운 내용에만
  새 내용 행과 임베딩이 필요하다.
- 삭제는 같은 worktree, 기록 commit이 HEAD의 조상, 경로 소실이 모두 증명될 때만 한다.
  다른 계보·다른 worktree·rebase/merge 진행·모르는 commit은 보류한다. 25% 대량 삭제
  가드는 최종 안전망이다.
- `sourceRevisions`의 원문 지문은 DB가 소유한다. 카드 frontmatter는 지문 저장소가 아니다.
- 사용자당 resident service 하나가 모든 프로젝트의 임베딩 모델과 학습 추론을 공유한다.
  이 서비스는 이미 존재하며 DB 개편 때문에 모델 서비스를 추가하지 않는다.

이 문서의 제안이며 사용자 확인 대상인 선택:

1. **공유 범위는 같은 저장소의 프로젝트 하나다.** 공통 Git dir와 프로젝트 상대 경로가
   같은 branch/worktree는 한 DB를 공유한다. 이름이 같거나 SHA가 같은 다른 프로젝트,
   별도 clone에는 자동 공유하지 않는다. 사용자 전체 content DB는 별도 설계다.
2. 카드 SHA는 **카드 전체 UTF-8 파일 바이트의 SHA-256**이다. Git object ID나
   `verifiedBodyHash`와 구분한다. 줄바꿈·frontmatter를 암묵적으로 정규화하지 않는다.
3. 같은 내용의 파생물은 `(blob_sha, chunk_recipe, vector_profile)` 안에서 공유한다.
   모델·차원·chunking·임베딩 입력이 바뀌면 새 파생물이다. 다른 프로필 벡터를 재사용한
   것처럼 취급하지 않는다.
4. 완성된 lexical view를 먼저 검색 가능하게 하고, 벡터가 완성되면 별도 세대를 봉인한다.
   동기화·마이그레이션·번들 업데이트 동안 증명된 기존 읽기 세대를 유지한다.
5. GC는 참조가 끊긴 행을 실행 중인 DB에서 수정하는 대신, 새 incarnation으로의
   mark-and-copy compaction을 사용한다. 보존 기간이 확정되기 전 자동 GC는 비활성이다.

범위는 저장·view·봉인·수명 주기와 안전한 전환이다. 호스트 프로토콜, 임베딩 모델,
추론 서비스의 API와 학습 동의 정책은 유지한다. 이 설계 작업은 코드·설정·버전을 바꾸지 않는다.

## 2. 현재 구현과 목표의 대응

아래 경로는 저장소 루트 기준이다. 심볼 이름을 기준으로 현재 구현을 확인할 수 있다.

| 현재 위치·구조 | 현재 계약 | 목표의 소유자·변경 경계 |
|---|---|---|
| `core/laya_index_a.py`, `_SCHEMA_V6`, `LayaIndexA` | application id `LYA1`, schema 6. `content(hash, body)`는 이미 내용 공유·불변이다. `membership(gen, scope, path, hash, eligible)`와 `head(scope, gen)`이 검색 집합을 정한다 | `wiki_blobs`는 기존 개념을 유지한다. scope 단일 head를 checkout별 `views`·`view_heads`로 나눈다. raw를 content에 복사하지 않는다 |
| 같은 파일의 `generations`, `candidate_seals`, `receipts`, `pins`, `exclusions`, `gen_fts_<ord>` | building → prepared → published, full membership, claim/manifest seal, view pin, scope 제외. generation별 FTS는 경로·제목·본문을 색인한다. UPDATE/DELETE/REPLACE 방지 trigger가 있다 | 세대는 view·HEAD·원문 지문·trust·파생물까지 묶는다. 제외를 현재 view에 먼저 적용한다. GC가 기존 trigger를 해제하지 않는다 |
| `core/laya_index_vector.py`, `LayaVectorIndex`, `source_evidence`, `profile_identity` | cache 키는 `(hash, title, profile_id, seq)`. chunk-set seal과 generation vector seal이 별도다. 제목은 Markdown heading 또는 경로에서 유래한다 | path에서 독립적인 입력 recipe를 사용한다. shared chunk/vector set과 view membership seal을 분리한다. legacy title 의존 벡터를 무조건 승계하지 않는다 |
| `core/laya_index_source_port.py`, `core/laya_source_catalog.py` | no-follow 스캔, 열거/해시/디렉터리·파일 검증/완료 pointer. 미완료 스캔은 완료 snapshot을 대체하지 않는다. catalog에는 파일 metadata와 SHA가 있다 | raw catalog는 지문만, wiki scanner는 blob 입력을 담당한다. scan capture에 worktree·branch·HEAD·operation 상태를 추가한다. partial listing은 삭제 근거가 아니다 |
| `core/laya_index_capacity.py`, `GenerationBindingStore` | `.bindings.sqlite`가 scope·DB incarnation·root identity·manifest·catalog snapshot을 봉인한다. `g-…-b<digest>` 이름이 binding을 커밋한다 | 같은 DB의 `generation_seals`가 checkout/source/trust root를 포함한다. 이름의 binding digest에 준하는 변조 탐지 계약을 유지한다 |
| `core/laya_index_claim_queue.py`, `core/laya_index_flow.py`, `core/laya_index_sync.py` | durable claim → stage → seal → publish → ACK. 이전 head를 유지한 채 완료 snapshot만 게시한다 | 작업 키에 view/capture/base-generation을 포함하고 CAS는 해당 view에만 적용한다. stale checkout 작업은 게시·삭제하지 않는다 |
| `core/laya_vector_capacity.py` | pending scan fill과 complete snapshot fill의 cursor가 별도다. 기본 fill은 8개·2 MB, 한 문서의 sealed set을 먼저 커밋한다 | branch/worktree 간 sealed set cache hit를 공유한다. content lease로 중복 inference를 합친다. 세대 봉인은 모델 없이 수행한다 |
| `core/laya_incremental_docs.py`, `core/laya_incremental_docs_query.py` | 설치·scope·incarnation·profile에 묶인 `docs(path, hash, title, body, …)` sidecar와 FTS. 먼저 완성된 문서가 기존 경로를 shadow한다 | 설치 전체 `path` overlay는 다른 branch에 적용할 수 없다. 새 모델은 작은 immutable lexical generation으로 먼저 게시하고 sidecar를 제거한다. 전환 중 기존 sidecar의 completed rows도 보존한다 |
| `core/laya_index_runtime.py`, `LayaIndexRuntime` | 증명된 scope/root/DB/queue를 받는다. capacity sidecar 두 개가 함께 존재해야 한다. runtime은 설치 권한을 만들지 않는다 | `ViewHandle`을 받아 같은 reader transaction에서 head·seal·membership을 pin한다. DB 공유는 접근 권한 공유를 뜻하지 않는다 |
| `core/laya_install_journal.py`, `verify_selected_route*`, `cas_mark_new_write` | journal은 설치·bundle·route·attestation·scope·incarnation을 증명한다. `newWriteSeen`은 첫 쓰기 전 fence다. bundle drift에 한해 증명된 읽기 전용 route를 허용한다 | reader endpoint를 journal에서 독립적으로 봉인한다. v6와 새 DB를 동시에 읽을 수 있는 bridge 뒤에만 DB/route 전환을 허용한다 |
| `core/laya_index_host_route.py`, `select_route`, `_worktree_route` | candidate/preview/worktree/blocked/legacy를 분류한다. 연결 worktree는 현재 main checkout의 마지막 wiki 세대를 읽기 전용 검색한다 | 공통 저장소의 owner install을 찾되 **검색 view는 호출 checkout**으로 선택한다. main-view fallback은 새 route에서 금지한다. 새 view의 writer는 별도 증명된 worker다 |
| `core/wiki_lifecycle.py`, `core/wiki_source_retire.py`, `core/git_branch_safety.py` | frontmatter `sourceRevisions`가 근거다. Git 계보 판정과 진행 중 operation 보류가 있다. linked worktree에서는 lifecycle을 실행하지 않는다. archive 후 unlink하고 sync를 요청한다 | DB의 정확한 compile-time 지문을 사용한다. worktree별 계보 증명 뒤 그 view와 그 checkout의 카드만 처리한다. 현재 Git heuristic으로 누락 commit 지문을 만들어내지 않는다 |
| `core/wiki_card.py`, `core/wiki_compile.py`, `core/laya_host_response.py`의 `_verified_wiki_locations`, `core/laya_source_exclusion.py` | 신뢰 카드·본문 hash·span·skipPaths 확인이 frontmatter 지문을 읽는다. selected recall은 현재 raw 본문을 다시 읽지 않는다 | trust·source spans·skipPaths 검사는 DB provenance join을 사용한다. 검색은 봉인된 wiki bytes만 읽는다. compiler/lifecycle/dedup/learning도 같은 provenance API를 쓴다 |
| `core/laya_bundle_upgrade.py`, `core/laya_active_upgrade.py`, `core/laya_bound_upgrade.py`, `core/laya_inactive_upgrade.py`, `core/laya_fresh_wiki_origin.py` | bundle provenance·provider 비교·intent·ledger `upgrading` fence가 있다. active upgrade는 journal 활성화 뒤 fence 정리까지 복구한다. fence 중 recall은 컨텍스트 없이 exit 0일 수 있다 | bundle 교체와 data-schema migration을 별도 상태 기계로 유지한다. fence가 writer를 막아도 마지막 증명된 reader endpoint는 유지한다 |
| `core/laya_resident_service.py`, `core/laya_product_sync_worker.py` | UID 단위 resident service가 profile별 worker를 공유하고 startup/설치 증명을 한다. cold load를 직렬화한다 | DB별 모델은 만들지 않는다. 현재 profile의 service를 사용하며 GC·migration은 모델을 로드하지 않는다 |

현재 검색이 source-catalog pending 동안 마지막 게시 snapshot을 제공하는 성질과,
bundle fence 중 빈 컨텍스트가 가능한 성질은 서로 다르다. 후자는 목표의 업데이트 중
검색 연속성을 충족하지 않으므로 bridge/reader-fence 작업이 schema 전환의 선행 조건이다.

## 3. identity와 content 계약

### 3.1 저장소·프로젝트·워크트리·브랜치

- `repo_id`는 owner가 생성한 UUID다. canonical common Git dir와 그 `(dev, ino)`,
  프로젝트의 저장소 내 상대 경로를 설치 journal에 바인딩한다. 경로 문자열만으로 동일성을
  인정하지 않는다. nested project와 submodule은 별도 project/repo binding이다.
- `worktree_id`는 등록 시 생성한 UUID다. canonical checkout root, canonical Git admin dir,
  각각의 `(dev, ino)`가 지문이다. 디렉터리 이동·삭제 후 같은 경로 재생성은 자동 인계하지
  않는다. 기존 state/project 이동 계약과 같은 명시적 rebind 절차를 사용한다.
- branch key는 `refs/heads/<name>` 전체 문자열이다. branch rename은 새 key이며 기존
  세대와 blob을 재사용해 새 view를 만들 수 있다. detached HEAD는 `detached:<object-id>`다.
- Git object ID는 저장소의 object format과 함께 저장한다. SHA-1 길이를 고정하지 않는다.
  unborn·Git 불명·non-Git의 HEAD는 NULL이며 삭제 권한이 없다. non-Git은
  `branch_key='non-git'`인 단일 view로 검색·편집할 수 있고 자동 삭제는 보류한다.
- 경로는 프로젝트 상대 POSIX 경로다. absolute/`..`/빈 segment/원문 dot metadata/심볼릭
  링크는 기존 규칙으로 거른다. macOS NFC/case alias 처리는 현재 canonical path 규칙을
  유지하고, 다른 파일이 같은 key로 충돌하면 scan을 보류한다. wiki root 내부는 기존
  meta-page 제외 규칙을 유지한다.

### 3.2 무엇을 hash하고 공유하는가

`blob_sha = sha256(exact_card_utf8_bytes)`다. DB `body`는 이 파일 전문이다. 읽은 바이트를
decode/encode한 결과가 원본과 같아야 한다. 같은 SHA에 다른 바이트가 들어오면
`blob_conflict`로 거절하고 덮어쓰지 않는다. 본문 trust hash는 기존 의미 그대로 별도다.

path·branch·worktree·HEAD·mtime·원문 지문은 blob의 외부 참조다. 카드 파일에 있는 제목,
상태 등의 내용이 달라지면 다른 blob이다. 의미가 비슷하다는 이유로 합치지 않는다.
`sourceRevisions`를 제거하는 명시적인 카드 재작성도 다른 blob이며 기존 blob은 유지된다.

현재 provider는 `embed_document_chunks(body, path)`를 받는다. 목표 caller는 실제 경로
대신 `blobs/<blob_sha>.md`를 전달한다. heading이 없을 때도 제목이 SHA에서 결정되므로
동일 바이트의 이름 변경이 임베딩 입력을 바꾸지 않는다. 표시 제목과 filename 검색은
view membership/FTS가 실제 경로를 사용한다. provider/worker 파일 수정은 이 설계에 없다.

이 caller 입력 규칙을 `blob-input-v1`이라는 새 input recipe에 포함한다. chunk recipe에는
tokenizer·chunker artifact digest, token window/overlap, 순서·offset 단위, heading 규칙,
입력 prefix와 synthetic-path 규칙을 포함한다. profile은 모델 바이트·차원·metric·query/doc
입력 recipe를 포함한다. 같은 model_id 문자열만으로 호환성을 판단하지 않는다.

legacy vector는 실제 title·chunk 입력·profile이 정확히 같은 경우에만 재사용한다.
다르면 legacy recipe로 보존하고 새 recipe의 vector를 백그라운드에서 채운다. full-text
검색이 이 작업을 기다리지 않는다. recipe 변경의 provider review/attestation 영향은
업그레이드의 명시적 rebuild 경로에서 처리하며 단순 경로 재배치로 위장하지 않는다.

## 4. 제안 스키마

설명용 DDL이다. 새 application id와 schema version은 구현 PR에서 예약한다. v6 파일의
`user_version`만 올려 덮어쓰지 않는다. PK/FK 외에 타입·SHA 형식·no-REPLACE·immutability
검증 trigger와 private-file 검사는 구현의 필수 조건이다. 아래 JSON은 정규 canonical JSON,
SHA는 소문자 64 hex, timestamp는 UTC ns, `head_oid`는 Git object-format에 맞는 문자열이다.

```sql
CREATE TABLE store_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  format TEXT NOT NULL, schema_version INTEGER NOT NULL,
  repo_id TEXT NOT NULL, scope_id TEXT NOT NULL, incarnation TEXT NOT NULL,
  common_git_dir TEXT, common_dev INTEGER, common_ino INTEGER,
  project_rel TEXT NOT NULL, git_object_format TEXT,
  created_ns INTEGER NOT NULL
);
CREATE TABLE worktrees (
  worktree_id TEXT PRIMARY KEY,
  canonical_root TEXT NOT NULL UNIQUE, git_admin_dir TEXT UNIQUE,
  root_dev INTEGER NOT NULL, root_ino INTEGER NOT NULL,
  admin_dev INTEGER, admin_ino INTEGER,
  binding_digest TEXT NOT NULL UNIQUE, registered_ns INTEGER NOT NULL
);
CREATE TABLE views (
  view_id TEXT PRIMARY KEY,
  worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
  branch_key TEXT NOT NULL,
  UNIQUE(worktree_id, branch_key)
);
CREATE TABLE wiki_blobs (
  blob_sha TEXT PRIMARY KEY, body TEXT NOT NULL,
  byte_length INTEGER NOT NULL CHECK(byte_length>=0), created_ns INTEGER NOT NULL
);
CREATE TABLE chunk_recipes (
  recipe_id TEXT PRIMARY KEY, canonical_spec TEXT NOT NULL
);
CREATE TABLE chunk_sets (
  blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  recipe_id TEXT NOT NULL REFERENCES chunk_recipes(recipe_id),
  chunk_count INTEGER NOT NULL CHECK(chunk_count>0), set_digest TEXT NOT NULL,
  PRIMARY KEY(blob_sha, recipe_id)
);
CREATE TABLE chunks (
  blob_sha TEXT NOT NULL, recipe_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq>=0), position INTEGER NOT NULL CHECK(position>=0),
  text TEXT NOT NULL, text_sha TEXT NOT NULL,
  PRIMARY KEY(blob_sha, recipe_id, seq),
  FOREIGN KEY(blob_sha, recipe_id) REFERENCES chunk_sets(blob_sha, recipe_id)
);
CREATE TABLE vector_profiles (
  profile_id TEXT PRIMARY KEY, model_id TEXT NOT NULL,
  embedding_fingerprint TEXT NOT NULL, input_recipe TEXT NOT NULL,
  dimension INTEGER NOT NULL CHECK(dimension BETWEEN 1 AND 2048),
  metric TEXT NOT NULL CHECK(metric='cosine')
);
CREATE TABLE embedding_sets (
  blob_sha TEXT NOT NULL, recipe_id TEXT NOT NULL,
  profile_id TEXT NOT NULL REFERENCES vector_profiles(profile_id),
  chunk_count INTEGER NOT NULL CHECK(chunk_count>0), set_digest TEXT NOT NULL,
  PRIMARY KEY(blob_sha, recipe_id, profile_id),
  FOREIGN KEY(blob_sha, recipe_id) REFERENCES chunk_sets(blob_sha, recipe_id)
);
CREATE TABLE vectors (
  blob_sha TEXT NOT NULL, recipe_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  seq INTEGER NOT NULL, input_sha TEXT NOT NULL,
  embedding BLOB NOT NULL, provenance_digest TEXT NOT NULL,
  PRIMARY KEY(blob_sha, recipe_id, profile_id, seq),
  FOREIGN KEY(blob_sha, recipe_id, seq) REFERENCES chunks(blob_sha, recipe_id, seq),
  FOREIGN KEY(blob_sha, recipe_id, profile_id)
    REFERENCES embedding_sets(blob_sha, recipe_id, profile_id)
);
CREATE TABLE source_fingerprints (
  fingerprint_id TEXT PRIMARY KEY,
  worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
  branch_key TEXT NOT NULL, path TEXT NOT NULL, source_sha TEXT NOT NULL,
  byte_length INTEGER NOT NULL CHECK(byte_length>=0), head_oid TEXT,
  tracked_state TEXT NOT NULL CHECK(tracked_state IN ('tracked','untracked','unknown')),
  binding_state TEXT NOT NULL CHECK(binding_state IN ('observed','legacy_unbound')),
  scan_id TEXT NOT NULL, observed_ns INTEGER NOT NULL
);
CREATE TABLE card_attestations (
  trust_digest TEXT PRIMARY KEY,
  blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  verification_kind TEXT NOT NULL CHECK(verification_kind IN ('compiled','rebound','legacy_verified')),
  engine_identity TEXT NOT NULL, policy_digest TEXT NOT NULL,
  provenance_digest TEXT NOT NULL, receipt_json TEXT NOT NULL,
  created_ns INTEGER NOT NULL,
  UNIQUE(trust_digest, blob_sha)
);
CREATE TABLE generations (
  gen_id TEXT PRIMARY KEY, view_id TEXT NOT NULL REFERENCES views(view_id),
  parent_gen TEXT REFERENCES generations(gen_id),
  head_oid TEXT, checkout_digest TEXT NOT NULL, checkout_evidence TEXT NOT NULL,
  scan_id TEXT NOT NULL, policy_digest TEXT NOT NULL,
  revocation_epoch INTEGER NOT NULL CHECK(revocation_epoch>=0),
  state TEXT NOT NULL CHECK(state IN ('building','prepared','published')),
  created_ns INTEGER NOT NULL,
  UNIQUE(gen_id, view_id)
);
CREATE TABLE wiki_members (
  gen_id TEXT NOT NULL REFERENCES generations(gen_id), path TEXT NOT NULL,
  blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  display_title TEXT NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
  trust_digest TEXT,
  CHECK(eligible=0 OR trust_digest IS NOT NULL),
  PRIMARY KEY(gen_id, path), UNIQUE(gen_id, path, blob_sha),
  FOREIGN KEY(trust_digest, blob_sha) REFERENCES card_attestations(trust_digest, blob_sha)
);
CREATE TABLE source_members (
  gen_id TEXT NOT NULL REFERENCES generations(gen_id), path TEXT NOT NULL,
  fingerprint_id TEXT NOT NULL REFERENCES source_fingerprints(fingerprint_id),
  presence TEXT NOT NULL CHECK(presence IN ('present','held_missing')),
  PRIMARY KEY(gen_id, path)
);
CREATE TABLE card_sources (
  gen_id TEXT NOT NULL, wiki_path TEXT NOT NULL, ordinal INTEGER NOT NULL,
  fingerprint_id TEXT NOT NULL REFERENCES source_fingerprints(fingerprint_id),
  spans_json TEXT NOT NULL,
  PRIMARY KEY(gen_id, wiki_path, ordinal),
  FOREIGN KEY(gen_id, wiki_path) REFERENCES wiki_members(gen_id, path)
);
CREATE TABLE member_vectors (
  gen_id TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
  recipe_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  PRIMARY KEY(gen_id, path),
  FOREIGN KEY(gen_id, path, blob_sha) REFERENCES wiki_members(gen_id, path, blob_sha),
  FOREIGN KEY(blob_sha, recipe_id, profile_id)
    REFERENCES embedding_sets(blob_sha, recipe_id, profile_id)
);
CREATE TABLE generation_seals (
  gen_id TEXT PRIMARY KEY REFERENCES generations(gen_id),
  membership_digest TEXT NOT NULL, sources_digest TEXT NOT NULL,
  trust_digest TEXT NOT NULL, lexical_digest TEXT NOT NULL,
  vectors_digest TEXT NOT NULL, binding_digest TEXT NOT NULL,
  eligible_count INTEGER NOT NULL, vector_member_count INTEGER NOT NULL,
  sealed_ns INTEGER NOT NULL
);
CREATE TABLE view_heads (
  view_id TEXT PRIMARY KEY REFERENCES views(view_id), gen_id TEXT NOT NULL,
  epoch INTEGER NOT NULL CHECK(epoch>=0),
  FOREIGN KEY(gen_id, view_id) REFERENCES generations(gen_id, view_id)
);
CREATE TABLE delete_receipts (
  receipt_id TEXT PRIMARY KEY, view_id TEXT NOT NULL REFERENCES views(view_id),
  base_gen TEXT NOT NULL REFERENCES generations(gen_id), path TEXT NOT NULL,
  fingerprint_id TEXT NOT NULL REFERENCES source_fingerprints(fingerprint_id),
  observed_head TEXT NOT NULL, proof_json TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('hold','retire')),
  reason TEXT NOT NULL, created_ns INTEGER NOT NULL
);
CREATE INDEX source_by_checkout_path
  ON source_fingerprints(worktree_id, branch_key, path, observed_ns);
CREATE INDEX source_by_sha ON source_fingerprints(source_sha);
CREATE INDEX wiki_blob_refs ON wiki_members(blob_sha, gen_id, path);
CREATE INDEX card_source_refs ON card_sources(fingerprint_id, gen_id, wiki_path);
CREATE INDEX generation_history ON generations(view_id, created_ns);
CREATE INDEX delete_by_view ON delete_receipts(view_id, base_gen, path);
```

`source_fingerprints`가 저장하는 값은 metadata뿐이다. `fingerprint_id`는 모든 identity·path·SHA·
commit·scan 필드의 domain-separated digest다. `source_members`는 현재 관측 지문이고,
`card_sources`는 **그 카드를 만들 때 실제로 사용한 지문**이다. 원문이 바뀌었다고
`card_sources`를 최신 SHA로 옮기지 않는다. 새 compile 또는 동일 입력의 검증된 checkout
rebind가 성공해야 새 provenance가 생긴다.
`legacy_unbound`는 원문 SHA를 읽을 수 있어도 원래 worktree/commit을 증명하지 못하는
import다. 삭제를 허가하지 않는다. spans는 source fingerprint에 묶인 locator metadata이며
원문 excerpt는 저장하지 않는다.

`card_attestations`의 trust digest는 blob SHA, compiler 검증 결과·engine identity, 사용한 fingerprint 목록,
spans, 적용 policy digest를 커밋한다. blob만 같아도 다른 view의 trust를 빌리지 않는다.
같은 카드가 두 view에서 서로 다른 원문 근거를 가지는 경우 각각의 `card_sources`를 쓴다.
DB가 지문을 소유한다는 것은 compile/verify receipt의 소유권도 DB에 있다는 뜻이다.
receipt JSON은 검증 방식·digest·policy·판정만 저장하며 raw 본문과 host transcript를 포함하지
않는다. 원문 지문을 확정하지 못한 legacy import에는 검증 가능한 이전 trust만
`legacy_verified`로 보존한다. 새 compile의 증명인 것처럼 표시하지 않는다.
다른 checkout에서 같은 카드를 재사용할 때는 원래 attestation과 모든 source path/SHA·spans·
policy를 비교하고 현재 checkout에서 같은 bytes를 관측한 뒤 `rebound` receipt를 만든다.
이 receipt는 원래 trust digest를 참조하며 새 worktree의 관측 지문을 별도로 묶는다.
원래 source SHA가 하나라도 다르거나 unknown이면 rebind하지 않는다. branch 이름만 바꿔
원래 compile commit/worktree를 새 값으로 덮어쓰지 않는다. fingerprint가 없는 단순 Git
카드 복사는 `legacy_unbound`로 보류하며 blob 공유 자체가 trust를 부여하지 않는다.

chunk/vector set과 모든 자식 행은 한 transaction으로 넣고, count·연속 seq·증가 position·
입력 digest·float32 길이와 유한성·nonzero norm을 검증한 뒤 commit한다. partial set은
staging job 안에만 존재하고 reader가 join하는 테이블에 커밋하지 않는다. replay가 같으면
no-op, 다르면 conflict다. 같은 blob에 대한 중복 inference는 work lease로 합치고 crash 후
lease 회수는 소유 증명으로 한다. timeout을 이유로 임의 프로세스를 종료하지 않는다.

FTS는 `wiki_fts_<generation ordinal>`로 path/title/body를 색인한다. ordinal은 SQLite가
배정하며 사용자 문자열은 SQL identifier가 아니다. `path`는 member, `body`는 blob에서
얻는다. 초기에는 현재와 같은 세대별 FTS를 유지해 ranking 동등성을 검증한다. FTS 내부
본문 토큰의 세대별 복제는 파생 색인 비용이며 `wiki_blobs`의 중복 content row가 아니다.
이를 없애는 global FTS는 view filter 이전 top-K 누락과 BM25 통계 혼입 때문에 후속 최적화다.

제약으로 보장할 조건:

- seal 이후 membership/provenance/FTS/member-vector 행을 바꾸지 않는다. generation
  상태는 단방향이며 head는 같은 view의 published+sealed 세대만 가리킨다.
- generation source/member fingerprint의 worktree·branch·path는 view와 맞아야 한다.
  과거 provenance의 commit은 generation HEAD와 달라도 된다. 다른 worktree의 지문을
  현재 관측 지문인 것처럼 등록하지 않는다.
- 검증되지 않은 wiki도 `eligible=0`, trust NULL로 membership에 기록할 수 있다. 검색에는
  포함하지 않고 trusted source/card로 승격하지 않는다. seal은 이 상태도 명시적으로 커밋한다.
- `parent_gen`은 같은 view 또는 검증된 bootstrap donor만 가리킨다. donor 참조는 GC
  영구 root가 아니며 현재 generation의 모든 의존 행은 독립적으로 참조한다.
- `source_members(path)`와 fingerprint.path, spans와 source SHA, vector set의 recipe/profile,
  seal의 count/digest가 일치해야 한다. FK만으로 부족한 부분은 writer와 seal verifier가
  함께 검증한다.
- raw 행을 `wiki_blobs`·FTS·chunks/vectors에 쓰는 API는 없다. blob 삽입에는 wiki-root
  membership proof가 필요하다. materialized legacy raw content는 migration 대상에서 제외한다.

## 5. view 해석과 검색

### 5.1 checkout capture

`ViewHandle`은 `{repo_id, scope_id, incarnation, worktree_id, branch_key, head_oid,
view_id, checkout_digest, read_endpoint_epoch}`다. route 선택과 데이터 view 선택을 분리한다.

1. cwd에서 가장 가까운 프로젝트 settings를 찾아 기존 selected-route/journal 증명을 한다.
   연결 worktree는 common Git dir와 프로젝트 상대 위치를 통해 owner install을 찾는다.
   서로 다른 settings/skipPaths, submodule, 자기 route가 있는 worktree는 자동 합치지 않는다.
2. Git env 오염을 제거한 bounded read-only Git 호출로 root/admin/common identity,
   symbolic ref/HEAD, index 상태, operation marker를 capture한다. `HEAD`만으로 dirty
   working tree를 대표하지 않는다. scan에는 index fingerprint와 파일 검증 manifest도 묶는다.
3. `(worktree_id, branch_key)`로 view를 정한다. caller가 임의 view_id를 지정해 다른 view를
   검색할 수 없다. SQL candidate 생성 전에 view를 확정한다.
4. reader는 SQLite read transaction 하나에서 endpoint/incarnation, view head, seal,
   membership을 pin한다. FTS와 vector와 context body는 **같은 gen_id**에서 읽는다.
5. 검색 완료 직전에 root·branch·HEAD·operation identity를 다시 확인한다. 도중 checkout이
   바뀌면 결과를 버리고 남은 hook budget 내 한 번 재시도한다. 계속 변하면
   `checkout_changed`로 컨텍스트 없이 정상 종료하고 다른 branch 결과는 반환하지 않는다.

검색 SQL의 핵심은 다음 join이다. vector search도 같은 membership을 출발점으로 삼는다.

```sql
SELECT m.path, m.blob_sha, b.body
FROM view_heads h
JOIN generations g ON g.gen_id=h.gen_id AND g.view_id=h.view_id
JOIN generation_seals s ON s.gen_id=g.gen_id
JOIN wiki_members m ON m.gen_id=g.gen_id AND m.eligible=1
JOIN wiki_blobs b ON b.blob_sha=m.blob_sha
WHERE h.view_id=:resolved_view AND g.state='published';
```

실제 FTS match·source skipPaths·revocation/trust filtering은 LIMIT 전에 적용한다. shared
vector 전역 top-K를 구한 뒤 다른 view의 행을 제거하는 구현은 금지한다. 동일 SHA의 여러
경로는 cosine을 한 번 계산한 뒤 현재 view의 경로에 확장한다. 출력 dedup과 path 선택은
결정적이어야 한다. RRF/selector/learning에는 pinned view/gen도 귀속한다.

현재 checkout view의 마지막 sealed generation이 현재 HEAD보다 오래될 수 있다. 이는
해당 view의 **마지막 게시 snapshot**이며 최신성 주장이 아니다. 편집·sync 중에도 검색한다.
같은 branch의 force-update/reset 등 다른 계보를 관측하면 과거 결과에 `lineage_changed`
상태를 붙이고 삭제는 금지한다. 그 view의 새 lexical generation을 우선 생성한다.
다른 branch의 view를 fallback으로 사용하지 않는다.

### 5.2 첫 view와 검색 연속성의 경계

등록된 view의 업데이트는 새 view가 ready가 될 때까지 기존 세대를 유지하므로 검색이
중단되지 않는다. branch switch도 목적 view가 있으면 그 view만 즉시 검색한다.

처음 보는 branch/worktree에는 그 checkout의 게시 snapshot이 없다. 추천 정책은 background
worker가 **그 checkout을 직접 검증한 lexical generation**을 먼저 만든 뒤 vector를 채우는
것이다. 기존 generation의 blob/FTS 입력을 donor로 쓸 수는 있지만, path→SHA·trust·
skipPaths가 해당 checkout에서 검증돼야 한다. HEAD가 같아도 untracked/dirty 파일이 다르면
main view를 복제하지 않는다. hook 안에서 raw 본문 스캔·임베딩을 시작하지 않는다.

검증된 최초 snapshot 전에는 `view_preparing`과 함께 정상 무컨텍스트 응답을 낸다.
이는 기존 검색을 업데이트 때문에 잃는 경우와 구분한다. **한 번도 관측하지 않은 branch에
즉시 결과를 보장하면서 다른 view의 결과를 금지하는 것은 사전 scan 없이 불가능하다.**
첫 호출까지 무컨텍스트를 허용하지 않는 요구라면, checkout 전 prewarm 또는 bounded 동기
wiki scan을 별도로 선택해야 한다. 이 선택을 사용자 미확정 질문으로 남긴다.

linked worktree에서 쓰기 권한은 이 reader 경로로 생기지 않는다. 등록/compile/sync worker가
owner journal과 해당 checkout binding을 별도로 증명한다. main의 기존 read-only worktree
fallback은 새 route에서 사용할 수 없다. 모든 host는 같은 core resolver를 사용한다.

## 6. 편집·compile·세대 봉인

### 6.1 원문 편집과 카드 게시

1. hook은 현재 checkout의 변경 wake만 남긴다. job key는
   `(view_id, captured_checkout_digest, source_path, source_sha, policy_digest)`다.
   원문 본문은 queue/DB/event log에 넣지 않는다.
2. bounded scan은 원문을 no-follow로 읽어 지문을 만든다. compile 호출 동안만 필요한
   원문을 메모리에 둔다. 읽기 전후 stat/hash와 branch/HEAD를 확인한다.
3. extractor 결과의 grounding·verify가 성공하면 card bytes와 compile receipt를 준비한다.
   DB provenance는 실제 입력 지문에 묶인다. 단순 관측만으로 기존 카드가 검증되지 않는다.
4. 파일과 DB의 원자 commit은 불가능하므로 durable compile intent를 쓴다. intent에는
   view/capture, target path, old/new card SHA, fingerprint ids, proof digest, 단계만 기록한다.
   카드 전문 임시 파일은 wiki staging 아래에 두며 raw 본문은 intent에 넣지 않는다.
5. writer는 route와 checkout을 재증명하고 temp card를 fsync/rename한다. 그 뒤 DB에 blob과
   receipt를 commit한다. crash가 끼면 old published generation은 검색 가능하다. replay는
   target SHA가 old/new 중 무엇인지 확인하고 idempotent하게 끝낸다. 다른 bytes면 보류한다.
6. metadata·카드 파일·receipt가 모두 맞는 것만 다음 세대에 포함한다. 카드 삭제도 같은
   intent/expected-hash 계약으로 archive 후 처리한다. 다른 checkout 파일은 수정하지 않는다.

원문은 바뀌었지만 재생성된 카드가 동일 bytes이면 새 `source_fingerprints`와 provenance
참조만 필요하다. blob/chunk/vector는 재사용한다. A branch 편집은 B view head를 움직이지
않는다. 같은 내용으로 돌아오는 편집은 기존 SHA를 재사용한다.

### 6.2 seal과 publish

```text
capture checkout + route epoch
  → bounded raw/wiki scans → complete scan manifest
  → insert/reuse immutable blobs + source/compile evidence
  → building generation: full path→SHA membership, source/trust references, FTS
  → verify all references + prepare generation seal
  → prepared → CAS(view head, expected base/epoch) → published + head swap
  → ACK exact wake revision

missing vectors → bounded shared fill → another sealed generation → same-view CAS
```

seal은 domain-separated canonical digest로 다음을 커밋한다:

- store incarnation·scope·view·worktree binding·branch·HEAD·scan/checkout digest;
- 정렬된 wiki path/SHA/eligibility/display-title/trust와 source path/fingerprint/presence;
- card→source/spans provenance, policy/revocation epoch, FTS row digest;
- 각 member의 recipe/profile/chunk-set digest와 vector-set digest 또는 명시적 lexical-only 표시.

공유 blob이나 vector set 전체를 세대마다 복사하지 않고 해당 digest를 참조한다. blob bytes와
set bytes의 hash/provenance는 삽입·seal 검증 때 증명한다. query는 pin된 seal과 반환 후보의
blob/set을 검증한다. 모든 다른 view의 blob을 매 호출 해시하지 않는다.

`gen_id`는 `g-<uuid>-b<binding_digest>` 형식으로 위 binding root를 이름에 커밋한다.
digest 계산에는 gen_id 자신을 넣지 않는다. writer는 완성된 scan·provenance·coverage의 예상
manifest를 먼저 정한 뒤 이름을 배정하고 그 이름으로 building rows를 쓴다. seal 때 실제
rows가 예상 root와 다르면 해당 generation은 게시하지 않는다. `checkout_evidence`에는
binding을 재검증할 canonical identity/index/operation/manifest metadata를 저장한다.
endpoint 교체의 journal receipt도 선택한 gen_id/root를 증명한다. DB 안의 seal과 sidecar를
함께 고쳐 쓰는 것만으로 외부 binding을 만족한 것처럼 만들 수 없어야 한다. 이는 기존
`g-…-b<digest>`의 consistency 보장을 유지하며, 같은 OS 사용자가 DB와 journal을 모두
임의 수정할 수 있는 상황까지 방어하는 보안 경계라는 뜻은 아니다.

initial lexical generation은 `vector_member_count=0`이다. 부분 vector coverage를 허용하는
후속 세대는 coverage를 정확히 봉인하고 FTS 후보를 항상 유지한다. profile을 섞어 cosine을
계산하지 않는다. vector worker 실패는 lexical 검색을 지우지 않는다. 초기 rollout은
lexical-only 또는 완전 vector coverage 두 상태로 제한하고, partial coverage는 별도 검증 후
허용한다. 벡터 한 행이 뒤늦게 생겨도 기존 generation seal의 의미는 달라지지 않는다.

CAS 직전 writer는 current route epoch, checkout root/ref/HEAD/index, scan manifest와
operation marker를 다시 확인한다. 다른 view의 갱신은 충돌이 아니고 같은 view의 base
갱신은 `view_head_changed`다. stale job은 재캡처하고 sealed shared content는 남겨 재사용한다.
source scan·model call 동안 SQLite writer transaction을 잡지 않는다. 세대별 transaction은
짧고 유한하며 SQLite busy는 retry이지 데이터 손상이 아니다.

## 7. 삭제·보류·GC

### 7.1 원문 소실과 카드 retirement

파일이 없다는 관측, view membership 제거, 실제 카드 파일 삭제, blob GC는 별개다.
다음 조건을 **모두** 만족해야 원문 소실을 자동 retirement의 근거로 사용한다.

1. 지문은 `observed`이고 기록 worktree identity가 현재 worker와 정확히 같다.
2. source 지문의 `head_oid`를 Git이 알고 현재 HEAD의 ancestor라고 증명한다.
   shallow boundary·timeout·명령 실패·NULL commit은 `hold`다.
3. 전체 scan이 완료되고 경로가 같은 checkout에서 없다. 상위 mount/root identity가 같다.
   sparse-checkout/skip-worktree, permission 오류, symlink/root 교체는 소실로 취급하지 않는다.
4. 경로가 HEAD tree와 index에도 없다. 디스크에서만 지운 tracked 파일, staged deletion이나
   checkout 중간 상태는 보수적으로 보류한다. committed same-lineage deletion이 기본이다.
5. rebase-merge/rebase-apply/MERGE_HEAD/CHERRY_PICK_HEAD/REVERT_HEAD/BISECT_LOG가 없다.
6. 삭제 직전에도 같은 worktree/ref/HEAD/expected card SHA이며 같은 근거가 유지된다.

같은 branch 이름만으로 같은 계보라고 보지 않는다. 다른 worktree의 관측을 현재 worktree의
삭제 증거로 쓰지 않는다. 원문이 다른 branch에만 있거나 unknown history이면 보류한다.
`legacy_unbound`, 원문 지문 없음·불완전·오류도 보류하고 재검증을 큐잉한다. 기존
frontmatter 누락만으로 카드를 자동 삭제하는 경로는 목표 DB에서는 사용하지 않는다.

보류는 `source_members.presence='held_missing'`와 이전 지문을 유지한다. 파일이 실제로
관측된 것처럼 HEAD/SHA를 새 값으로 갱신하지 않는다. deletion proof는 bounded Git 호출의
repo/worktree identity, recorded/current commit, ancestry verdict, HEAD/index path verdict,
operation absence, complete-scan id와 reason을 `delete_receipts`에 남긴다. 원문 본문은 없다.

여러 원문을 인용한 카드에서 일부만 삭제되면 살아 있는 원문을 먼저 재검증·재생성한다.
그 전에는 카드를 보존한다. 다른 카드의 같은 blob도 지우지 않는다. 카드 파일 자체가
사라진 경우도 마지막 관측의 같은-worktree/ancestor/committed-path-absence 조건을 사용한다.
untracked 카드의 수동 unlink에는 commit 증거가 없으므로 자동 retirement는 보류하고
명시적 삭제 의도를 별도 receipt로 처리한다.

### 7.2 대량 삭제 최종 가드

현재 `wiki_lifecycle.guard_verdict`의 정확한 기준을 유지한다:

```text
trip = wouldDelete > 3 AND wouldDelete > 0.25 * decidedCards
```

held/unknown은 decided 분모와 삭제 분자에서 제외한다. 분모는 현재 view에서 판정 가능한
카드이고 다른 branch/worktree 카드를 포함하지 않는다. 전체 판정의 wouldDelete를 먼저
계산하고 run당 25장 retirement cap을 나중에 적용해 소규모 반복으로 가드를 우회하지 않는다.
trip이면 해당 run의 retirement·지문 backfill·regeneration enqueue를 모두 보류한다.
정확히 25%와 25% 초과, 3장과 4장을 각각 시험한다. raw scan의 partial listing으로 이 가드를
통과하려고 시도하지 않는다. wiki dedup/수동 discard에도 별도의 사용자 의도·receipt가 필요하다.

### 7.3 blob와 파생물 GC

view에서 매핑을 제거해도 다른 view·과거 보존 세대의 blob은 존재한다. GC root는 다음과 같다.

- 모든 등록 view의 current head와 보존 대상으로 지정한 과거 세대;
- journal의 current/previous read endpoint, rollback/migration/upgrade intent;
- 진행 중인 compile/fill job이 lease로 보호하는 blob/set와 아직 ACK되지 않은 publish receipt;
- active reader가 pin한 store incarnation. reader lifetime 동안 store read lease를 유지한다.

views가 사라졌다고 간주할 수 있는 근거는 명시적 unregister/retention 정책뿐이다.
worktree 디렉터리 없음, branch 목록에서 사라짐, 미마운트, rebase 중간 상태를 자동 해제로
쓰지 않는다. detached view·unknown worktree는 보존한다. history parent는 provenance이지
무한 보존 root가 아니며, 선정한 retained generations의 실제 FK를 따라 mark한다.

기존 append-only seal을 유지하기 위해 GC는 별도 candidate DB에 살아 있는 blob/set,
retained generation의 membership/provenance/seal을 복사하고 **새 incarnation으로 다시
봉인**한다. 원래 generation digest를 새 identity에 그대로 쓰지 않는다. source DB와 새 DB의
path/SHA/search parity를 검증한 뒤 migration과 같은 reader endpoint CAS로 바꾼다.
old/new generation 대응 receipt는 남긴다. 명시적 archive가 참조하는 카드 내용도 retention
정책에서 root 여부를 정하며, archive 파일은 DB GC가 삭제하지 않는다.
copy 시 retained generation의 parent가 미보존 세대를 가리키면 새 generation의 parent는
NULL로 두고 old parent ID를 mapping receipt에 남긴다. 따라서 존재하지 않는 parent FK를
만들지 않는다. delete/compile audit receipt의 보존 범위도 명시하고, 복사할 receipt가 참조하는
fingerprint/base generation은 함께 mark하거나 별도 metadata audit로 export한다.

old DB 파일은 이전 endpoint/rollback intent가 참조하지 않고 reader lease의 exclusive lock을
얻을 때만 cleanup한다. lock을 못 얻으면 다음 pass로 미루며 reader를 종료하지 않는다.
writer도 old-store write lease를 놓아야 한다. WAL/SHM을 개별 임의 삭제하지 않는다.
공간 부족이면 compaction을 시작하지 않는다. 참조 없는 미게시 blob은 유예 기간 뒤 같은
GC로 회수한다. 삭제 ancestry 조건은 사용자 원문/카드의 retirement 권한이고, 어떤 view도
참조하지 않는 실패 job의 cache 회수에 가짜 Git 삭제 증명을 요구하지 않는다.

## 8. v6에서의 무중단 migration

### 8.1 bridge가 선행한다

0.43.19 reader는 새 view/schema/reader endpoint를 알지 못한다. incompatible DB를 기존
`candidateDb` 경로에 덮어쓰면 검색이 실패한다. **먼저 v6와 새 format을 모두 읽는 bridge
bundle을 모든 사용하는 host에 배포한다.** 오래된 host가 새 DB를 열도록 강요하지 않는다.
capability 없는 host가 남아 있으면 v6 endpoint를 유지하고 최종 전환을 미룬다.

bridge는 현재 v6 검색·검증을 유지하며 journal에 명시된 endpoint만 읽는다. v6 초기 import는
검증된 main checkout view에만 바인딩한다. 현재 linked-worktree main-index fallback 결과를
그 worktree의 게시 view라고 등록하지 않는다. 새 view route는 새 provenance/checkout
계약을 갖춘 후에 활성화한다.
bridge writer는 기존 sidecar에 현재 없는 단조 증가 publish revision과 durable receipt를
설치 상태에 추가하고, 아직 실행 중인 구 writer가 쓰기를 끝내도록 기존 operation/write
lease로 조율한다. 이 writer 계약을 확인하지 못하면 copy를 활성화하지 않는다. revision은
generation 이름·scan_id만으로 추정하지 않는다.

### 8.2 상태 기계와 copy 경계

```text
planned → copying → caught_up → shadow_verified → reader_ready → switched
                                                ↘ deferred / retry
switched → rollback_retained → cleanup_eligible
```

1. installer operation lock 아래 intent를 만든다. old route SHA/journal epoch, v6 incarnation,
   pinned generation, completed incremental-docs snapshot/revision, settings/policy digest,
   new DB path/incarnation, migration id와 cursor를 기록한다. 경로는 검증된 private state다.
2. v6 published membership과 completed sidecar rows의 union을 **현재 v6 shadow 규칙과
   동일하게** capture한다. sidecar가 shadow한 path의 옛 row를 다시 살리지 않는다.
   장시간 SQLite read transaction을 유지하는 대신 immutable generation과 revision receipt를
   pin하고 sidecar batch 복사를 짧은 transaction으로 검증한다. endpoint tuple가 바뀌면 재캡처한다.
3. wiki로 증명된 rows만 `wiki_blobs`로 copy한다. v6 raw content/vector/FTS는 copy하지 않는다.
   raw catalog에서는 지문만 copy한다. legacy raw가 old rollback DB에 남는 전환 기간은
   별도 표시하고 cleanup 승인/retention 뒤 old DB를 정리한다. 새로운 DB는 항상 raw-free다.
4. frontmatter `sourceRevisions`를 읽어 DB provenance로 import하되, 원래 worktree/commit을
   알 수 없는 지문은 `legacy_unbound`다. import 시점의 HEAD를 과거 compile commit으로
   적지 않는다. 기존 검증 가능한 카드 trust와 body는 유지하며, 소실 자동 삭제는 보류한다.
   현재 disk 원문을 다시 읽는 것은 새로운 관측이지 legacy compile 증명의 소급 작성이 아니다.
5. 기존 byte hash·seal·trust·source span을 검증하고 lexical generation을 먼저 완성한다.
   입력이 정확히 같은 legacy vector만 그 recipe로 copy한다. 새 recipe backfill은 독립이다.
6. migration 동안 legacy publish owner를 유지한다. bridge writer가 durable change receipt
   `(migration_id, old generation/sidecar revision, new generation, view, policy)`를 기록한다.
   old publish 성공 후 new publish 실패는 old 검색을 계속하며 receipt에서 catch-up한다.
   새 DB 성공을 위해 old ACK를 먼저 성공한 것처럼 기록하지 않는다. change receipt 누락은
   pinned old endpoint와 전체 path/SHA 비교로 탐지하고 다시 copy한다.
7. 마지막 catch-up에서 **writer만** 짧게 quiesce한다. old/new logical mapping, eligibility,
   frontmatter/DB trust 판정, spans, skipPaths, lexical query corpus, compatible vector 결과를
   shadow 비교한다. 조건 불일치면 old endpoint를 유지하고 switch하지 않는다.
8. `reader_ready` 증명에는 새 schema/incarnation·view binding·sealed gen·parity report digest와
   bridge capability가 포함된다. journal CAS 한 번으로 preferred reader endpoint를 바꾼다.
   JSON route/ledger 파일 교체가 끼어도 reader는 journal의 old/new immutable endpoint 중
   일관된 하나를 읽는다. 기존 transaction은 old에서 끝나고 신규 transaction은 new를 pin한다.
9. rollback 기간에는 v6에 표현할 수 있는 main-view 변경을 old에도 replay한다. 새 worktree
   view는 v6에 표현할 수 없으므로 bridge/new DB에 유지하고, rollback이 그 view를 main
   결과로 대체하지 않는다. **reader rollback과 구 bundle 설치는 다르다.** frontmatter 없는
   카드나 새 view가 있으면 0.43.19로의 완전 downgrade를 자동 허용하지 않는다.
10. rollback 기간 종료·모든 host capability 확인·reader/write lease 해제·명시적 cleanup
    조건 후 old DB/sidecar를 정리한다. frontmatter 지문 제거는 그 이후 별도 단계다.

### 8.3 frontmatter 지문 제거

처음에는 import/dual reader 기간의 기존 카드 bytes를 그대로 둔다. 이때 DB provenance가
준비된 카드도 old reader 호환용 frontmatter를 보존한다. 목표 상태의 compiler는 원문 SHA/
HEAD/worktree 지문을 frontmatter에 쓰지 않고 DB에만 commit한다. 공개 source link/locator를
남길지는 §13의 질문이다. 기존 `sourceSpans` 내부 SHA도 provenance DB로 옮겨 단일 원천을
유지한다. `verifiedBodyHash` 같은 카드 자체의 trust 표시는 원문 지문과 구분한다.

fingerprint-free 카드의 reader/compile/lifecycle/dedup/skipPaths/learning이 DB provenance를
사용하는 것이 host별로 검증된 후에만 카드 파일 재작성을 시작한다. 이는 사용자의 Git diff를
만드는 별도 승인 범위이며 migration이 전체 wiki를 자동 수정하지 않는다. 재작성은 새 blob
SHA로 게시하고 같은 본문이라는 이유로 옛 byte-SHA의 벡터를 임의 승계하지 않는다.

## 9. journal fence·bundle upgrade·권한

bundle identity generation, install/selection epoch, **data generation**은 서로 다른 값이다.
route의 기존 `generation`은 bundle/install 계약이므로 data gen_id로 덮어쓰지 않는다.

새 journal 계약은 immutable `read_endpoints`와 mutable `reader_selection`을 추가한다.
endpoint는 `{endpoint_id, install_id, scope_id, store_format, path, incarnation,
schema_capability, view_binding_policy, provider_attestation_digest, creation_epoch}`를
커밋한다. selection은 `{epoch, preferred_endpoint, previous_endpoint, transition_id}`다.
새 route schema의 `store_binding_id`가 그 journal과 repo/worktree resolver 계약을 묶는다.
정확한 schema 번호는 구현에서 예약하며 구 reader가 해석 가능한 척 필드를 추가하지 않는다.

- fence **전** 마지막 정상 endpoint와 read-only trust proof를 durable하게 봉인한다.
  upgrade ledger의 `upgrading`은 writer/model-start 권한을 막고 reader endpoint를 없애지 않는다.
- fence 중 recall은 fence-aware journal reader로 같은 checkout의 마지막 증명된 endpoint를
  읽는다. 현재 `classify-hook → upgrading → empty` 조기 종료 경로도 core 분류/응답 계약에서
  바꿔야 한다. hook 등록 파일의 변경은 필요하지 않다.
- read proof는 private-file/root identity, scope/incarnation, sealed view, attestation binding과
  현재 checkout을 계속 증명한다. bundle 불일치라는 이유로 arbitrary DB를 읽지 않는다.
  identity 손상·symlink 교체·journal 변조·unknown schema는 여전히 fail-closed다.
- old endpoint 읽기는 DB schema migration이나 `newWriteSeen` 기록을 하지 않는다. 첫 새 DB/
  blob/provenance 쓰기 **이전** journal write fence를 통과한다. old selected store가 살아 있어도
  migration의 첫 쓰기를 `newWriteSeen` 밖으로 숨기지 않는다.
- bundle upgrade의 intent/fence/activate/finish 순서와 data migration intent는 독립이다.
  프로젝트 operation lock은 공유하고 두 writer 전환을 겹치지 않는다. order는 operation lock →
  journal transition → store writer lock이며 model cold load나 Git scan 동안 잡지 않는다.
- provider path만 다른 동일 artifact의 기존 수락 규칙은 유지한다. model/runtime/closure/
  provider bytes 또는 input recipe가 바뀌면 새 review/profile/rebuild다. GC도 profile을 바꾸지 않는다.
- compatible companion host copy, older-host refusal, QMD-origin refusal, state/project move,
  unfinished activation 복구 계약을 유지한다. schema 전환을 이유로 QMD-origin을 우회하지 않는다.
- journal activation 후 fence 해제 전 crash는 `finish_activated`에 해당하는 복구가 처리한다.
  reader는 이 구간에도 endpoint를 유지한다. stopped fence도 경고와 별개로 마지막 유효
  endpoint를 읽을 수 있어야 한다. 임의 TTL 만료로 reader를 끊지 않는다.

이 연속성 계약은 bridge를 로드한 host부터 보장한다. 이미 실행 중인 구 host가 현재 코드를
그대로 실행하면서 fence 중 결과를 내도록 만들 수는 없다. 모든 사용 host의 bridge 전환이
검색 연속성 보장의 배포 선행 조건이다. 변조/손상으로 안전한 endpoint가 하나도 없을 때는
검색 거절이 올바르며 무조건 fallback하는 가용성 보장은 하지 않는다.

## 10. 성능·자원 예산

SE의 약 1,800장은 사용자 제공 크기이며 현재 실사용 DB를 열어 측정한 숫자가 아니다.
평균 1–4 chunks/card, 768차원 float32를 가정하면 1,800–7,200 vectors의 payload는
약 **5.3–21.1 MiB**다. 2,048차원이면 약 14.1–56.3 MiB다. SQLite/FTS/metadata/WAL과
모델 RSS는 별도다. 두 branch가 같은 내용이면 vector payload는 늘지 않고 view metadata만
늘어난다. branch마다 다른 카드 10%는 대략 그 새 내용만큼 cache가 늘어난다.

| 항목 | 목표/기존 경계 | 검증 방법 |
|---|---|---|
| warm view 해석+head/seal lookup | p95 50 ms 이내, 저장소 전체 순회 없음 | 합성 1,800장·10 views, Git 호출 수/timeout 포함 |
| warm lexical query | p95 150 ms, provider 준비 실패도 FTS 유지 | 현재 FTS의 한국어/CJK·파일명·heading golden corpus 비교 |
| warm hybrid query | 기존 hook provider 2초 budget 안, 전체 recall p95 2.5초 목표 | resident warm/없음/pressure refusal 별도 계측, selector 시간 분리 |
| exact vector 연산 | 현재 `MAX_FLOAT_WORK=4,000,000` 상수는 있으나 `search_exact(max_float_work=None)`가 기본이며 product는 deadline을 준다. 목표의 명시적 work 예산으로 4,000,000을 제안 | 1,800×768=1,382,400; 7,200×768=5,529,600은 제안 예산 초과이며 현재 기본 hard cap을 뜻하지 않음 |
| 명시적 vector 예산 초과 | lexical 우선, semantic 단계는 budget-deferred; 새 cap의 품질 영향은 별도 검증 | 1/2/4 chunks, 768/2048차원에서 under/over 경계 시험 |
| bounded scan/fill | catalog 기본 200 work units·5 MB·2,000 stats; fill 8개·2 MB를 출발점으로 사용 | 한 body+한 set 메모리, cursor replay, 큰 단일 문서의 예외 계약 검증 |
| seal/publish | seal은 stream, head CAS 목표 50 ms/상한 200 ms | full manifest 검증과 CAS 시간 분리. SQLite busy는 defer |
| migration/GC step | 100 cards 또는 8 MB, 최대 1초를 제안 | FTS·vector bytes 포함, cursor commit 뒤 yield; 초과 시 smaller batch |
| migration 공간 | old DB+new DB+WAL+예비 공간 사전 계산, 부족하면 시작/추가 copy 보류 | 실제 파일 크기와 예상 copy 크기로 계산, 고정 2배 보장 주장 금지 |
| idle 서비스 | DB/view 추가 때문에 모델 instance 추가 없음 | 2 projects·2 worktrees의 service binding과 worker count 관측 |

제안 vector float-work 예산 초과를 해결하기 위해 ANN이나 임의 chunk 절삭을 이 PR군에 섞지 않는다.
먼저 lexical degradation을 검증하고 exact-search 최적화는 별도 예산/품질 PR로 판단한다.
`MAX_CANDIDATES=20,000`은 문서 chunk-set 경계이고 전체 generation card cap이 아니다.
query의 `max_candidates`도 현재 기본은 None이며 명시한 caller만 제한한다.
`MAX_GENERATION_SEAL_ROWS=None`인 streaming seal 계약을 유지한다. 현재 lifecycle의
`MAX_CARDS=2,048`에 1,800장은 가까우므로 그 이상은 명시적 deferred로 관측하고, chunk 수와
card cap을 혼동하거나 삭제로 해소하지 않는다. cap 변경은 별도 검증 대상이다.

writer는 store별 하나이며 서로 다른 view도 짧은 SQLite write transaction을 공유한다.
읽기는 WAL snapshot을 사용하고 긴 writer/model 작업과 겹쳐도 기존 세대를 유지한다.
resident service의 model load serialization·peer 검증·admission·backoff를 재사용한다.
learning inference 공유는 DB content/provenance를 프로젝트 간 노출하는 권한이 아니다.
학습 sample/export는 pinned view/gen에 귀속하고 source body를 memvane-db에 보관하지 않는다.

## 11. 실패 모드와 기대 결과

| 실패/경쟁 | 검색 | 쓰기·복구 |
|---|---|---|
| partial scan, 원문 대량 소실, 미마운트 | 기존 현재-view generation 유지 | complete/identity proof 전 삭제 금지; 25% guard는 마지막 단계 |
| A에서 compile 중 B로 checkout | B의 view만 제공; capture race면 bounded retry | A job은 publish/unlink 전 capture 불일치로 보류; B head 불변 |
| 같은 SHA를 두 worktree가 동시에 요청 | 각 view 결과 독립 | content lease와 PK가 set을 하나만 게시; 불일치 결과는 conflict |
| 원문 지문은 새것, compile 미완료 | 이전 검증 카드와 이전 compile provenance 검색 | source observation만 갱신; trust·card_sources를 최신 SHA로 위장하지 않음 |
| fingerprint/frontmatter 없음, shallow/unknown commit | 검증 가능한 기존 카드 검색; trust 불명은 제외 | 삭제 hold, 재검증. 원래 commit을 현재 HEAD로 채우지 않음 |
| model unavailable, resource pressure, oversized member | lexical view 유지 | vector fill만 deferred/blocked, cache/cursor 보존 |
| blob/set hash 손상 | 손상 후보 제외, lexical로 가능한 안전 결과 유지 | candidate quarantine·명시적 repair; 같은 PK 덮어쓰기 금지 |
| SQLite busy/disk full/copy crash | 선택된 old endpoint 유지 | 짧은 transaction rollback, cursor replay; 불완전 새 DB 선택 금지 |
| sidecar newer than copied v6 generation | v6의 실제 shadow 결과 유지 | effective endpoint parity까지 catch-up; stale path 부활 금지 |
| bundle fence/companion-host cache 변화 | bridge의 증명된 현재-view reader 유지 | writer fence, intent 복구. 구 host 존재 시 schema 전환 보류 |
| endpoint CAS 직전/직후 crash | old 또는 new 하나의 완전한 reader tuple | journal/intent reconcile, mixed incarnation·seal 조합 거절 |
| worktree 이동·삭제 후 경로 재사용 | binding 일치 view만 검색 | 새로운 UUID 또는 명시 rebind; old view 자동 GC/삭제 금지 |
| cleanup 중 old reader 존재 | pin한 old store에서 요청 완료 | exclusive lease 실패 시 cleanup defer, 프로세스 종료 없음 |
| source skipPaths/policy 변경 | 새 policy를 LIMIT 전 적용; 제외 source 카드 누출 금지 | 재seal. 정책 변경을 raw 소실·삭제 증거로 사용하지 않음 |

진단은 `view_id`, `worktree_id`, branch key, abbreviated HEAD, gen/endpoint epoch,
cache hit/miss, pending 이유, guard 판정과 migration phase를 남긴다. prompt/raw 본문과 비밀은
남기지 않는다. 사용자 안내는 자연어 또는 노출된 skill로 제공하며 내부 maintenance 명령을
새로운 public 운영 인터페이스로 만들지 않는다.

## 12. PR 크기의 rollout과 검증

각 구현 PR은 **회귀 테스트 먼저 작성 → 그 PR의 base에서 실패 출력 보관 → 구현 → focused →
전체 suite** 순서다. 아래는 미래 구현의 검증 계획이며 이 설계 PR이 테스트를 실행했다는
뜻이 아니다. fixture는 임시 realpath HOME·runtime/state root만 사용한다. 일반 테스트는
development guard·파일 병렬도 1·유한 timeout을 유지한다. `development_job_busy`는 대기 후
재시도하고 hold·OS 관측 거부는 우회하지 않는다. 모델 실험은 별도 model-test 가드다.

| PR | 범위·완료 조건 | 먼저 실패시킬 테스트 / 기존 회귀 |
|---|---|---|
| 1. checkout identity와 view capture | 새 data path 미선택. Git facts, ref/HEAD/index/operation capture, stable worktree binding API | 같은 common dir의 다른 worktree 분리, 같은 branch의 다른 lineage, detached/unborn/non-Git, env 오염, case alias, path reuse. `test/laya-index-host-route.test.mjs`·새 view identity suite |
| 2. immutable blob/recipe/provenance schema | 별도 candidate format, no raw body API, 동일 bytes 한 row, 명시 recipe/profile | rename/path 없는 heading, byte 충돌, 중복 insert, malformed/partial sets, raw ingestion 거절, provenance가 다른 같은 blob. `test/laya-index-a.test.mjs`·`test/laya-index-vector.test.mjs`와 새 store suite |
| 3. view generation과 lexical reader | 한 view head CAS, FTS/trust/source seal, 기존 v6 reader 유지 | A 편집 후 B 검색 불변, view filter 이전 LIMIT, pin 중 head swap, zero/held source, skipPaths, FTS CJK parity, legacy bridge query. `test/laya-source-catalog.test.mjs`·`test/laya-fts-text.test.mjs` |
| 4. 공유 vector fill·coverage seal | caller synthetic path recipe, content lease, bounded cursor, lexical 우선 | 같은 content 두 views의 provider call 1회, title fallback rename, recipe/profile 변경 재embedding, no-op fill write-fence 0회, incomplete set·provider failure·float-work degradation. `test/laya-vector-capacity.test.mjs`·`test/laya-incremental-query.test.mjs` |
| 5. journal reader endpoint와 fence bridge | v6/new reader capability, writer-only upgrade fence, endpoint CAS/old lease | fence 모든 단계에서 동시 recall, stopped fence, activate 후 crash, route/journal reorder, bundle drift 허용 범위, tamper 거절, legacy Hermes/older host 보류. `test/laya-journal-v4.test.mjs`·`test/laya-active-upgrade.test.mjs`·`test/laya-bundle-auto-upgrade.test.mjs` |
| 6. migration shadow/catch-up | v6+completed sidecar effective view copy, no automatic switch, parity receipt | main-only import, sidecar shadow 우선, copy 도중 edit, crash cursor, copy 중 disk full/busy, raw bytes/FTS 부재, unknown commit import hold, old/new query 동등성. 새 migration suite와 `test/laya-incremental-docs.test.mjs` |
| 7. view 선택·증명된 writer 활성화 | bridge host만 새 route, linked worktree도 자기 view, main fallback 제거 | cold view preparing, same-HEAD dirty worktrees, A/B switch during search·compile, no-source-reread recall, main/linked settings 불일치. Claude/Codex/Hermes hook payload·gate·observer 회귀, `test/laya-host-response.test.mjs`·`test/wiki-selected-laya.test.mjs` |
| 8. DB provenance compile/lifecycle | compile intent recovery, DB source refs, same-worktree+ancestor deletion | missing raw hold/retire 행렬, sparse/index-only deletion, rebase/merge, mixed-source refresh, fingerprint 없음 hold, 25%/3장 경계·cap 우회 방지, archive/unlink crash. 새 lifecycle fixture와 기존 wiki lifecycle/source-retire suite |
| 9. frontmatter-free 카드 writer | 모든 consumer의 DB provenance 사용, 파일 재작성은 명시 요청만 | provenance 없이 trust 불가, source span/hash/skipPaths 위조, fingerprint-free compile→recall→dedup→learning, 구 host downgrade 거절. 기존 source-spans·compile·dedup suites |
| 10. retention/GC·운영 확대 | 사용자 retention 결정 뒤 mark-copy, 새 seal, endpoint swap, old lease cleanup | 다른 view refs·active reader·job cache 보존, missing worktree hold, orphan 유예, compaction crash와 no-space, old/new parity. 1,800장/10 views 및 2,048 초과 synthetic budget report |

PR 5의 fence bridge는 PR 6/7 활성화에 선행한다. PR 8은 같은-worktree 지문을 신뢰할 수 있는
경우부터 적용하고 unknown legacy provenance는 계속 보류한다. PR 9/10은 bridge rollout·
rollback 종료와 사용자 선택이 끝나기 전 자동 실행하지 않는다. 데이터 schema와 bundle
버전은 각각 검토하며, 실제 code PR만 저장소의 7개 manifest 버전 규칙을 따른다.

release gate는 다음 증거를 요구한다:

- 두 branch·두 worktrees에서 동일 내용 embedding 재사용, 한 branch 편집 뒤 다른 view
  mapping/검색 불변, 내용 복귀 cache hit와 새 content 1회 embed;
- 모든 publish/migration/fence crash point에서 이미 게시된 현재-view 검색이 유지되고
  다른 view 결과·raw body가 반환/저장되지 않음;
- source deletion의 same-worktree/known-ancestor/path-absence 조건과 25% 최종 가드;
- 모든 host의 bridge capability, journal/route/store tamper 거절, rollback 제한과 cleanup lease;
- fixture 기준 latency/bytes/vector work/DB 성장, full suite 결과와 base 실패 출력.

## 13. 사용자에게 남기는 질문

확정 조건을 다시 묻는 항목이 아니라, 구현/배포 정책에 필요한 선택이다. 답변 전에는 위의
보수적 기본값을 설계 가정으로만 사용하며 자동 activation/cleanup을 허가한 것으로 보지 않는다.

1. **처음 보는 branch/worktree의 첫 검색:** 검증된 lexical snapshot까지 `view_preparing`
   무컨텍스트를 허용하는가? 반드시 첫 호출부터 결과가 필요하면 checkout 전 prewarm과
   제한된 동기 wiki scan 중 어떤 인터페이스를 원하는가? 기존 view 업데이트는 어느 선택에서도
   검색을 유지한다.
2. **공유 범위:** 같은 common Git dir의 같은 프로젝트까지만 blob을 공유하는가, 아니면
   별도 clone/다른 프로젝트에도 content store를 공유해야 하는가? 후자는 권한·GC·설치 binding
   범위를 넓히므로 첫 rollout에서는 제외하는 안을 제안한다.
3. **보존·GC:** 현재 heads 외에 과거 세대/미게시 cache/previous reader/삭제 worktree를
   각각 얼마나 보존하는가? 기본 제안은 정책 확정 전 자동 GC 비활성, worktree unregister와
   old rollback DB cleanup은 명시 요청이다.
4. **미커밋 파일의 삭제:** tracked 파일의 로컬 unlink·staged deletion·untracked 파일 소실을
   committed deletion 전까지 hold하는 보수적 정책을 적용하는가? 더 빠른 반영이 필요하면
   같은-worktree/계보 조건을 유지한 명시적 사용자 삭제 receipt를 설계해야 한다.
5. **frontmatter 표현과 Git diff:** source SHA/HEAD/worktree 지문을 DB로 옮긴 뒤 카드 파일에
   사람이 읽는 source path/link는 유지하는가? 기존 카드 일괄 재작성은 하지 않고 신규/갱신
   카드부터 적용하는가? DB를 동반하지 않은 wiki checkout의 자동 trust는 허용하지 않는다.
6. **임베딩 입력과 초기 backfill:** 경로 독립 synthetic filename과 새 recipe를 사용하면
   일부 legacy title 기반 벡터는 재생성이 필요하다. lexical 검색을 계속 제공하며 천천히
   backfill하는 안을 적용하는가? 모델/provider 자체 변경은 이 선택에 포함하지 않는다.
7. **host 호환·rollback 창:** 사용 중인 모든 Claude/Codex/Hermes가 bridge를 로드한 뒤
   schema를 전환하고, frontmatter-free 카드 활성화 이후에는 구 bundle 완전 downgrade 대신
   bridge reader rollback만 제공하는 안을 적용하는가? rollback 보존 기간도 함께 정해야 한다.

## 14. 사용자 결정 (2026-11-01)

13절의 질문은 모두 이 문서의 기본 제안으로 확정한다.

1. 처음 보는 branch/worktree의 첫 검색은 검증된 lexical snapshot이 준비될 때까지 `view_preparing` 무컨텍스트를 허용한다. 기존 view 업데이트는 검색을 유지한다.
2. blob 공유 범위는 같은 common Git dir의 같은 프로젝트다. 별도 clone·다른 프로젝트 공유는 첫 rollout에서 제외한다.
3. 보존 정책 확정 전 자동 GC는 비활성이다. worktree unregister와 이전 rollback DB 정리는 명시 요청으로만 한다.
4. tracked 파일의 로컬 unlink·staged deletion·untracked 소실은 committed deletion 전까지 hold한다.
5. source SHA·HEAD·worktree 지문은 DB가 소유하고, 사람이 읽는 source path/link는 카드에 유지한다. 기존 카드는 일괄 재작성하지 않고 신규·갱신 카드부터 적용한다.
6. 경로 독립 recipe로 바뀌는 legacy 벡터는 lexical 검색을 유지하며 천천히 backfill한다.
7. 모든 Claude/Codex host가 bridge를 로드한 뒤 schema를 전환하고, frontmatter-free 카드 활성화 이후의 rollback은 bridge reader rollback으로 제한한다. rollback 보존 기간은 PR 10의 retention 설계에서 정한다.
