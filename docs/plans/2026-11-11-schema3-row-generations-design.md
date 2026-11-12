# Schema3 증분 세대 저장 설계

상태: 사용자 승인된 설계. 구현 및 운영 적용 전이다. 범위는 schema3 view store다. v6와 journal record·migration intent record·compile dedup key·lock·hook action·wake kind는 바꾸지 않는다. 현재 rollback journal을 유지하고 WAL과 lifetime EX 차용을 사용하지 않는다. 수치 목표와 구 bundle 거절은 실제 새 형식 구현 전 실증되지 않았다.

## 모델과 불변식

각 view는 현재 head와 그 바로 전 published 세대만 데이터로 보존한다. 세대 ordinal은 전역 단조 증가, view epoch는 게시당 1 증가한다. 카드·원문·카드 원문 관계는 반열린 구간 `[gen_from, gen_to)`이며 `NULL`은 현재까지 유효함을 뜻한다. view/path마다 한 시점에 유효한 행은 최대 1개다. rollback은 마지막 게시의 차이를 역적용하는 **새 게시**이며 이전 head 번호로 역행하지 않는다. 연속 rollback은 A→B→A→B이며 이후 sync는 현재 파일을 재관측하여 통상 게시하므로 rollback이 파일 변경을 영구 차단하지 않는다.

FTS는 각 view의 현재 head만 포함한다. 일반 세대는 eligible 카드 corpus 하나, legacy 세대는 base·sidecar 두 corpus를 각각 보존하고 inactive/untrusted 행도 기존 BM25 통계에 필요한 만큼 남긴다. 한 view의 FTS는 다른 view가 변경하지 않는다. 직전 세대는 직접 검색하지 않고 rollback에만 쓴다. 공개 pin은 head에 결속하고 검색 트랜잭션에서 동일 head·epoch를 다시 확인한다. stale이면 `view_head_changed`로 종료하고 상위 호출은 전체 recall을 한 번 다시 pin한다. 두 번째 변경·busy·deadline은 기존 fail-closed 정책으로 반환하며 다른 view로 대체하지 않는다. 본문·trust·벡터·locator는 같은 읽기 transaction에서 가져온다. 기존 짧은 reader transaction의 SH와 SQLite rollback journal이 게시 commit을 직렬화한다. 장기 pin은 과거 결과의 지속 열람 보장이 아니며 다음 검색에서 stale을 감지한다.

## 변경 relation DDL

아래는 변경 대상 relation의 논리적 SQL 계약이다. 기존 `store_meta`, `worktrees`, `wiki_blobs`, `blob_admissions`, recipe/profile 및 content set은 identity·검증을 유지하며, `store_meta.schema_version`, `PRAGMA user_version`, 완전한 DDL manifest를 새 형식으로 함께 바꾼다. `store_meta`와 `worktrees`의 기존 식별 열 이름·의미는 보존한다. 구 형식과 새 형식의 validator를 별도 manifest로 유지한다. 아래 `BLOB`은 실제 binary bytes, ID·digest는 정규 hex TEXT다. CHECK, FK, index, trigger를 포함한 실제 manifest를 구현 전에 고정한다.

```sql
CREATE TABLE s_generations (
  ordinal INTEGER PRIMARY KEY, gen_id TEXT NOT NULL UNIQUE,
  view_id TEXT NOT NULL REFERENCES views(view_id), epoch INTEGER NOT NULL CHECK(epoch>0),
  prior_ordinal INTEGER, kind TEXT NOT NULL CHECK(kind IN ('normal','legacy','rollback')),
  sidecar_present INTEGER NOT NULL CHECK(sidecar_present IN (0,1)),
  capture_json TEXT NOT NULL CHECK(json_valid(capture_json)),
  policy_digest TEXT NOT NULL, skip_digest TEXT NOT NULL,
  recipe_id TEXT REFERENCES chunk_recipes(recipe_id),
  profile_id TEXT REFERENCES vector_profiles(profile_id),
  coverage TEXT NOT NULL CHECK(coverage IN ('lexical','complete')),
  member_digest TEXT NOT NULL, source_digest TEXT NOT NULL,
  logical_fts_digest TEXT NOT NULL, physical_fts_digest TEXT NOT NULL,
  member_acc TEXT NOT NULL CHECK(json_valid(member_acc)), fts_acc TEXT NOT NULL CHECK(json_valid(fts_acc)),
  legacy_json TEXT CHECK((kind='legacy')=(legacy_json IS NOT NULL) AND (legacy_json IS NULL OR json_valid(legacy_json))),
  seal_digest TEXT NOT NULL, UNIQUE(view_id,epoch), UNIQUE(view_id,ordinal), UNIQUE(gen_id,view_id),
  CHECK((recipe_id IS NULL)=(profile_id IS NULL)),
  CHECK(coverage='lexical' OR recipe_id IS NOT NULL)
);
CREATE TABLE s_heads (
  view_id TEXT PRIMARY KEY REFERENCES views(view_id), ordinal INTEGER NOT NULL,
  epoch INTEGER NOT NULL CHECK(epoch>0),
  FOREIGN KEY(view_id,ordinal) REFERENCES s_generations(view_id,ordinal)
);
CREATE TABLE s_cards (
  view_id TEXT NOT NULL REFERENCES views(view_id), path TEXT NOT NULL COLLATE BINARY,
  gen_from INTEGER NOT NULL, gen_to INTEGER, blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  title TEXT NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
  trust_digest TEXT, metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  PRIMARY KEY(view_id,path,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from),
  FOREIGN KEY(trust_digest,blob_sha) REFERENCES card_attestations(trust_digest,blob_sha)
);
CREATE UNIQUE INDEX s_cards_open ON s_cards(view_id,path) WHERE gen_to IS NULL;
CREATE INDEX s_cards_gc ON s_cards(view_id,gen_to) WHERE gen_to IS NOT NULL;
CREATE TABLE s_source_content (
  content_id TEXT PRIMARY KEY, source_sha TEXT NOT NULL, byte_length INTEGER NOT NULL CHECK(byte_length>=0),
  UNIQUE(source_sha)
);
CREATE TABLE s_source_observations (
  observation_id TEXT PRIMARY KEY, worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
  branch_key TEXT NOT NULL, path TEXT NOT NULL COLLATE BINARY,
  tracked_state TEXT NOT NULL CHECK(tracked_state IN ('tracked','untracked','unknown')),
  binding_state TEXT NOT NULL CHECK(binding_state IN ('observed','legacy_unbound')),
  content_id TEXT REFERENCES s_source_content(content_id),
  legacy_fingerprint_id TEXT REFERENCES source_fingerprints(fingerprint_id),
  CHECK((content_id IS NULL)<>(legacy_fingerprint_id IS NULL)),
  UNIQUE(worktree_id,branch_key,path,tracked_state,binding_state,content_id),
  UNIQUE(worktree_id,branch_key,path,tracked_state,binding_state,legacy_fingerprint_id)
);
CREATE TABLE s_attestation_sources (
  trust_digest TEXT NOT NULL REFERENCES card_attestations(trust_digest),
  ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<64),
  observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
  spans_json TEXT NOT NULL CHECK(json_valid(spans_json)),
  PRIMARY KEY(trust_digest,ordinal), UNIQUE(trust_digest,observation_id)
);
CREATE TABLE s_sources (
  view_id TEXT NOT NULL REFERENCES views(view_id), path TEXT NOT NULL COLLATE BINARY,
  gen_from INTEGER NOT NULL, gen_to INTEGER, observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
  PRIMARY KEY(view_id,path,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from)
);
CREATE UNIQUE INDEX s_sources_open ON s_sources(view_id,path) WHERE gen_to IS NULL;
CREATE INDEX s_sources_gc ON s_sources(view_id,gen_to) WHERE gen_to IS NOT NULL;
CREATE TABLE s_card_sources (
  view_id TEXT NOT NULL, card_path TEXT NOT NULL COLLATE BINARY,
  source_path TEXT NOT NULL COLLATE BINARY, gen_from INTEGER NOT NULL, gen_to INTEGER,
  observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
  spans_json TEXT NOT NULL CHECK(json_valid(spans_json)),
  PRIMARY KEY(view_id,card_path,source_path,gen_from),
  CHECK(gen_to IS NULL OR gen_to>gen_from), FOREIGN KEY(view_id) REFERENCES views(view_id)
);
CREATE UNIQUE INDEX s_card_sources_open ON s_card_sources(view_id,card_path,source_path) WHERE gen_to IS NULL;
CREATE INDEX s_card_sources_gc ON s_card_sources(view_id,gen_to) WHERE gen_to IS NOT NULL;
CREATE TABLE s_deltas (
  view_id TEXT NOT NULL, ordinal INTEGER NOT NULL, rel TEXT NOT NULL
    CHECK(rel IN ('card','source','card_source','legacy_fts')),
  key_json TEXT NOT NULL CHECK(json_valid(key_json)),
  old_json TEXT CHECK(old_json IS NULL OR json_valid(old_json)),
  new_json TEXT CHECK(new_json IS NULL OR json_valid(new_json)),
  PRIMARY KEY(view_id,ordinal,rel,key_json),
  FOREIGN KEY(view_id,ordinal) REFERENCES s_generations(view_id,ordinal)
);
CREATE TABLE s_delta_blobs (
  view_id TEXT NOT NULL, ordinal INTEGER NOT NULL, rel TEXT NOT NULL, key_json TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('old','new')), blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  PRIMARY KEY(view_id,ordinal,rel,key_json,side,blob_sha),
  FOREIGN KEY(view_id,ordinal,rel,key_json) REFERENCES s_deltas(view_id,ordinal,rel,key_json)
);
CREATE INDEX s_delta_blobs_by_blob ON s_delta_blobs(blob_sha);
CREATE TABLE s_delta_observations (
  view_id TEXT NOT NULL, ordinal INTEGER NOT NULL, rel TEXT NOT NULL, key_json TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('old','new')),
  observation_id TEXT NOT NULL REFERENCES s_source_observations(observation_id),
  PRIMARY KEY(view_id,ordinal,rel,key_json,side,observation_id),
  FOREIGN KEY(view_id,ordinal,rel,key_json) REFERENCES s_deltas(view_id,ordinal,rel,key_json)
);
CREATE INDEX s_delta_observations_by_observation ON s_delta_observations(observation_id);
CREATE TABLE s_stage_jobs (
  view_id TEXT PRIMARY KEY REFERENCES views(view_id), job_id TEXT NOT NULL UNIQUE,
  incarnation TEXT NOT NULL, base_gen_id TEXT REFERENCES s_generations(gen_id), base_epoch INTEGER NOT NULL CHECK(base_epoch>=0),
  capture_json TEXT NOT NULL CHECK(json_valid(capture_json)), policy_digest TEXT NOT NULL,
  scan_digest TEXT NOT NULL, scan_complete INTEGER NOT NULL CHECK(scan_complete=1),
  cursor_path TEXT COLLATE BINARY, state TEXT NOT NULL CHECK(state IN ('collecting','ready','abandon')),
  vector_spec_json TEXT CHECK(vector_spec_json IS NULL OR json_valid(vector_spec_json)),
  discard_relation TEXT CHECK(discard_relation IS NULL OR discard_relation IN ('card_source','source','item','job')),
  discard_key TEXT COLLATE BINARY,
  CHECK((base_gen_id IS NULL)=(base_epoch=0)),
  CHECK((state='abandon' AND discard_relation IS NOT NULL) OR
        (state<>'abandon' AND discard_relation IS NULL AND discard_key IS NULL))
);
CREATE TABLE s_stage_items (
  job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), path TEXT NOT NULL COLLATE BINARY,
  action TEXT NOT NULL CHECK(action IN ('put','delete')), input_sha TEXT,
  blob_sha TEXT REFERENCES wiki_blobs(blob_sha), trust_digest TEXT,
  metadata_json TEXT CHECK(metadata_json IS NULL OR json_valid(metadata_json)),
  PRIMARY KEY(job_id,path), FOREIGN KEY(trust_digest,blob_sha) REFERENCES card_attestations(trust_digest,blob_sha),
  CHECK((action='delete' AND input_sha IS NULL AND blob_sha IS NULL AND trust_digest IS NULL AND metadata_json IS NULL)
     OR (action='put' AND input_sha IS NOT NULL AND blob_sha IS NOT NULL AND metadata_json IS NOT NULL))
);
CREATE INDEX s_stage_items_by_blob ON s_stage_items(blob_sha);
CREATE TABLE s_stage_source_changes (
  job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), path TEXT NOT NULL COLLATE BINARY,
  action TEXT NOT NULL CHECK(action IN ('put','delete')),
  observation_id TEXT REFERENCES s_source_observations(observation_id),
  PRIMARY KEY(job_id,path),
  CHECK((action='put' AND observation_id IS NOT NULL) OR (action='delete' AND observation_id IS NULL))
);
CREATE TABLE s_stage_card_source_changes (
  job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id), card_path TEXT NOT NULL COLLATE BINARY,
  source_path TEXT NOT NULL COLLATE BINARY, action TEXT NOT NULL CHECK(action IN ('put','delete')),
  observation_id TEXT REFERENCES s_source_observations(observation_id),
  spans_json TEXT CHECK(spans_json IS NULL OR json_valid(spans_json)),
  PRIMARY KEY(job_id,card_path,source_path),
  CHECK((action='put' AND observation_id IS NOT NULL AND spans_json IS NOT NULL)
     OR (action='delete' AND observation_id IS NULL AND spans_json IS NULL))
);
CREATE TABLE s_legacy_fts (
  view_id TEXT NOT NULL REFERENCES views(view_id),
  origin TEXT NOT NULL CHECK(origin IN ('base','sidecar')), path_key TEXT NOT NULL COLLATE BINARY,
  gen_from INTEGER NOT NULL, gen_to INTEGER, blob_sha TEXT NOT NULL REFERENCES wiki_blobs(blob_sha),
  title TEXT, position INTEGER, active INTEGER NOT NULL CHECK(active IN (0,1)),
  PRIMARY KEY(view_id,origin,path_key,gen_from), CHECK(gen_to IS NULL OR gen_to>gen_from),
  CHECK((origin='sidecar')=(title IS NOT NULL AND position IS NOT NULL))
);
CREATE UNIQUE INDEX s_legacy_fts_open ON s_legacy_fts(view_id,origin,path_key) WHERE gen_to IS NULL;
CREATE INDEX s_legacy_fts_gc ON s_legacy_fts(view_id,gen_to) WHERE gen_to IS NOT NULL;
CREATE TABLE s_fill_cursors (
  view_id TEXT NOT NULL REFERENCES views(view_id), recipe_id TEXT NOT NULL REFERENCES chunk_recipes(recipe_id),
  profile_id TEXT NOT NULL REFERENCES vector_profiles(profile_id), job_id TEXT NOT NULL,
  head_ordinal INTEGER NOT NULL, target_count INTEGER NOT NULL CHECK(target_count>=0),
  cursor INTEGER NOT NULL CHECK(cursor>=0 AND cursor<=target_count), PRIMARY KEY(view_id,recipe_id,profile_id)
);
CREATE TABLE s_head_fts_map (
  view_id TEXT NOT NULL REFERENCES views(view_id), origin TEXT NOT NULL CHECK(origin IN ('base','sidecar')),
  path_key TEXT NOT NULL COLLATE BINARY, fts_rowid INTEGER NOT NULL,
  PRIMARY KEY(view_id,origin,path_key), UNIQUE(view_id,origin,fts_rowid)
);
CREATE TABLE s_parity_anchor (
  view_id TEXT NOT NULL REFERENCES views(view_id), gen_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, incarnation TEXT NOT NULL, receipt_digest TEXT NOT NULL,
  proof_digest TEXT NOT NULL, PRIMARY KEY(view_id,gen_id)
);
```

`prior_ordinal`는 과거 세대 전체를 FK로 붙잡지 않는다. sealed 현재·직전 연결만 검사하고 숫자만으로 신뢰하지 않는다. 각 view의 FTS 이름은 검증된 view ID에서 만든 고정 접두사+hex로만 생성하고 식별자를 SQL에 임의 삽입하지 않는다. 일반 FTS와 legacy base/sidecar FTS의 tokenizer·columns·weights·MATCH·row mapping은 기존 `laya_view_fts`와 같다. legacy 세대의 FTS corpus는 `s_legacy_fts`의 구간 행이다. 한 행은 origin(base/sidecar)·FTS 전체 경로·immutable blob·sidecar 제목과 원래 rowid 순서·active 여부이고, 카드가 아닌 inactive/untrusted 행도 BM25 통계를 위해 같은 구간 규칙으로 남는다. 카드 경로는 `s_cards`, FTS 전체 경로는 `s_legacy_fts`에 두고 두 경로 체계를 한 relation에 섞지 않는다. legacy 카드의 FTS 경로·origin·원문 줄 span은 그 카드 행의 `metadata_json`에 있다. head의 legacy FTS 행은 `s_legacy_fts` 열린 행과 blob 본문에서 다시 만들고 seal의 논리·물리 digest로 대조한다. 따라서 delta가 GC된 뒤에도 head corpus를 다시 증명할 수 있다. legacy 세대의 `member_digest`는 카드 행과 legacy FTS 행을 함께 결속한다. `legacy_fts` delta는 path·origin·본문/FTS 입력의 immutable blob 참조를 저장하며 raw 원문은 넣지 않는다. 최초 legacy 및 vector-only 게시에서 이전과 동일한 corpus를 보존한다.

`sidecar_present`는 현재 head의 sidecar FTS 존재 여부를 명시해 전체 FTS schema manifest와 seal이 같은 corpus 집합을 검증하도록 한다. 이 플래그만으로 legacy 원본이나 content 증명이 성립하지는 않는다.

SQL 트리거와 기존 connection guard는 (1) 구간 역행·중첩과 두 open 행, 임의 값 수정·삭제, 타 view 참조, (2) head가 sealed 최신 ordinal이 아니거나 epoch가 정확히 +1이 아닌 변경, (3) 현 head·직전 세대 및 진행 중 fill의 행 삭제, (4) seal·delta의 수정과 다른 대상에 대한 임의 재사용을 거절한다. 구간 종료 외의 UPDATE는 금지한다. 삭제 가능 여부는 현재 참조를 같은 writer transaction에서 검사하며, 일반 API는 삭제를 내보내지 않는다. 가드가 SQLite 외부 연결을 막는 보안 경계라고 주장하지 않는다. `s_generations`의 seal은 incarnation·view·ordinal·epoch·prior·capture·policy·skip·recipe/profile·coverage·member/source의 정렬 digest·logical FTS digest·두 corpus의 physical digest를 결속한다. legacy observation도 seal에 포함한다. `stage_json`·manifest 전체·files 전체는 영속 세대마다 저장하지 않는다. seal 계산은 해당 구간의 행을 순회하되 전량 사본을 만들지 않는다.

## 원문·trust·vector fill

새 content ID는 SHA-256의 도메인 구분자와 관측된 원문 bytes의 digest만으로 계산한다. `s_source_content`는 content hash와 길이만 보관하고, `s_source_observations`는 worktree·branch·path·tracked/binding 상태와 content 또는 기존 legacy fingerprint를 별도 결속한다. 동일 bytes라도 tracked와 untracked 경로는 서로 다른 observation이며 content만 공유한다. 새 `s_attestation_sources`는 observation을 참조해 경로와 spans를 구분하고 기존 `attestation_sources`는 구 receipt 검증에 그대로 남긴다. 두 버전의 trust digest는 domain을 구분하고 동일 receipt를 양 테이블 방식으로 동시에 해석하지 않는다. HEAD·scan·시각은 content와 observation의 정체성에 넣지 않되 해당 세대의 capture에 기록하고 게시 직전 현재 worktree/HEAD/index·완전한 scan 결과와 observation을 재검증한다. 경로·tracked 상태 변경은 새 observation 및 membership 변경이다. HEAD만 변경되고 bytes와 경로 상태가 같으면 content·observation·source interval을 다시 쓰지 않는다. 삭제는 완전한 scan에서만 닫고 부분·실패 scan은 보류한다. 해시 동일·길이 불일치는 거절하지만 원문 bytes를 영속하지 않으므로 서로 다른 bytes의 SHA-256 충돌을 독립적으로 탐지한다고 주장하지 않는다. 기존 legacy fingerprint와 attestation은 변환 중 바꾸지 않고 구 verifier로 증명한다. 새 receipt는 source membership의 scope·capture 증거를 별도로 결속해 다른 view의 trust를 빌리지 않는다.

새·구 provenance 구분, 동일 내용 두 경로의 tracked 상태와 spans, rename/delete, HEAD 전진, partial scan, 기존 `rebound`의 선행 trust chain 및 변환 후 구 receipt 검증을 실제 schema의 foreign key·트리거와 함께 검사한다. 동일 `content_id`에 서로 다른 observation을 두는 것이 허용되지만 같은 trust에서 같은 observation 중복은 거절한다. `s_attestation_sources`와 구 `attestation_sources`가 같은 trust에 섞이는 것은 트리거와 verifier가 거절한다. source/attestation 삭제 시 두 relation과 stage·delta의 참조를 모두 확인한다.

`observation_id`는 도메인 구분된 canonical `(worktree_id,branch_key,path,tracked_state,binding_state,content_id 또는 legacy_fingerprint_id)`의 digest다. `s_sources.path`는 참조 observation의 path와 같고, 해당 view의 worktree/branch와 observation이 같아야 한다. `s_card_sources.source_path`도 같은 규칙이며 `(card_path,source_path)`와 해당 view에서 유효한 카드·원문 구간을 가리켜야 한다. INSERT/종료 및 publish verifier는 이 관계와 byte-length 내 spans를 직접 검사한다. SQLite UNIQUE는 NULL을 서로 다르게 취급하므로 위 복합 UNIQUE만으로 XOR 양쪽의 중복을 막았다고 주장하지 않는다. canonical ID 재계산, duplicate-identity trigger 및 실제 schema 검사로 같은 legacy/content observation의 중복을 거절한다. `source_content`의 content ID는 원문 SHA/길이 규칙을 재검증하며, tracked 여부는 공유 content가 아니라 observation에서 읽는다.

형식 4의 fill cursor는 view·spec마다 `s_fill_cursors` 한 행이다. 그 행은 현재 head ordinal, 결정적 job ID(incarnation·view·head·spec·대상 digest의 digest), 대상 수와 cursor를 담고, head나 spec이 바뀌면 같은 행을 새 identity로 덮어쓴다. 대상은 head의 eligible 카드 경로 순서이며 대상 행은 저장하지 않는다. `fill_status`·`fill_view`의 응답 필드(`job_id`·`cursor`·`complete`·`status`·`filled`·`cache_hits`·`deferred`)와 호출자 예산·deferred 의미는 형식 3과 같고, 형식 4에서는 호출자의 manifest를 읽지 않는다. stage job은 게시할 세대의 vector spec을 `vector_spec_json`에 담고 세대 ID가 그 spec을 결속한다. 벡터는 `(blob_sha, recipe_id, profile_id)`의 불변 완성 set을 재사용하고 head 세대는 spec과 `lexical` 또는 `complete`만 기록한다. `complete` 봉인 전 모든 eligible 카드가 같은 spec의 완전한 set을 갖는지 검증한다. 개별 카드별 vector 세대 사본은 만들지 않는다. 현 `fill_targets`와 무기한 job receipt 대신 view·spec별 유계 cursor 1개를 두고 순회 종료 시 누락 set이 있으면 처음으로 재순회한다. 기존 content OS lease·claim·set 검증은 유지하고 callback은 SQLite transaction 밖이다. head 없는 cold view는 새 head를 게시하기 전에 lexical head를 먼저 게시한 뒤 fill한다. head 변경 중 fill은 기존 set만 재사용하고 새 head의 eligibility·capture를 다시 확인한 뒤 complete 세대를 게시한다. 진행 중 fill의 claim·임시 content는 GC 대상에서 제외한다. `fill_status` 소비자는 job ID 고정 재호출 대신 현재 head/spec의 누락 set과 cursor를 사용하도록 함께 바꾼다. 이 API 의미 변경은 PR8과 구 번들 교차 검증 대상이다.

## 게시·rollback·read

### 64장 초과와 cold view의 durable stage

각 view에 active `s_stage_jobs`는 하나이며 기존 head/epoch(없으면 NULL/0), incarnation, capture·policy와 완전한 source/wiki scan digest에 묶인다. `s_stage_items`는 기존 head와 다른 카드 또는 삭제만 기록한다. cold view라면 전체 카드가 변경분이므로 한 번에 한 job의 최대 20,000개까지 누적한다. 각 pass는 현재 scan·capture와 job의 identity를 대조하고 binary path 순서에서 cursor 다음 최대 64장 또는 기존 deadline까지 원문 파일을 재관측·ingest한다. 해당 pass의 검증된 action·blob/trust 및 cursor는 한 SQLite transaction에서 commit한다. 동일 path replay는 모든 정규화 값이 같을 때 no-op이며 다른 값은 stage를 폐기한다. `s_stage_source_changes`와 `s_stage_card_source_changes`는 같은 기준 관측의 변경분만 저장하고 그 참조는 GC root다. 동일 head의 여러 pass마다 전체 manifest/files를 누적해 복사하지 않는다.

pass 시작과 cursor commit 직전 capture·HEAD/index·settings·source/wiki 목록과 각 처리된 바이트를 다시 확인한다. 다음 pass에서는 보존된 item 각각의 path·현재 bytes hash·trust와 source observation이 현재 관측과 같은지 재확인한다. scan digest는 각각의 정렬된 `(path,bytes digest,tracked status)`를 결속하며 raw 본문은 저장하지 않는다. 검사 실패·부분 scan·동일 path 내용 변경·head/epoch 또는 worktree 변경은 아무 head도 게시하지 않고 stage를 `abandon` 처리한다. stage의 참조를 지우기 전 active writer/claim을 확인하고 action·source 관계·item→job 순으로 유계 batch에서 삭제한다. `abandon` 상태와 `discard_relation`/`discard_key` 폐기 cursor는 crash 뒤 다시 실행할 수 있어야 하며 누락 상태에서 stage를 참조하는 head를 발급하지 않는다. 같은 view의 이전 job이 완전히 삭제된 뒤에만 기존 sync wake에서 새 job을 시작한다. 이 상태는 같은 store 안에 두며 새 journal·wake·lock은 아니다.

마지막 pass는 cursor 뒤 누락이 없고 action set·scan digest·old head를 한 transaction에서 재검증한 뒤 그 변경분 전체를 본문 게시 절차의 하나의 transaction으로 반영한다. cold view는 그 commit 전 `view_preparing`이며 기존 head가 있는 view는 끝까지 기존 head를 제공한다. stage가 유효하더라도 commit 전 파일을 다시 관측해 바뀌면 전체 트랜잭션을 rollback한다. staged content의 body·trust와 준비된 source 증거는 job FK에 의해 보존되고, 게시 완료 뒤 stage 삭제는 별도 유계 GC다. 마지막 단계는 전체 변경량만큼 FTS 쓰기·proof를 한 transaction에서 수행하므로 45초 기존 pass budget 안에 임의의 최대 20,000장 cold view가 끝난다는 증거는 **없다**. commit 시작 전에 deadline 초과가 감지되면 직전 head는 유지되고 job을 보존해 명시적 defer한다. 이 final transaction이 반복해서 예산을 넘으면 조치 없이 무한 재시도하지 않고 로그·원인·대안을 사용자에게 보고한다. 따라서 64장씩 준비한 것만으로 최대 크기 view의 게시 가능성을 해결했다고 하지 않는다.

**45초 경계의 실제 의미:** 현 `_publish_schema3`는 시작 시 `SCHEMA3_PASS_SECONDS=45`를 정하고, `_publish_schema3_view`는 ingest에 `SCHEMA3_PUBLISH_RESERVE=15`초를 남기려 한다. `prepare_generation`·`publish_generation`·FTS `storage_digest`는 deadline을 인자로 받아 작업 중 검사하며, publish의 `finish`도 commit 직전에 시간을 확인한다. 그러나 SQLite commit과 OS `fsync` 자체를 선점해 45초 안에 반환시키는 계약은 없다. 따라서 이것은 협력적 deadline이며, 물리적 응답시간의 하드 상한으로 쓰지 않는다. 새 최종 transaction도 같은 deadline을 받는다: 각 staged action과 FTS shadow 순회 전에 남은 예산을 검사하고, SQLite 연산에는 connection별 progress handler를 적용하며, pre-commit deadline 만료·interrupt·busy는 rollback/defer한다. 이미 SQLite commit이 시작된 뒤 deadline이 지나 성공했다면 커밋된 head를 사실대로 기록하고 rollback했다고 보고하지 않는다. SQLite progress handler가 filesystem sync를 선점한다고 가정하지 않는다.

기존 형식의 합성 4천 장에서 마지막 32장 pass는 17.42초, 4천 장 head에서 변경 1장 게시(새 원문 포함)는 13.96초였다. 이는 새 형식의 **4천 장 전체 delta를 하나의 FTS transaction으로 적용하는 시간 측정이 아니다**. 단계별 64장 stage만으로 final commit의 작업량을 나눌 수 없고, head 전용 FTS를 여러 transaction에서 미리 수정하면 중간 상태가 노출된다. 현재 결정된 형식과 단일 게시 atomicity를 유지한 상태에서 45초 내 모든 규모의 완료는 보장되지 않는다. 새 형식 구현 전 합성 1천·4천·2만 장 final transaction과 변경량별 proof/commit을 양 Python에서 측정하고, 반복 deadline 초과가 확인되면 45초 정책·일시적 별도 FTS·게시 범위 등 실제 대안을 사용자 결정으로 가져간다. 아직 새 lock이나 소유 계약 변경이 불가피하다고 결론내리지 않는다.

1. 기존 sync owner와 worker writer scope 및 전후 proof window를 그대로 취득한다. SQLite transaction 밖에서 capture·설정·전체 scan 완결성·변경 후보의 바이트와 원본 파일 identity를 검증한다. unchanged는 head 구성·source membership·설정을 대조한 뒤 판정한다.
2. `BEGIN IMMEDIATE`에서 schema/incarnation·selected endpoint·현재 head/epoch·capture 및 `ready` stage job을 재검증한다. 불일치 또는 busy면 쓰지 않고 기존 wake로 재시도한다. 다음 ordinal을 할당한다. shadow migration도 위의 durable stage를 사용하고 `publication_cut` 아래 이 transaction에서 최종 재검증한다. 검증되지 않은 stage 파일이나 재개 결과는 게시하지 않는다.
3. 변경 path만 기존 open 행을 `gen_to=:new_ordinal`로 닫고 새 값을 INSERT한다. 정확한 old/new delta는 transaction 소유 임시 작업 집합에 기록하고, generation 삽입 뒤 FK를 만족하도록 영속 `s_deltas`에 넣는다. source와 card-source도 동일하다. 임시 작업 집합의 bytes·행 수도 유계로 검사하며 초과하면 전체 transaction을 되돌린다. 필요 content set은 기존 content claim 계약 아래 별도 사전 완료하고 head는 그 완료 여부를 다시 증명한다.
4. 이 view의 FTS에서 변경 카드만 DELETE/INSERT한다. legacy base·sidecar는 origin별로 동일하게 처리한다. FTS의 마지막 쓰기 뒤 공개 savepoint/release를 적용하고 추가 FTS 쓰기를 금한다. 기존 `ConnectionGuard.building`의 내부 commit은 제거하고 호출자가 한 transaction을 소유하도록 조정한다. FTS integrity check와 물리·논리 digest를 계산한다. 공개 SQLite 계약이 commit 이후에도 같은 물리 digest를 보장한다는 증거가 없으므로 양 Python·SQLite에서 actual ViewStore의 commit 전후 및 crash 검증이 통과하지 않으면 이 방식을 사용하지 않는다.
5. 새 generation과 seal을 삽입한 뒤 임시 작업 집합에서 영속 delta를 삽입하고 head를 `(old ordinal,old epoch)` CAS로 `(new ordinal,old epoch+1)`로 전환한다. 변경 행 수가 1이 아니거나 proof·capture 재검증이 실패하면 ROLLBACK한다. `COMMIT` 전 중단은 이전 head, commit 후 중단은 완성된 새 head다. SQLite recovery 오류·`SQLITE_FULL`·잠금 및 공간 부족 시 자동 삭제나 부분 head 발급을 하지 않는다.

rollback 진입은 기존 setup skill의 owner 승인 경로에 추가하는 명시 동작으로 하며 운영 데이터 변경은 별도 사용자 승인 뒤다. 동일 owner·proof 아래 현재 head와 직전 seal 행을 검사하고 현재 head의 `s_deltas`를 역순 적용해 새 ordinal을 만든다. 추가는 닫고 삭제는 새 interval로 다시 열며 수정은 양쪽을 수행한다. FTS도 같은 차이만 역적용한다. 새 세대의 논리 member·FTS digest는 head의 증분 digest에서 delta 행의 기여만 빼고 더해 계산하고, 그 값이 직전 세대의 논리 digest와 같아야 commit한다. rollback은 head 전체를 다시 검증하지 않고 물리 FTS digest도 다시 계산하지 않는다(아래 「seal 증분화」). 봉인 후 head CAS·commit은 일반 게시와 같다. 따라서 rollback이 읽고 쓰는 양은 마지막 delta와 seal 행에 비례하고 corpus 크기와 무관하다. 이 시간 성질은 1천·4천·2만 합성 실측으로 확인하며 GOAL의 네 기준을 완화하지 않는다.

### seal 증분화

`member_digest`와 `logical_fts_digest`는 행 순서와 무관한 증분 multiset hash다. 각 행의 기여는 도메인을 구분한 SHA-256 값이고(카드 행·legacy FTS 행·FTS 입력 행이 각자의 도메인을 쓴다), 누적값은 행 수, 행 바이트 합, 기여값의 2^256 모듈러 합이다. 세대마다 두 누적값을 `s_generations.member_acc`·`fts_acc`에 canonical JSON으로 저장하고, digest는 누적값을 도메인과 함께 한 번 더 해시한 값이며 seal은 누적값과 digest를 함께 결속한다. 게시와 rollback은 head 세대 행의 누적값에서 바뀐 행의 기여만 빼고 더해 새 누적값을 만든다. 프로세스를 다시 시작해도 누적값은 store에 있으므로 corpus를 다시 순회하지 않는다. 일반 세대의 FTS 입력 행은 eligible 카드의 정규화된 FTS 행이고, legacy 세대는 base·sidecar corpus 행을 같은 누적값에 origin을 구분해 더하며 sidecar는 원래 rowid 순서 값을 행 내용에 포함한다. 이 digest는 구조적 결속이며 보안 경계가 아니다. 모듈러 합 multiset hash는 대량의 의도적 행 조작에 대한 충돌 저항을 주장하지 않고, 동일 UID 위조는 기존과 같이 범위 밖이다.

head FTS의 경로별 rowid는 `s_head_fts_map`에 있다. 이 relation은 각 view head의 FTS 행마다 한 행이고 구간 없이 head와 함께 바뀌며, FTS 행을 넣고 지우는 같은 transaction에서 같이 고친다. 게시와 rollback은 이 relation으로 바뀐 경로의 rowid를 찾고 FTS 테이블 전체를 읽지 않는다. pin은 FTS 행과 이 relation의 경로·rowid가 정확히 대응하는지 함께 대조한다.

pin과 검색은 head의 카드 행·legacy FTS 행·blob 본문에서 FTS 행을 다시 만들어 FTS 테이블 내용과 행 단위로 대조하고, 같은 행들로 누적값을 다시 계산해 seal의 누적값과 비교한다. 이 대조는 FTS 테이블의 저장된 열 내용을 확인하며 역색인 postings의 물리 바이트를 대신 증명하지 않는다. 일반 게시는 FTS 쓰기 뒤 FTS5 `integrity-check`를 실행하고, flush한 head FTS의 물리 digest를 계산해 seal에 넣는다. rollback 세대는 `integrity-check`와 물리 digest 계산을 하지 않고 `physical_fts_digest`에 "rollback이 물리 digest를 미룸"을 뜻하는 도메인 구분 값(직전 head의 물리 digest와 rollback 세대 ID의 digest)을 넣는다. 이 세대를 읽는 pin은 FTS 행 단위 대조와 누적값으로 내용을 증명하고 물리 shadow 바이트 대조는 하지 않는다. rollback을 연속으로 하면 그 세대들은 모두 미룸 값을 가지며, 생략은 다음 일반 게시가 `integrity-check`와 물리 digest를 다시 봉인할 때까지 이어진다. 그 사이 역색인 postings의 물리 손상은 pin이 감지하지 못하고 FTS5 질의 오류나 결과 차이로만 드러날 수 있다.
## GC

GC는 sync owner·writer proof 아래 한 transaction당 제한된 행 수와 bytes·deadline으로 수행한다. head 및 직전 ordinal을 재조회해 `:prev`를 고정하고 각 삭제 직전 같은 transaction에서 참조를 다시 확인한다. 다른 view·active claim·content fill·rollback delta·parity anchor·trust chain을 참조하는 객체는 남긴다. 아래는 각 batch의 핵심 SQL이며 실제 코드는 각 테이블 FK 순서와 보호 trigger를 함께 적용한다.

### S v1 보존 범위

S v1은 view 행(`s_cards`·`s_sources`·`s_card_sources`·`s_legacy_fts`)·head FTS·stage/delta·세대만 GC한다. 구간 행은 `gen_to`가 head의 직전 세대 ordinal 이하일 때만 지우고, head와 직전 세대가 보는 행은 삭제 trigger가 거절한다. 현재 head와 직전 세대, 활성 stage·parity 및 필요한 참조를 보호한다. 여러 view가 공유하는 blob 본문·admission·attestation·fingerprint·chunk/embedding set·recipe/profile은 삭제하지 않는다. 따라서 검색 가능한 view는 현재+직전이지만 오래된 공유 본문·벡터는 더 남는다. 공유 content 회수의 활성화 규칙 R1~R6와 경합 재현은 `docs/TODO.md`와 `context/claude-handoff-s-blob-gc-locks-20261112.md`에 보류한다. 보고서는 정적 분석이며 실제 경합 재현·S 구현 검증이 아니다. 새 lock이나 소유 계약 변경은 하지 않는다.

### view 행 GC의 S 구현 RED 후보

임시 설치에서 실제 SQLite transaction과 기존 lock을 사용한다. (1) cold/64장 초과 stage를 pass 사이에 중단·재개하고, 변경·삭제 또는 stale capture가 생기면 stage 폐기 중에도 head가 부분 게시되지 않는지 확인한다. (2) journal CAS 후 switched intent 기록 전 종료와 오래된 shadow prepare 핸들을 교차해 head 변경 전후의 `view_head_conflict` 또는 orphan stage를 관측하고, orphan만 유계 GC한 뒤 head·직전·parity 증거와 FK가 보존되는지 확인한다. 선택 sync 추가 게시와 shadow의 head 관측 순서를 별도로 고정한다. (3) 진행 stage·열린 reader와 충돌하는 view 행 삭제, GC batch의 commit 전후 crash, rollback 직전 세대 및 delta child 원자 삭제를 시험한다. 이 사례들은 구현 전 RED 후보이며 아직 재현되지 않았다.

```sql
DELETE FROM s_card_sources WHERE rowid IN (SELECT rowid FROM s_card_sources
 WHERE view_id=:view AND gen_to IS NOT NULL AND gen_to<=:prev ORDER BY gen_to,rowid LIMIT :batch);
DELETE FROM s_sources WHERE rowid IN (SELECT rowid FROM s_sources
 WHERE view_id=:view AND gen_to IS NOT NULL AND gen_to<=:prev ORDER BY gen_to,rowid LIMIT :batch);
DELETE FROM s_cards WHERE rowid IN (SELECT rowid FROM s_cards
 WHERE view_id=:view AND gen_to IS NOT NULL AND gen_to<=:prev ORDER BY gen_to,rowid LIMIT :batch);
-- 다음 네 문장은 같은 transaction; tmp_gc_delta는 연결 전용 TEMP table.
DELETE FROM temp.tmp_gc_delta;
INSERT INTO temp.tmp_gc_delta(view_id,ordinal,rel,key_json)
 SELECT d.view_id,d.ordinal,d.rel,d.key_json FROM s_deltas d
 WHERE d.view_id=:view AND d.ordinal<>:head AND d.ordinal<>:prev
 ORDER BY d.ordinal,d.rel,d.key_json LIMIT :batch;
DELETE FROM s_delta_blobs WHERE (view_id,ordinal,rel,key_json) IN
 (SELECT view_id,ordinal,rel,key_json FROM temp.tmp_gc_delta);
DELETE FROM s_delta_observations WHERE (view_id,ordinal,rel,key_json) IN
 (SELECT view_id,ordinal,rel,key_json FROM temp.tmp_gc_delta);
DELETE FROM s_deltas WHERE (view_id,ordinal,rel,key_json) IN
 (SELECT view_id,ordinal,rel,key_json FROM temp.tmp_gc_delta);
DELETE FROM s_generations WHERE ordinal IN (SELECT g.ordinal FROM s_generations g
 WHERE g.view_id=:view AND g.ordinal<>:head AND g.ordinal<>:prev
 AND NOT EXISTS(SELECT 1 FROM s_deltas d WHERE d.view_id=g.view_id AND d.ordinal=g.ordinal)
 AND NOT EXISTS(SELECT 1 FROM s_stage_jobs j WHERE j.base_gen_id=g.gen_id)
 ORDER BY g.ordinal LIMIT :batch);
```

`tmp_gc_delta`는 `(view_id,ordinal,rel,key_json)` primary key를 가진 TEMP table이며 각 delta와 모든 정규화 child를 같은 batch transaction에서 함께 지운다. 하나의 delta와 child 전체가 bytes/행 상한을 넘으면 일부만 지우지 않고 defer한다. shared content 및 그 하위 행은 S v1에서 삭제하지 않는다. 각 batch는 rollback 또는 전체 commit이고 다음 wake는 같은 조건을 다시 계산해 재개한다. view 행의 보호 trigger·stage 및 외부 parity 참조 완전성과 시간 상한은 구현 전 검증한다.

## 기존 store 변환과 구 bundle

선택 journal record와 store inode/incarnation을 유지한다. 변환 전 선택된 양 host의 **실행 번들**이 S를 지원하는 같은 release commit을 loaded receipt로 증명하고, journal이 선택한 bundle·attestation과 일치하며 Hermes 사본·업그레이드/state move/project move intent가 없고 기존 owner/worker가 경쟁하지 않는지 확인한다. 기존 receipt가 S 지원 증명에 충분한지는 실제 발급·검증 경로의 교차 시험이 필요하다. 불충분하면 변환하지 않고 사용자에게 보고한다. 필수 backup은 기존 backup guard/검증 절차로 생성하고 공간 및 복구 비용을 확인한다. 변환은 운영 사용자의 승인 절차 뒤에만 수행한다.

새 bundle은 기존 format의 정확한 DDL·identity·head/직전 seal·모든 참조를 우선 검증한다. 기존 sync owner + writer scope 아래 rollback journal의 한 `BEGIN IMMEDIATE`에서 기존 head와 직전 콘텐츠를 구간 relation과 head 전용 FTS로 옮기고 양 세대 간 delta를 만든다. 원래 head가 최초 세대면 직전이 없음을 명시한다. 원래 head/직전 logical digest와 신 형식 집합·legacy corpus가 일치하는지 검사한다. 양 host가 사용하는 구 validator의 선행 metadata 조회에 필요한 `store_meta`·`worktrees` 열을 보존한다. 기존 target+job이 진행 중이면 변환을 보류하고 완료 또는 안전한 재시도를 확인한다. 구 relation 및 세대별 FTS 제거, 새 DDL·head·seal, `user_version`과 `store_meta.schema_version`은 같은 commit이다. 동시 구 reader가 연 transaction을 마치지 못해 busy면 기존 wake로 재시도한다. 변환 commit 이전 실패 시 old schema, commit 후 새 schema만 허용한다. hot journal의 recovery와 권한/sidecar identity는 SQLite·기존 validator의 실제 조건을 검증하며 임의 sidecar 삭제는 금지한다.

`s_legacy_fts`도 형식 4 DDL manifest의 일부이므로 구 bundle은 이 relation을 읽기 전에 header 검사에서 거절한다. 구 bundle의 `_schema3_store`는 버전 검사 전 일부 metadata를 읽지만 이후 `ViewStore.open_reader`의 `BlobStore._header`는 exact version/DDL을 요구한다. 새 형식에서 구 bundle이 content 반환·writer 발급·setup 변경 전에 `candidate_format_mismatch` 등 고정 거절에 도달해야 한다. 단일 저수준 validator 관측은 전체 consumer 증거가 아니다. 실제 새 형식 DB와 고정 구 core로 recall, dedup, compile, sync, setup, linked worktree 및 열린 연결의 다음 transaction을 양 Python과 양 host 경로에서 검사한다. 거절 사유와 파일 바이트·inode를 기록하고, 열려 있던 read transaction이 변환 전 일관된 snapshot을 마치는 경우와 변환 후 신규 접근을 구분한다.

legacy 세대는 v6 관측 값(observation·corpus_observation·sidecar_observation·sidecar_commitment·provenance scan)을 `s_generations.legacy_json`에 canonical JSON으로 담고 seal이 그 값을 결속한다. legacy가 아닌 세대는 이 열이 NULL이다. shadow prepare는 카드 변경을 stage item으로 기록하고, legacy corpus는 stage에 저장하지 않는다. stage의 `scan_digest`가 legacy corpus 행의 digest와 그 관측 값을 결속한다. publish는 publication_cut 아래에서 이미 다시 관측하는 v6 snapshot으로 corpus를 다시 만들어 그 digest와 관측 값을 대조하고, 다르면 `migration_snapshot_changed`로 거절한다. shadow migration은 새 store를 처음부터 새 DDL로 만들고 기존 sealed inventory의 legacy base·sidecar와 observation을 첫 세대로 게시한다. parity는 `6f7f1399`의 원본 sealed inventory 개수와 candidate eligible 개수, 고정 샘플 검색을 동일 pin에서 검사한다. 원본의 모든 경로 동일성까지 증명하는 것은 아니다. 카드 0개일 때 빈 corpus·inventory를 정상 봉인하고 sampled query는 0개다. v6 rollback 뒤 명시 재개는 새 incarnation의 새 candidate와 receipt를 만들며 기존 receipt를 재사용하지 않는다.

현재 `_schema3_parity_floor`는 endpoint 봉인과 v6→schema3 CAS에서 `parity.generation`의 published ordinal을 찾는다. GC는 이를 통째로 제거하지 않는다. 변환 시 기존 generation의 view·id·ordinal·incarnation과 receipt 결속을 `s_parity_anchor`에 보존하고 seal/proof를 검증한다. 새 verifier는 anchor가 기존 published row와 동일한 증명 권한과 floor 관계를 갖는지 확인한다. record가 더 이상 참조하지 않는다는 증거가 있기 전 anchor를 지우지 않는다. anchor 발급 권한과 구 receipt·선택 record·GC의 동등성은 재리뷰·교차 시험 전 미증명이다.

## 검증과 독립 리뷰 차단

### 코드에 근거한 경계와 남은 설계 결정

1. **시간 목표:** GOAL의 검증 기준은 rollback·GC *시간*이 corpus 크기와 무관해야 한다. 증분 seal 전 구현의 합성 실측(py3.13)에서 rollback은 1천·4천·2만 장에서 0.22·0.83·4.27초로 corpus에 비례했고, 원인은 rollback 안의 head 전체 검증, 직전 세대 FTS 행 재계산, 물리 digest 전체 읽기였다. 「seal 증분화」가 이 세 읽기를 delta 비례 계산으로 바꾼다. 같은 변경량에서 1천·4천·2만 장의 rollback 시간을 양 Python으로 다시 측정하고, 기준을 만족하지 못하면 실패로 두고 설계 또는 사용자 결정을 다시 구한다. 평균 시간·변경량 비례 쓰기만으로 시간 기준을 충족했다고 판정하지 않는다.
2. **parity anchor:** 현재 `_parity_receipt`는 shadow intent에서 발급된 candidate 경로·incarnation·generation·digest를 record와 대조하고 `_schema3_parity_floor`는 별도로 원래 published ordinal을 찾는다. receipt만으로 ordinal이 증명되지는 않는다. 변환 전에 실제 구 generation row·seal·record·receipt·view·incarnation과 floor 관계를 하나의 owner proof 아래 검증하고, anchor proof는 이 모든 값을 정규화해 결속한다. anchor는 **구 proof를 검증한 변환 transaction에서만** 발급하고 기존 `s_generations` head seal에 그 집합 digest를 묶는다. 새 CAS에서는 anchor seal과 현 head seal, receipt 및 floor를 함께 확인한다. 구 generation의 ordinal 필드만 복사하거나 최신 mutable head에 무검증 anchor를 연결하는 것은 금지한다. `_schema3_parity_floor` 호출은 endpoint 봉인과 v6→schema3 CAS이며 일반 검색 전체로 확대하지 않는다. 기존 receipt는 조회 시 live source를 다시 증명하지 않으므로 anchor도 그렇게 주장하지 않는다. 이 구성의 권한 동등성 및 anchor가 구 selected record에 의해 실제로 얼마나 오래 요구되는지는 구현·재리뷰 전 미증명이다.
3. **GC 범위:** S v1은 view 행만 회수한다. 공유 content와 set은 삭제하지 않는다. 나중에 활성화할 R1~R6 및 content writer 경합의 검증은 `docs/TODO.md`에 남긴다. 현행 정적 분석은 검증 완료가 아니다.
4. **양 host receipt:** `find_loaded_bridge`/`check_loaded_bridge`는 Claude·Codex 양 관측의 release commit, 선택 endpoint/epoch·manifest와 유효기간을 대조한다. 이는 그 host가 새 release를 읽었다는 증거이지, 새 format의 변환·구 reader 배제를 그 자체로 증명하지 않는다. 변환 gate는 정확한 S 지원 commit이 journal-selected bundle 및 양 receipt와 같은지 owner 아래 다시 검사하고, 그 commit의 S 지원 여부는 release manifest의 고정 증거와 직접 연결한다. 검증 직후 endpoint·commit이 바뀌면 기존 writer proof 재검증에서 보류해야 한다. 모든 host 사본이 새 bundle이라는 증명으로 확대하지 않으며 구 bundle의 별도 fail-closed 실험이 필수다.
5. **fill 호출 계약:** 현재 `_schema3_vectors`는 `_fill_plan`의 targets·cursor와 `fill_status`의 `job_id`, `cursor`, `complete`, `fill_view`의 `expected_cursor`·`status`·`filled`·`cache_hits`를 사용한다. 새 cursor는 `(incarnation,view,head ordinal,recipe,profile)`에 결속하며 head/spec 변경은 폐기 후 재스캔한다. 상태 응답의 필드와 호출자 예산·deferred 의미는 유지하고 job ID는 이 identity의 결정적 digest로 발급한다. 완료 재호출은 같은 head/spec의 전체 eligible set을 확인한 경우에만 무쓰기·provider 무호출로 완료한다. `fill_targets` 영속 사본 없이 누락을 찾는 방식의 최악 순회 비용·claim 충돌과 head 없는 cold 게시 경로는 실제 검사 전 미증명이다.

### 구현 위치와 설계 밖 선택의 근거

- **`core/laya_view_rows.py`의 책임:** 형식 4 view store의 행 단위 세대 전체를 소유한다. 변경 행 계산과 durable stage 기록, 한 트랜잭션의 게시(카드·legacy FTS 구간, head FTS 변경, seal, delta, head CAS), head seal 검증과 pin·검색·card locations, delta 역적용 rollback, view 행 GC, 형식 3→4 변환이다. `ViewStore`의 공개 메서드는 store 형식(`_rows`)에 따라 형식 3 구현과 이 모듈 구현 중 하나로 넘긴다. 이 모듈은 형식 3 코드를 감싸 호출하지 않으며 형식 4 relation의 SQL과 seal 계산을 직접 가진다. `laya_view_store.py`(형식 3 세대·공통 handle·관측)와 `laya_view_reader.py`(형식 3 읽기)의 형식 3 경로는 바뀌지 않는다.
- **`delete_receipts`의 FK:** 형식 3의 `delete_receipts`는 `generations(gen_id,view_id)`를 참조하는데 형식 4에는 그 테이블이 없다. 형식 4는 같은 열·검사를 유지하고 `(base_gen,view_id)`가 `s_generations(gen_id,view_id)`를 참조해, receipt가 다른 view의 세대를 가리키지 못한다. 이 relation은 아직 writer가 없는 예약 relation이며, 변환은 행이 있는 store를 `row_conversion_unsupported`로 거절한다. GC는 `delete_receipts`가 참조하는 세대 행을 지우지 않는다.
- **변환 전 형식 3·4 이중 운영:** 변환은 양 호스트 gate와 운영 승인 뒤에만 일어나므로 그 전까지 새 번들은 기존 형식 3 store를 계속 읽고 게시해야 한다. 새 번들은 store header의 버전으로 형식 3과 4를 고르고, 새 store의 기본 생성 형식은 형식 4 경로가 worker·reader·shadow에서 모두 통과한 뒤에 바꾼다. 구 번들은 형식 4를 header에서 거절한다.
- **첫 세대 delta 미기록:** view의 첫 세대는 직전 세대가 없어 rollback 대상이 아니다. 그 delta는 corpus 전체 크기이고 쓰임이 없으므로 기록하지 않는다. rollback은 직전 세대가 있는 head에서만 시작하며 그 head의 delta는 항상 기록된다.
- **카드 원문 span의 위치:** 일반 세대의 trusted 카드 원문 span은 `s_card_sources` 구간 행이다. 한 행은 카드 경로·원문 경로·observation·spans다. 카드의 attestation이 같은 원문 경로를 여러 번 인용하면 같은 observation일 때 spans를 ordinal 순서로 합치고, 서로 다른 observation이면 `card_source_conflict`로 거절한다. 이 행은 카드 경로와 trust digest의 함수이므로 prepare는 카드 행이 바뀐 카드만 attestation에서 다시 만들고 나머지의 열린 행은 유지한다. 변경은 `s_stage_card_source_changes`에 stage되고 게시가 `card_source` delta·`s_delta_observations`를 남기며, `member_digest`의 `card_source` 도메인이 결속한다. legacy import 카드는 줄 span을 카드 행 `metadata_json`에 두고, legacy 게시는 열린 `s_card_sources` 행을 닫는다. `card_locations`는 두 출처를 합쳐 같은 locator 규칙으로 답한다. 형식 3→4 변환은 일반 세대의 `card_sources`를 같은 구간 규칙으로 옮긴다.
- **sync worker의 형식 4 게시:** worker는 형식 4 저장소에서 head의 열린 카드 행을 이전 구성원으로, view의 ready stage를 진행분(cold view는 진행분 자체)으로 읽는다. 변경 없음 판정은 head의 capture HEAD가 현재와 같고 head의 열린 원문 행이 이번 관측에서 계산한 observation 행과 같을 때다. 저장된 manifest는 읽지 않는다. vector는 head에 묶인 fill이므로, 새 카드의 set이 하나라도 없으면 lexical 세대를 먼저 게시하고 같은 pass에서 head를 fill한 뒤 vector 세대를 게시한다. 필요한 set이 모두 저장돼 있으면(head가 같은 spec이면 바뀐 카드의 set, 아니면 모든 구성원의 set) vector 세대 하나만 게시한다. 변경 없는 head의 vector 세대는 이번 관측의 원문 fingerprint와 head 구성원으로 manifest를 만들어 게시한다.
- **migration intent `prepared.generation`:** 형식 4에서 prepare는 게시 전 세대 행을 만들지 않고 durable stage를 기록하며, 반환하는 세대 ID(`g-<job id>-b<stage digest>`)는 게시될 때 그 세대의 ID가 된다. intent record의 모양과 GEN_ID 형식은 그대로이고, `prepared.generation`이 가리키는 대상은 prepared 세대 행 대신 그 view의 ready stage다. 이 의미 변경은 구 번들(0.44.18)이 형식 4 candidate를 가리키는 intent를 만나 fail-closed로 거절함을 시험으로 보인 경우에만 쓴다. 그 시험이 통과하지 못하면 형식 4 prepare는 기존 의미(게시 전 세대 행)로 되돌린다.

### shadow 전환과 view 행 GC

`_switch_step`은 shadow candidate의 `store._path`를 schema3 endpoint로 봉인하므로 전환 후 선택 store가 동일한 파일일 수 있다. 잠금 밖 `prepare_shadow_generation`의 쓰기 transaction과 CAS 이후 살아 있는 옛 핸들을 view 행 GC와 별도로 교차 검증한다. 현재 정적 검토는 무잠금 prepare가 항상 배제된다는 실행 증거가 아니다. shared content writer 배제의 분석과 후속 R1~R6는 `context/claude-handoff-s-blob-gc-locks-20261112.md` 및 `docs/TODO.md`에 둔다.

### 저수준 구 validator 실행 준비

전역 개발 hold·guard 상태와 정확한 HEAD를 확인한 뒤 각 Python을 순차 실행한다. 기존 저수준 구 validator probe의 명령은 아래와 같으며, 이는 **새 S 형식·실제 선택 endpoint 시험이 아니다**. 3.9 초기 실패와 후속 관측은 기존 로그로 보존한다.

```sh
LANE_B_PYTHON=/opt/homebrew/bin/python3.13 npm run dev:guard -- --kind test -- node --test --test-concurrency=1 /Users/dulee/work/memvane-handoff/context/lane-B-old-validator-cross-20261111.test.mjs
LANE_B_PYTHON=/usr/bin/python3 npm run dev:guard -- --kind test -- node --test --test-concurrency=1 /Users/dulee/work/memvane-handoff/context/lane-B-old-validator-cross-20261111.test.mjs
```

4천 카드의 실제 schema3 worker·별도 프로세스 recall 합성 기준 측정은 아래와 같다. 실제 S 새 형식과 고정 bb4ae489 구 core의 양 host 교차 fixture는 아직 없다. 기존 FTS-only 4천 합성은 제품 sync 기준 시간을 대체하지 않는다. 새 교차 fixture를 구현·검토한 뒤 guard를 사용해 양 Python에서 순차 실행한다. 모든 실행은 임시 HOME, 실제 child Python/SQLite·HEAD, PID/PPID·자원·경과·전체 로그 및 guard settled를 기록하고, 시간 경과만으로 정상 작업을 종료하지 않는다.

### 현행 schema3 합성 기준 관측

고정 `bb4ae489`의 실제 worker·선택 journal·query endpoint를 합성 authority 아래 실행한 4천 카드 lexical 단일 표본(Python 3.13.12 / SQLite 3.54.1)에서 무변경 sync는 4.604초, 카드 1장 변경 게시는 13.960초, 새 프로세스 recall은 query 1.755초·spawn 포함 1.981초였다. 초기 4천 장 준비는 실제 64장/pass로 63 pass가 걸렸다. 변경 전 DB·sidecar 합계 738,480,128 B에서 변경 후 DB 파일은 755,671,040 B였다. 다만 427 B 합성 카드 수정 시 별도 원문도 추가되었으므로 17,190,912 B 차이는 **카드 한 장만 바꾼 저장 증가량이 아니다**. 파일 크기는 유효 데이터 순증이나 freelist 증거도 아니다. 단계별 로그와 guard는 `context/lane-B-schema3-baseline-4000-py313-result-20261112.md`에 있다.

이 관측은 기존 format의 합성 기준이며 운영 SE 시간·양 host proof·새 설계의 성능·GOAL 충족을 증명하지 않는다. Python 3.9 baseline과 card-only·반복 표본은 아직 없다. S v1 view 행 GC의 경합 안전성과 실제 새 형식의 구 bundle 교차 검증도 미증명이다. 공유 content GC의 잠금·경합 검증은 후속 과제로 둔다.

- 양 Python에서 실제 형식의 RED/GREEN, FTS digest commit 안정성·물리 무결성·기존 tokenizer와 BM25 순위, normal/legacy 두 corpus, 다른 view, stale pin·busy·deadline, long pin과 게시 경합을 검사한다.
- 변경 1장·대량 삭제·반복 rollback·crash 전후·full disk·장기 claim·GC 경합·prior chain·남은 참조를 검사한다. 1천/4천/2만 카드에서 DB+journal+freelist 증가, 동일 변경량의 rollback/GC 시간, 4천 unchanged sync cold/warm 및 recall 품질·지연을 기존 형식과 비교한다. 수치 기준은 GOAL/TODO 원문 그대로 판정한다.
- 구 bundle의 모든 진입점과 양 host loaded receipt, 기존 store 변환 전후·crash 복구 및 0장 shadow 재개를 실제 새 형식으로 확인한다. seal 검증 범위의 변경은 별도 독립 리뷰한다.
- FTS flush 안정성, anchor 권한, 구 bundle 교차, fill 계약, GC 참조 완전성은 실행 증거가 없다. 현행 schema3 기준은 위 3.13 합성 단일 표본이 있으며 양 Python·반복 비교와 새 설계의 네 목표는 미측정이다. 구현 뒤 변경 범위 검사와 독립 리뷰가 필요하다.
