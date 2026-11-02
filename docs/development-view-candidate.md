# View candidate 세대와 lexical reader 계약

`laya_view_store.ViewStore`는 `BlobStore`를 확장하는 비활성 개발용 API다.
형식은 `memvane-git-blob-candidate-v2`, application id는 `0x4d564231`, schema는 `2`다.
candidate v1과 운영 v6 파일은 읽기 전용 preflight에서 바이트 변경 없이 거절한다.
운영 v6 reader·hook·route는 이 API를 호출하지 않는다.

내용 admission·recipe·profile·provenance 계약은
[`development-blob-candidate.md`](development-blob-candidate.md), checkout 등록·capture는
[`development-checkout-capture.md`](development-checkout-capture.md)에 있다.
원문 body API는 없다. 벡터 자동 채움, reader bridge, migration, 운영 활성화,
lifecycle, frontmatter 변경, retention·GC는 이 candidate API의 범위 밖이다.

## 내부 API와 수명

| API | 결과 |
| --- | --- |
| `ViewStore.create(...)` / `ViewStore.open(...)` | `BlobStore`와 같은 생성·소유권 인자로 candidate 열기 |
| `capture_view(worktree_id)` | 현재 checkout·settings에 묶인 store 발급 `ViewHandle` |
| `head(handle)` | `{view_id, gen_id, epoch, incarnation}` |
| `prepare_generation(handle, manifest, *, expected_head)` | 봉인한 `prepared` 세대 ID |
| `publish_generation(handle, gen_id, *, expected_head)` | 같은 view의 CAS 게시 후 head |
| `pin_view(handle)` | 현재 게시 세대와 seal에 묶인 store 발급 `ViewPin` |
| `search(pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None, deadline_seconds=2)` | 고정한 세대의 lexical 결과 목록 |
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
wiki root의 `SCHEMA.md`·`index.md`·`log.md`는 member가 될 수 없다.
`presence`는 `present` 또는 `held_missing`이다. `trust_digest`는 null일 수 있다.
source 지문은 handle과 같은 worktree·branch·path에 속해야 한다.
`present`는 현재 scan과 capture의 HEAD도 일치해야 한다.
`complete: true`는 호출자의 완료 주장이고 원문 전체를 관측했다는 증명이 아니다.

검색 자격 `eligible`은 저장소가 source·attestation·`skipPaths`에서 도출한다.
호출자가 직접 지정하지 않는다. 같은 blob SHA라도 다른 view의 trust를 빌리지 않는다.
non-null trust의 blob·policy가 다르거나 다른 worktree·branch 출처이면 거절한다.
source가 없거나 trust가 null인 카드, 누락·held source, SHA·길이가 바뀐 source,
`legacy_unbound`, 제외된 카드·source는 검색 대상이 아니다.
non-Git의 observed source와 unknown tracked state는 candidate 자격을 가질 수 있지만
삭제 권한을 얻지 않는다.

`card_sources`는 원래 attestation의 지문 ID·순서·span을 보존한다.
같은 경로·SHA·길이를 새 scan에서 관측해도 기존 provenance를 고쳐 쓰지 않는다.
명시적인 rebound 증거는 `BlobStore`의 별도 불변 attestation 계약을 따른다.

## 준비·봉인·게시

세대 상태는 `building → prepared → published` 단방향이다.
member·source·card-source·seal은 불변이며 head는 같은 view의 게시·봉인 세대만 가리킨다.
prepare는 유한한 두 쓰기 transaction으로 구성한다.

1. SQLite가 ordinal을 할당하는 `generation_slots`와 세대별 FTS를 기록하고 commit한다.
2. 안정된 FTS physical commitment와 manifest·capture·membership·source·trust roots를
   계산해 최종 세대 ID를 정하고 전체 행과 seal을 기록해 `prepared`로 봉인한다.

slot의 stage JSON은 경로·hash·title·trust 참조·파일 및 capture metadata만 포함한다.
source·wiki body를 stage JSON에 넣지 않는다. FTS에는 admission을 통과한
wiki 카드 본문을 사용한다. 미봉인 slot은 검색 head가 될 수 없으며 자동 회수하지 않는다.
원문 scan·모델 작업은 SQLite writer transaction 안에서 실행하지 않는다.

prepare와 publish는 원래 handle의 capture·settings·파일 관측을 다시 확인한다.
publish는 같은 transaction 안에서 seal과 원래 base를 확인하고
`(view_id, incarnation, gen_id, epoch)`를 비교해 head를 바꾼다.
epoch는 같은 head 값으로 돌아오는 ABA를 구분한다. 다른 view의 게시가 CAS 충돌을
만들지는 않는다. 같은 게시의 정확한 replay만 현재 head와 일치할 때 허용한다.
중간 실패나 CAS 충돌은 기존 게시 head를 바꾸지 않는다.

파일·Git·settings 재확인은 검증 시점의 관측이다. 파일시스템과 SQLite를 아우르는
원자적 snapshot을 보장하지 않는다.

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
| pin / handle registry | 128 / 4,096; handle 초과 시 가장 오래된 발급 기록 제거 |
| pin·세대 전체 검증 deadline / SQL busy | 30초 / 2초 |

개별 카드 4 MiB, source·span·recipe·chunk·vector 상한은 blob 계약을 따른다.
deadline은 SQL progress handler와 유한한 검증 순회에 적용하며 실제 경과 시간은
외부 Git 호출 및 관측 비용을 포함한다.

`BlobStoreError.reason`은 원문 없는 거절 코드다. 주요 코드는
`view_preparing`, `view_handle_invalid`, `view_pin_invalid`, `view_store_closed`,
`checkout_changed`, `view_head_conflict`, `generation_unsealed`,
`generation_seal_mismatch`, `fts_storage_mismatch`, `fts_query_invalid`,
`fts_deadline_exceeded`다. 거절 뒤 다른 view·운영 v6로 우회하는 API는 없다.

검증 성공은 candidate의 구조·일관성에 한정된다. 합성 1,800카드·10 Git views의
warm 검색 30개 표본은 중앙값 1,022.42 ms, p95 1,240.01 ms다. 검색은 호출마다
전체 세대의 구조·FTS commitment를 검증한다. warm view p95 50 ms와 lexical
p95 150 ms 목표는 충족하지 못하며 운영 활성화 전 해결할 성능 항목이다.
모델 품질, 실제 migration 또는 운영 endpoint parity도 이 candidate 검증의 범위 밖이다.
