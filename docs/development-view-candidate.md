# View candidate 세대와 lexical·vector reader 계약

`laya_view_store.ViewStore`는 `BlobStore`를 확장하는 schema3 view store API다.
형식은 `memvane-git-blob-candidate-v3`, application id는 `0x4d564231`, schema는 `3`다.
candidate v1/v2와 운영 v6 파일은 읽기 전용 preflight에서 바이트 변경 없이 거절한다.
v6 reader가 선택된 설치에서는 shadow migration이 후보 store를 만들 때만 이 API를 쓰고 recall은
쓰지 않는다. schema3 reader가 선택된 뒤에는 recall pin, sync 게시와 vector fill, dedup retrieval이
이 API를 쓴다.
`ViewStore`는 provider를 직접 부르거나 query embedding을 만들지 않는다. chunk·embedding은
호출자 callback으로 받고, 운영 sync owner가 resident provider를 callback으로 넘긴다.

내용 admission·recipe·profile·provenance 계약은
[`development-blob-candidate.md`](development-blob-candidate.md), checkout 등록·capture는
[`development-checkout-capture.md`](development-checkout-capture.md)에 있다.
원문 body API는 없다. reader 선택·전환·rollback은 `laya_journal_reader`·`laya_shadow_migration`,
운영 게시 순서는 `laya_product_sync_worker`가 소유한다. lifecycle, frontmatter 변경,
retention·GC는 이 API에 없다.

## 내부 API와 수명

| API | 결과 |
| --- | --- |
| `ViewStore.create(...)` / `ViewStore.open(...)` | `BlobStore`와 같은 생성·소유권 인자로 candidate 열기 |
| `capture_view(worktree_id)` | 현재 checkout·settings에 묶인 store 발급 `ViewHandle` |
| `head(handle)` | `{view_id, gen_id, epoch, incarnation}` |
| `fill_status(handle, manifest, recipe_id, profile_id)` | 결정적인 job ID와 durable `cursor`·`complete`, 조회는 읽기 전용 |
| `fill_view(handle, manifest, recipe_id, profile_id, *, chunker, embedder, write_fence, expected_cursor, max_files=8, max_bytes=2000000, deadline_seconds=30)` | content set을 재사용하는 유계 진행 단계 |
| `prepare_generation(handle, manifest, *, expected_head, vector_spec=None)` | lexical-only 또는 완전 vector coverage로 봉인한 `prepared` 세대 ID |
| `publish_generation(handle, gen_id, *, expected_head)` | 같은 view의 CAS 게시 후 head |
| `pin_view(handle)` | 현재 게시 세대와 seal에 묶인 store 발급 `ViewPin` |
| `search(pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None, deadline_seconds=2)` | 고정한 세대의 lexical 결과 목록 |
| `search_exact(pin, query_vector, limit, *, profile_id, max_float_work=4000000, deadline_seconds=2, path_prefixes=None, excluded_paths=None)` | 고정한 세대의 exact cosine 결과 목록 |
| `search_hybrid(pin, fts_query, query_vector, limit, *, profile_id=None, max_float_work=4000000, deadline_seconds=2, path_prefixes=None, excluded_paths=None)` | 같은 pin의 `{lexical, semantic, semantic_status}` |
| `release(pin)` | 해당 pin 해제 |
| `verify_views()` | 저장된 세대·head·미봉인 slot 검증, 성공 시 `True` |
| `close()` | 연결 종료 및 모든 handle·pin 무효화 |

handle과 pin은 frozen dataclass이며 발급 store의 registry가 정확한 객체와 필드를 확인한다.
임의 view ID, 복제·변조 객체, 다른 store의 객체는 사용할 수 없다.
handle은 worktree·branch·HEAD·checkout digest·settings policy·store incarnation에 묶인다.
게시 전 head는 `gen_id=None, epoch=0`이다. 게시된 head가 없으면
`pin_view`는 `view_preparing`으로 거절하며 다른 view로 폴백하지 않는다.
pin 해제·store 종료 뒤 검색은 거절한다.

## Manifest와 검색 자격

manifest의 키는 정확히 다음과 같다. 추가 키와 중복 경로는 거절한다.

```text
{
  scan_id, policy_digest, revocation_epoch, complete: true,
  sources: [{path, fingerprint_id, presence}],
  members: [{path, blob_sha, display_title, trust_digest}]
}
```

source path는 프로젝트 상대 경로이고 member path는 wiki 상대 Markdown 경로다.
상대 경로와 검색 경로 필터는 UTF-8로 인코딩할 수 있어야 한다.
wiki root의 `SCHEMA.md`·`index.md`·`log.md`는 member가 될 수 없다.
`presence`는 `present` 또는 `held_missing`이다. `trust_digest`는 null일 수 있다.
source 지문은 handle과 같은 worktree·branch·path에 속해야 한다.
`present`는 현재 scan과 capture의 HEAD도 일치해야 한다.
`complete: true`는 호출자의 완료 주장이고 원문 전체를 관측했다는 증명이 아니다.

검색 자격 `eligible`은 저장소가 source·attestation·`skipPaths`에서 도출한다.
prepare는 그 시점 settings의 `skipPaths`를 쓰고 stage에 봉인하며, 게시된 세대의 검증은
봉인된 값으로 다시 도출한다.
호출자가 직접 지정하지 않는다. 같은 blob SHA라도 다른 view의 trust를 빌리지 않는다.
non-null trust의 blob·policy가 다르거나 다른 worktree·branch 출처이면 거절한다.
source가 없거나 trust가 null인 카드, 누락·held source, SHA·길이가 바뀐 source,
`legacy_unbound`, 제외된 카드·source는 검색 대상이 아니다.
non-Git의 observed source와 unknown tracked state는 candidate 자격을 가질 수 있지만
삭제 권한을 얻지 않는다.

`card_sources`는 원래 attestation의 지문 ID·순서·span을 보존한다.
같은 경로·SHA·길이를 새 scan에서 관측해도 기존 provenance를 고쳐 쓰지 않는다.
명시적인 rebound 증거는 `BlobStore`의 별도 불변 attestation 계약을 따른다.

## View 채우기와 재시작

fill job은 store incarnation·발급 view capture·settings·전체 manifest·recipe/profile과
정렬한 eligible path/blob/UTF-8 byte 길이에 묶인다. `fill_jobs`·`fill_targets`에는
본문을 저장하지 않는다. identity와 대상은 불변이며 cursor는 대상 수까지 전진한다.
같은 job의 완료 여부는 cursor와 대상 수의 일치로 결정한다.
대상 계획은 본문 검증 뒤 길이 metadata만 보관하며 모든 카드 본문을 모아 두지 않는다.

`fill_status`는 job이 없으면 `cursor=0, complete=false`를 반환한다.
`fill_view`는 정확한 정수 `expected_cursor`로 CAS를 수행하며 결과에
`job_id`, `cursor`, `complete`, `status`, `filled`, `cache_hits`, `deferred`를 담는다.
단계 기본 예산은 8개 대상·2,000,000 UTF-8 bytes이며, 명시적인 상한은
1–64개·1–64 MiB다. 캐시 적중과 같은 내용의 다른 경로도 대상 예산에 포함한다.
첫 대상이 byte 예산을 넘으면 provider 호출·cursor 전진 없이 deferred로 반환한다.
호출자는 기존 카드 본문 상한 4 MiB 안에서 필요한 byte 예산을 명시할 수 있다.

완전한 content set을 먼저 commit하고 cursor는 별도 짧은 transaction에서 기록한다.
cursor 기록이 실패해도 유효한 set은 남으며 다음 호출은 provider 없이 캐시를 재사용한다.
이 복구는 cursor를 쓰므로 첫 DB 변경 전에 fence가 한 번 필요하다.
완료된 동일 job의 재호출은 DB 쓰기·fence·provider 호출이 모두 없다.
빈 대상 job은 한 번의 fenced 초기화로 완료하며 이후 재호출은 읽기 전용이다.

하나의 호출은 job·content·cursor 변경 전체에서 `write_fence()`를 한 번만 호출한다.
callback·fence·content lock 획득 중 SQLite transaction을 유지하지 않는다.
callback 뒤 checkout·settings를 재관측하고 cursor commit 직전에 다시 확인한다.
변경이 관측되면 유효한 불변 content 캐시는 유지할 수 있지만 stale job은 전진하지 않는다.
다른 content owner의 lock과 경합하면 기다리거나 TTL로 인계하지 않고 deferred로 반환한다.
deadline은 유한한 `0 < seconds <= 300`이며 임의 callback을 선점하지 못한다.

## 준비·봉인·게시

세대 상태는 `building → prepared → published` 단방향이다.
member·source·card-source·member-vector·seal은 불변이며 head는 같은 view의 게시·봉인 세대만 가리킨다.
prepare는 유한한 두 쓰기 transaction으로 구성한다.

1. SQLite가 ordinal을 할당하는 `generation_slots`와 세대별 FTS를 기록하고 commit한다.
2. 안정된 FTS physical commitment와 manifest·capture·membership·source·trust roots를
   vector coverage와 함께 계산해 최종 세대 ID를 정하고 전체 행과 seal을 기록해 `prepared`로 봉인한다.

slot의 stage JSON은 경로·hash·title·trust 참조·파일 및 capture metadata와
vector 모드·recipe/profile spec을 포함한다.
source·wiki body를 stage JSON에 넣지 않는다. FTS에는 admission을 통과한
wiki 카드 본문을 사용한다. 미봉인 slot은 검색 head가 될 수 없으며 자동 회수하지 않는다.
원문 scan·모델 작업은 SQLite writer transaction 안에서 실행하지 않는다.

prepare와 publish는 원래 handle의 capture·settings·파일 관측을 다시 확인한다.
vector 세대는 준비·봉인·게시 시 참조한 complete set의 실제 행·입력 SHA·바이트·
provenance를 재검증한다. set head의 count와 digest만으로 게시하지 않는다.
publish는 같은 transaction 안에서 seal과 원래 base를 확인하고
`(view_id, incarnation, gen_id, epoch)`를 비교해 head를 바꾼다.
epoch는 같은 head 값으로 돌아오는 ABA를 구분한다. 다른 view의 게시가 CAS 충돌을
만들지는 않는다. 같은 게시의 정확한 replay만 현재 head와 일치할 때 허용한다.
중간 실패나 CAS 충돌은 기존 게시 head를 바꾸지 않는다.

파일·Git·settings 재확인은 검증 시점의 관측이다. 파일시스템과 SQLite를 아우르는
원자적 snapshot을 보장하지 않는다.

## Vector coverage와 공유 set

`vector_spec=None`은 기본 `lexical-only`이며 vector 참조 행과 `vector_member_count`는 0이다.
명시적인 spec은 정확히 `{recipe_id, profile_id}` 두 키를 가지며 `exact-complete` 모드다.
모든 eligible member에 같은 recipe/profile의 완전한 chunk·embedding set이 필요하다.
일부 member만 채운 partial coverage나 혼합 profile은 게시하지 않는다.

`member_vectors`는 세대별 path·blob·recipe·profile과 `chunk_count`, `chunk_digest`,
`embedding_digest`를 참조한다. vector 바이트를 세대마다 복사하지 않는다.
원래 manifest에서 도출한 전체 coverage와 실제 저장한 참조 행을 비교하고,
정렬한 참조·모드·spec의 `vectors_digest`와 실제 참조 수를 binding root·seal에 포함한다.
다른 view의 동일 content는 같은 불변 set을 재사용하지만 검색 자격과 provenance는
해당 view를 따른다. 나중에 shared set이 추가되어도 기존 lexical 세대나 pin이
자동으로 vector 세대로 바뀌지 않는다. 새 coverage는 새 세대와 같은-view head CAS로 게시한다.

## FTS와 봉인의 범위

각 세대는 자신의 ordinal에 속하는 FTS5와 shadow tables를 가진다.
tokenizer는 `porter unicode61`, CJK spacing은 v6 helper를 사용한다.
BM25의 path/title/body 가중치는 `1.5/4.0/1.0`이며 같은 점수는
binary path 오름차순으로 정렬한다. FTS title과 결과의 `display_title`은
해당 세대 manifest의 `display_title`을 사용한다. owner가 실제 경로·카드에서
표시 제목을 정하며, 같은 path/title/body 입력의 CJK·BM25 동등성을 검증한다.

고정 schema manifest와 등록 ordinal별 FTS DDL·shadow 구성을 검사한다.
봉인 전 FTS `integrity-check`를 실행하며 logical rows와 opaque physical shadow
commitment를 함께 봉인한다. shadow 값은 SQLite 저장 바이트의 commitment이고
FTS 내부 형식의 해석이나 외부 신뢰 attestation이 아니다.

candidate 쓰기 연결은 SQL statement cache 없이 범위가 제한된 authorizer를 설치한다.
FTS 변경 capability는 같은 connection·generation·thread의 building 구간과
commit 중 FTS sync에만 유효하다. shadow 직접 쓰기와 shadow trigger는 지원하지 않는다.
prepared·published FTS 변경은 거절하고 reader는 physical commitment를
가상 테이블 조회보다 먼저 확인한다. 내부 connection·직접 SQL은 지원 API가 아니다.

## Pin 검색과 snapshot 상태

pin은 한 read transaction에서 head·seal·membership·source·trust·FTS를 확인하고
불변 세대 참조를 보관한다. pin 수명 동안 SQLite read transaction을 열어 두지 않는다.
검색 transaction도 호출에 한정하며 rollback journal을 사용한다. WAL·GC API는 없다.
같은 view의 head가 새 세대로 바뀌어도 기존 pin은 원래 세대만 검색한다.

reader는 세대를 봉인한 원래 capture의 정합성과 검색 caller의 현재 capture를
별도로 검증한다. 검색 중 checkout·settings가 caller의 pin capture와 달라지면
결과를 반환하지 않는다. query는 source·wiki 파일 본문을 다시 읽지 않고
저장한 카드 blob·세대 metadata와 현재 settings·Git metadata를 사용한다.

세대와 자격, `path_prefixes`·`excluded_paths` 조건은 SQL `LIMIT` 전에 적용한다.
전역 top-K 뒤 view를 제거하는 방식은 사용하지 않는다.
prefix는 정확한 경로 또는 `/`로 구분한 하위 경로이며 빈 prefix 목록은 결과가 없다.
excluded path는 정확한 경로 일치다.

`search_exact`는 pin에 봉인된 recipe/profile만 사용하며 lexical-only 세대는
`vector_coverage_absent`로 거절한다. query vector는 profile dimension과 일치하는
유한한 숫자 list/tuple이며 bool과 zero norm은 거절한다. 각 카드의 모든 chunk와
cosine을 계산한 최댓값이 카드 점수다. 점수 내림차순, 동점은 UTF-8 binary path
오름차순이다. ANN이나 일부 chunk 절삭을 사용하지 않는다.

view·eligibility·path 필터는 연산량 계산과 scoring·top-K 전에 적용한다.
`max_float_work`는 양의 정수이며 기본 4,000,000이다. 연산량은 필터를 통과한
각 path의 `chunk_count × dimension` 합계로 계산한다. 같은 blob을 참조하는 여러
path도 각각 예산에 포함하며 카드 수 상한으로 해석하지 않는다.

`search_hybrid`는 같은 pin에서 lexical·semantic의 정렬된 두 목록을 반환하며
순위 융합을 하지 않는다. `profile_id=None`이면 pin에 봉인된 profile을 사용한다.
`query_vector=None`이면 lexical 결과와 빈 semantic 목록, `provider_absent` 상태를
반환한다. query가 있어도 lexical-only 세대이면 `lexical_only`다.
semantic 검색 성공은 `complete`, 명시적 work 예산 초과는 `deferred_work_budget`,
semantic 시간 초과는 `deferred_deadline`이며 이미 완료한 lexical 결과를 유지한다.
잘못된 profile·query·pin, seal/set 손상과 checkout 변경은 semantic defer로 숨기지 않고 거절한다.

검색 deadline은 기본 2초, 유한한 `0 < seconds <= 30`이다. hybrid는 호출 시작부터
입력·pin 확인과 lexical·semantic 전체에 하나의 예산을 적용하고 각 단계에는 남은
시간만 전달한다. lexical 완료 전에 시간이 소진되면 결과 없이 거절한다.
이 검색 예산은 callback fill의 기본 30초·최대 300초와 별개다.

결과는 `path, blob_sha, body, display_title, score, view_id, gen_id`와
`evidence: "commitment-only"`, `owner_verified: false`, `snapshot_state`를 포함한다.
body는 저장한 wiki 카드이며 반환 전 blob 바이트와 seal을 확인한다.

| `snapshot_state` | 의미 |
| --- | --- |
| `published_snapshot` | caller와 봉인 시점의 checkout digest가 같다 |
| `stale_snapshot` | digest가 다르지만 HEAD가 같거나 원래 HEAD가 현재 HEAD의 조상이다 |
| `lineage_changed` | 원래 HEAD가 현재 HEAD의 조상이 아니다 |
| `lineage_unverified` | HEAD가 없거나 유한한 Git 계보 확인으로 판정하지 못한다 |

이 상태는 해당 view의 마지막 게시 snapshot을 설명하며 최신 원문 일치를 보장하지 않는다.
seal·receipt·지문·검색 자격은 구조적 commitment다. compiler 실행 사실, 실제 원문 관측,
인용 정확성, 운영 owner의 권한이나 삭제 권한을 증명하지 않는다.

## 운영 사용

schema3 reader가 선택된 설치에서 설치 journal의 endpoint record가 store 경로·incarnation·
main worktree binding·head floor·vector recipe/profile을 고정한다. recall은 그 record로
main view를, 연결 워크트리에서는 그 워크트리의 자기 view를 pin한다. CAS 때 고정한 floor보다
뒤의 head는 거절한다. 같은 UID가 head trigger를 지우고 floor 위 세대를 모두 게시 취소하는
일관된 위조는 검출하지 않는다.

sync owner는 pass마다 main view를 먼저, 그 뒤 연결 워크트리 view를 게시한다. 게시 head가
없는 view는 완료 전까지 prepared 세대만 남기고 게시하지 않으며, 그동안 recall은
`view_preparing`이다. 바뀐 카드만 ingest하고 같은 바이트의 member는 기존 trust를 유지한다.
manifest의 `sources`는 같은 pass의 raw 관측이며 관측 상한(4,096 파일·64 MB)을 넘으면
빈 목록이다.

vector는 lexical 세대를 먼저 게시한 뒤 `fill_view` cursor로 설치 provider spec의 set을 채우고,
완료되면 vector 세대를 새로 게시한다. view chunk는 provider의 1차 분할과 같은 위치여야 한다.
provider가 token window로 다시 쪼개는 카드는 재현할 chunk 텍스트가 없어
`chunk_text_unreconstructable`로 그 view의 vector fill을 막고, 그 view는 카드가 바뀔 때까지
lexical 세대로 남는다. main view의 vector spec이 endpoint record에 봉인되기 전까지 recall은
lexical 결과만 쓴다.

## 입력 상한과 실패

| 대상 | 상한 |
| --- | --- |
| 세대 wiki / source 수 | 각각 20,000 |
| 세대 wiki body 합계 | 64 MiB |
| manifest / slot stage JSON | 32 MiB / 64 MiB |
| FTS 입력 / physical commitment 순회 | 128 MiB / 384 MiB |
| query UTF-8 / 결과 limit | 4,096 bytes / 1–100 |
| path prefixes / excluded paths | 32 / 256 |
| 검색 deadline | 기본 2초, 최대 30초 |
| exact/hybrid float work | 기본 4,000,000, 명시적인 양의 정수 |
| pin / handle registry | 128 / 4,096; handle 초과 시 가장 오래된 발급 기록 제거 |
| pin·세대 전체 검증 deadline / SQL busy | 30초 / 2초 |

개별 카드 4 MiB, source·span·recipe·chunk·vector 상한은 blob 계약을 따른다.
deadline은 SQL progress handler와 유한한 검증 순회에 적용하며 실제 경과 시간은
외부 Git 호출 및 관측 비용을 포함한다.

`BlobStoreError.reason`은 원문 없는 거절 코드다. 주요 코드는
`view_preparing`, `view_handle_invalid`, `view_pin_invalid`, `view_store_closed`,
`checkout_changed`, `view_head_conflict`, `generation_unsealed`,
`generation_seal_mismatch`, `fts_storage_mismatch`, `fts_query_invalid`,
`fts_deadline_exceeded`, `vector_coverage_incomplete`, `vector_rows_mismatch`,
`vector_coverage_absent`, `vector_profile_mismatch`, `vector_query_invalid`,
`vector_work_exceeded`, `vector_deadline_exceeded`다.
거절 뒤 다른 view·운영 v6로 우회하는 API는 없다.

검증 성공은 store의 구조·일관성에 한정된다. `pin_view`와 검색은 호출마다 고정한 세대의
구조·FTS commitment를 검증한다. 서빙하지 않은 행의 변조는 `verify_views`가 검출하며, 이 검증
범위는 `test/laya-view-reader.test.mjs`·`test/laya-view-budget.test.mjs`가 고정한다.
합성 1,800카드·10 Git views에서 새 프로세스 30개 표본(nearest-rank, py3.13)의 p95는
카드 본문을 공유할 때 pin 80 ms·검색 77 ms, 본문이 모두 다를 때 pin 256 ms·검색 251 ms다.
같은 표본에서 `capture_view`(Git 호출 14회)의 p95는 280–303 ms이고 recall 비용의 큰 몫이다.
측정은 `MEMVANE_VIEW_WARM_RED=1`일 때만 돈다.
호스트 실행 비용에 따라 기본 2초 제한으로 `fts_deadline_exceeded`를 반환할 수 있다.
규모 fixture는 완료 비용 측정을 위해 명시적 30초 제한을 사용하며,
기본 제한의 거절·복구 검사는 별도로 유지한다.
모델 품질과 v6 대비 parity는 이 검증의 범위 밖이다. parity는 shadow migration의 receipt가 봉인한다.
