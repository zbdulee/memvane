# QMD 2.5.3 게시 view 구현 조사 (2026-10-17)

이 문서는 C1 합성 CAS 이후의 **C2 설계 조사**다. 운영 공유 DB, endpoint,
pointer, 사용자 소설이나 모델을 변경한 실행 결과가 아니다.

**후속 방향:** A/B와 후보 C는 QMD 2.5.3을 전제로 조사한 비교 자료다.
제품의 검색·색인 소유권은 [Laya Index 계획](2026-10-17-embedded-sqlite-search-engine.md)에
정리했다. 아래의 A 우선 판단과 QMD adapter 절차는 새 엔진의 최종 저장 구조나
upstream PR 의존성을 결정하지 않는다. 기존 설치·DB 전환은 별도 마이그레이션이다.
여기서 **A-copy**는 QMD 전체 DB 복제·pointer 선택을 뜻한다. 후속
[A/H 비교](2026-10-17-laya-index-ah-comparison.md)의 **A-int**는 하나의
새 엔진 DB 내부에 불변 세대 membership을 누적하는 별개 구조다.

## 확인된 adapter 기능과 제약

| 근거 | 확인한 동작 | C2에 미치는 영향 |
|---|---|---|
| [QMD 2.5.3 README](https://github.com/tobi/qmd/blob/v2.5.3/README.md) SDK·CLI 설명 및 설치된 `@tobilu/qmd/dist/store.js`·`dist/cli/qmd.js` | 별도 `dbPath`/`INDEX_PATH`, 이름 붙인 `--index`, `QMD_CONFIG_DIR`로 독립 index를 열 수 있다. SDK 문서의 `store.update({collections})` 예제와 설치된 배포물의 `createStore()` 메서드 표면은 같지 않다. | 배포물의 실제 API를 기준으로 adapter를 시험해야 한다. 단순히 README 예제 호출을 제품에 넣지 않는다. |
| 설치된 `dist/store.js`의 `reindexCollection()`·`generateEmbeddings()` | collection 재색인은 파일별로 쓰고, 임베딩은 청크별로 쓴다. 전체 update/embed를 감싸는 단일 transaction은 보이지 않는다. | 현재 활성 DB에서 CUD 전체를 원자적 게시라고 부를 수 없다. 비활성 index에서 완료·검증해야 한다. |
| 설치된 `dist/cli/qmd.js`의 `updateCollections()`·`embed` 분기 | CLI `update`는 설정된 전체 collection을 순회한다. `embed`는 `-c`로 한 collection을 받을 수 있다. | 공유 DB 전체 사본 방식은 다른 소유 프로젝트의 파일도 다시 읽을 수 있다. 출처와 승인·revision을 모두 검증하거나 SDK의 제한된 collection 갱신 adapter를 별도 검증해야 한다. |
| 설치된 `dist/cli/qmd.js`의 collection `update` 설정 처리 | YAML collection에 `update` 명령이 있으면 CLI가 `bash -c`로 이를 실행한다. | 후보 세대의 설정에서 실행 명령을 금지·검증하지 않은 채 `qmd update`를 호출하면 소스나 외부 저장소가 바뀔 수 있다. 합성 adapter에서도 먼저 이 경계를 검사한다. |
| `core/sqlite_read.py:snapshot_fingerprint`·`cutover_guard` | 원본 QMD WAL을 읽기 전용 SQLite backup으로 관측하고, 짧은 `BEGIN IMMEDIATE` writer 예약으로 cutover 직전 fingerprint를 재검증하는 기존 유틸리티가 있다. | 비활성 후보 복제의 출발점은 있다. 복제본을 장기 작업한 뒤 원본과 합치는 계약은 아직 없다. |
| `core/qmd_shared_store.py:stage_empty/activate_empty`·`core/runtime_update.py` | 공유 store pointer는 최초 빈 세대만 독점 활성화한다. 프로젝트 로컬 shadow cutover는 마이그레이션용이다. | 운영 공유 DB의 반복 CAS 전환과 crash 복구는 새 계약이 필요하다. |
| `core/qmd_endpoint.py`·`backend/index_worker.sh` | 현재 endpoint 하나가 활성 공유 세대에 묶이고 worker는 그 DB에 직접 `collection add → update → embed`한다. | pointer만 먼저 바꾸면 요청이 다른 세대의 daemon/DB를 혼용한다. worker의 옛 세대 쓰기를 차단하는 준비·게시 분리가 필요하다. |

위의 설치 배포물은 로컬에서 **읽기만** 확인했고
[v2.5.3 store 소스](https://github.com/tobi/qmd/blob/v2.5.3/src/store.ts)와
[CLI 소스](https://github.com/tobi/qmd/blob/v2.5.3/src/cli/qmd.ts)의 해당
경로를 교차 확인했다. README는 원자적 게시·요청 세대 pin을 약속하지 않는다.

## 구현 가능한 두 경로

### A. 공유 DB 전체의 세대별 사본

활성 DB를 SQLite backup으로 새 owner-private 세대에 복제하고, 별도 설정·캐시와
`INDEX_PATH`로 QMD를 **그 사본에만** 실행한다. 모든 승인 collection의 파일
revision·등록 수명·scope를 증명한 뒤 후보의 문서·벡터·설정·WAL 상태를
검증한다. writer lock 아래에서 시작 세대와 모든 승인 revision을 재검증하고
pointer CAS로 게시한다. 실패·경합이면 기존 pointer와 queue claim을 유지한다.
후보 설정에는 QMD의 shell `update` 명령이 없음을 증명해야 한다.

이 방법은 기존 한 개의 물리 검색 DB 구조를 가장 덜 바꾸지만, 편집마다 전체
DB 복제와 `update`의 전체 collection 스캔 비용이 든다. 다른 worktree가
바뀌는 동안 후보가 낡으므로 재시도 폭증 가능성이 있다. 활성 DB는 기존 worker가
쓰고 있으므로, 새 경로 전환 전에 **모든 writer를 이 준비·게시 프로토콜로
옮겨야** 한다. 혼합 writer 상태에서 pointer CAS만 추가해서는 안전하지 않다.

### B. worktree별 독립 index + 검색 합성

승인된 worktree마다 별도 DB/collection identity를 두고 해당 소유자의 index만
비활성 갱신·게시한다. 공통 collection 검색이 필요하면 조회 시 복수 index의
결과를 결합해야 하며 점수·중복·출처 정책을 검증해야 한다. 변경 한 건의 복제
비용은 줄지만, 기존 단일 QMD daemon·공유 DB·collection registry·검색 점수
정책을 더 크게 재설계한다. 지금의 중앙 등록 관리 결정과 물리 DB를 유지한다는
단기 방침에는 A가 먼저 맞지만, 장기 비용 기준은 측정 전 미정이다.

## 안전한 실행 순서

1. **격리 합성 C2a:** 임시 DB만 사용해 SQLite backup, 후보 준비 실패,
   A/B 늦은 worker 장벽, CAS 거절, queue 미ACK, crash 뒤 후보 격리와 이전
   view 보존을 결정적으로 검증한다. 운영 DB·모델은 건드리지 않는다.
2. **실제 배포물 adapter 검증:** 격리된 합성 Markdown/index와 명시적 환경에서
   QMD 2.5.3 `update`의 범위·`embed -c`·WAL/close 동작, SDK의 제한된
   collection 갱신 API를 확인한다. 모델/API가 필요한 부분은 별도 승인까지
   실행하지 않는다. 설치된 코드와 README의 SDK 표면 차이를 이때 고정한다.
3. **운영 전환 설계:** QMD writer 전체를 후보 세대 프로토콜에 묶고, 게시 manifest에
   store 세대·owner incarnation·content revision·endpoint identity를 함께
   결속한다. 요청 시작 시 하나의 manifest를 고정해 모든 query에 사용하고,
   후보 daemon이 준비된 다음 CAS 전환한다. 이전 daemon은 진행 중 요청의
   완료/timeout을 기다린 뒤 정리한다. 비활성 후보 정리는 게시/rollback과
   분리한다.
4. **릴리즈 검증:** 설치 후보의 실제 Claude/Codex 새 세션에서 pull·branch·
   worktree 추가·수정·삭제, 질의 중 전환, 늦은 worker, 실패/재시도와 rollback을
   확인한다. 이 증거가 나오기 전 C2와 RQA-RACE-01은 완료가 아니다.

현재 C1 `published_view.publication_decision()`은 이 계획의 비교 규칙을
합성으로 표현할 뿐 운영 writer나 endpoint를 전환하지 않는다.

### C2a 좁은 합성 증거 (2026-10-17)

`test/shadow-index-snapshot.test.mjs`는 임시 SQLite WAL 원본을 backup으로
비활성 후보에 복제하고, 후보만 변경한 뒤 옛 worker의 늦은 원본 쓰기가 이미
게시된 합성 후보를 덮지 못하는 상황을 확인한다. 실패한 후보 작업에서는
합성 pointer와 원본이 그대로다. 기존 C1 경합 시험과 함께 개발 가드 2/2
통과, `settled:true`(receipt `2ecc3582e0144cacb03a5164d124bbbc`). 이는
위 1단계의 **일부**다. queue 미ACK·crash 복구, 실제 QMD update/embed,
운영 daemon/manifest 전환은 아직 검증하지 않았다.

### 운영 연결 없이 구현한 adapter 원시 기능

`core/shadow_index.py`는 호출자가 지정한 owner-private 임시 root에만
`backup_inactive(source, root)`로 SQLite WAL의 한 읽기 snapshot을 새 후보
`candidates/g-*/index.sqlite`에 복제한다. `seal_candidate`는 후보의
`PRAGMA quick_check`와 읽기 snapshot 지문을 돌려준다. 별도
`published-view.json`의 `publish_candidate`는 owner·registration·worktree·
scope·store·content revision을 비교하고, 후보 지문을 SQLite writer 예약 아래
재확인한 뒤 lock + 임시 파일 fsync + `os.replace` + 디렉터리 fsync로 CAS한다.
경합·후보 변조는 pointer를 진전시키지 않는다. 운영 공유 store의
`active.json`이나 QMD endpoint는 선택·수정하지 않는다.

이 원시 기능으로 **비활성 DB 복제와 단일 private pointer의 게시 판정**은
소스 수준에서 구현할 수 있다. 아직 해결되지 않은 계약은 (1) QMD 설정의
shell `update` 금지 증명 및 후보 전용 CLI/SDK 실행, (2) 모든 기존 writer의
활성 DB 직접 쓰기 중단과 queue 미ACK 복구, (3) 후보 daemon을 먼저 준비하고
검색 요청을 한 endpoint·세대에 고정하는 것, (4) 게시 뒤 옛 daemon·후보
정리와 crash 복구다. SQLite CAS만으로 이 네 항목을 증명하지 못한다.

반복 게시를 **기존 공유 DB 전체 복제(A)**로 할지 **worktree별 독립 DB와
검색 합성(B)**으로 할지는 저장 구조 선택이다. A는 현재 단일 DB·검색 점수
구조를 지키는 대신 모든 writer 전환과 전체 사본 비용이 필요하다. B는 변경별
복제 비용을 낮출 여지가 있지만 collection registry, daemon 수명, 검색 점수·
중복 정책을 바꾼다. 어느 쪽도 이번 격리 adapter가 운영 마이그레이션을
자동 승인하지 않는다. 실제 저장 구조 변경을 시작할 때 선택된 경로와
마이그레이션/rollback 범위의 별도 검토가 필요하다.

`test/shadow-index-adapter.test.mjs`는 live WAL 백업, 후보 변경 후 seal,
private pointer의 원자 CAS, 늦은 worker 거절과 seal 이후 후보 변조 거절을
임시 SQLite로 확인했다. C1/C2a·dispatcher와 함께 개발 가드 집중 검사
20/20 통과(run `2a9a0bbfc6f44f30b3d0eb430d673a5e`). 수정 후 전체 회귀에서도
이 사례는 통과했지만 전체 suite는 PyYAML이 없는 테스트 Python 때문에
4건 실패했다. 실제 QMD 후보 CLI·endpoint·운영 pointer 검증과 혼동하지 않는다.

### C2b 소스 연결과 격리 검증 (2026-10-17)

기준 전체 합성 회귀는 PyYAML을 테스트 전용 경로로 제공한 뒤 1,584건 중
1,582 PASS·0 FAIL·2 SKIP, 개발 가드 `passed/settled:true`였다(run
`42d0c8947e4f48f5b7b187cd61d3ec1f`). 그 다음 아래 **비운영** 소스
경계를 추가했다.

- `qmd_candidate_config.py`는 이미 승인된 baseline hash와 collection 이름·
  경로를 입력받아 QMD `index.yml`의 제한된 형태만 증명한다. collection의
  `path`·`pattern` 외 필드, shell `update`, alias·anchor, 모델 suffix 변경과
  승인되지 않은 collection은 거절한다. YAML 일반 파서를 제품 의존성으로
  넣지 않는다. 후보 환경의 세 경로가 원문 경로와 겹치지 않는지도 판단한다.
- `shadow_publish_flow.py`는 explicit private root의 SQLite backup → 후보
  수정 callback → seal → `shadow_index.publish_candidate` CAS → ACK callback을
  잇는다. 실패하거나 CAS에서 지면 ACK하지 않는다. 직전 게시 후보 파일이
  있으면 다음 backup의 source가 정확히 그 파일인지 확인해 이전 게시 내용을
  빠뜨리는 복제를 거절한다.
- `shadow_qmd_adapter.py`는 두 경계를 합친다. 합성 QMD
  `store_collections(name,path,pattern)`과 승인 collection을 비교하고,
  후보 폴더에만 제한된 `index.yml`·cache를 만든다. 후보 수정 callback 뒤
  설정 hash와 물리 collection을 다시 확인한 뒤 게시 흐름으로 넘긴다.
  QMD 실행은 callback의 계약으로 남겨 두었고 이번 검사는 임시 SQLite만
  수정했다. 어떤 운영 worker도 이 adapter를 호출하지 않는다.

새 fixture와 기존 C1/C2a 게시 검사를 묶은 개발 가드 집중 회귀 **5/5 PASS**,
`settled:true`(run `45a44ae39cbd4091ba33c347c0936b7c`). fixture는
악성 `update` 차단, 물리 scope 불일치, callback 중 config 변경,
후보만 수정, A/B 늦은 게시 거절, 게시 후에만 ACK, 두 번째 게시의 source
연속성을 검사한다.

**남은 경계:** 게시 뒤 ACK callback이 실패하거나 프로세스가 죽으면 private
pointer가 앞서고 queue claim은 남는다. 현재 adapter에는 durable receipt가
없으므로, 그 사이 다른 세대가 게시되면 첫 게시의 소유를 증명할 수 없다.
운영으로 연결하기 전에 queue claim과 게시 manifest의 동일 transaction/복구
계약이 필요하다. 기존 `index_worker.sh`·`update.sh`의 활성 DB 직접 쓰기,
후보 QMD CLI의 실제 설정·WAL 동작, daemon 세대 전환과 요청 pin도 아직
미구현이다. 공유 DB 전체 복제(A)와 worktree별 DB(B) 중 저장 구조 선택과
마이그레이션·rollback 범위 승인 전에는 운영 포인터를 전환하지 않는다.

### C2c 불확실 ACK와 요청 pin의 소스 계약 (2026-10-17)

저장 구조 A/B 선택과 독립적인 두 경계를 추가했다.

- `shadow_ack_policy.py`는 claim id·digest, 예상/준비 view, 후보 seal의
  **게시 전 receipt**를 입력으로 만드는 순수 함수와 재시도 판정을 제공한다.
  현재 private pointer가 준비 view와 정확히 같고 동일 claim·후보 seal이
  확인될 때만 `ack`를 반환한다. 예상 view 그대로면 `reprocess`, 다른 세대
  또는 불확실한 증거는 `reprocess`/`defer`로 분리한다. 파일이나 queue를
  직접 읽거나 쓰지 않는다.
- `shadow_publish_flow.run`과 `shadow_qmd_adapter.run`은 선택적으로
  `claim`·`retain_receipt` callback을 받아 **callback이 반환된 뒤에만**
  private CAS를 시도한다. 보존 실패는 게시 전 거절이다. 합성 fixture는
  임시 receipt를 fsync한 뒤 ACK 지점에서 크래시를 흉내 내고, 다시 읽은
  receipt·pointer·후보 fingerprint로 동일 claim의 ACK 결정만 허용함을
  확인한다. 제품 queue나 운영 pointer는 호출하지 않는다.
- `request_view_pin.py`는 선택된 route의 전체 canonical hash, 설정·프로젝트
  identity, 공유 endpoint identity와 URL, 선택된 CLI index 경로를 한 요청
  값으로 고정한다. `recall.py`의 1차·hierarchical raw backfill 질의는
  같은 pin의 경로를 받고, 공유 endpoint URL이 앞서 확인한 URL과 다르면
  조기 종료한다. 출력 직전 새 route·endpoint·설정이 pin과 다른 경우
  결과를 보류한다. fixture 모드와 미선택 route에는 새 pin을 적용하지 않는다.

recall·C1·worktree freshness·합성 selected index·ACK 정책을 묶은 개발 가드
집중 회귀 **57/57 PASS**, `settled:true`(run
`a2d120f02e294d1f98354d923c647d97`). private receipt·설정·게시 결합
집중 회귀 **4/4 PASS**, `settled:true`(run
`da996f209c3f4be2bd4753fb87fc7b3a`).

**운영 미완료:** source callback은 receipt의 영속성을 직접 보장하지 않는다.
운영 claim journal/manifest에 게시 전 receipt를 기록하고, pointer·후보
seal 관측에서 실제 queue ACK까지 동일한 세대와 claim 조건을 잠금으로
유지해야 한다. receipt가 없는 크래시나 다른 세대로 이미 게시된 상태는
정책상 ACK 근거가 아니다. 요청 pin 역시 선택 identity만 고정하며 활성 DB
내용은 기존 writer가 직접 수정할 수 있다. 따라서 RQA-RACE-01 및 운영
반복 게시·daemon 전환은 계속 BLOCKED다.

### 후보 C: 공유 hash와 worktree별 전체 wiki 멤버십 (격리 원형)

후보 C는 A의 공유 DB 전체 복제와 B의 worktree별 DB 사이에서, QMD의
`documents(collection,path,hash)`와 hash 기준 `content`/`content_vectors`를
활용할 가능성을 검토한 것이다. 같은 wiki bytes라면 내용·벡터를 공유할 여지가
있지만 실제 배포물의 재사용·삭제 동작은 아직 증명하지 않았다. 단순히 기존 공유
DB에서 `update`만 증분 실행하면 파일별/청크별 중간 상태가 검색에 보인다.
따라서 안전한 C의 게시 단위는 **worktree별 불변 검색 세대**여야 한다.

멤버십 표현은 부모 세대에 삭제 tombstone을 덧씌우는 overlay 대신
`(worktree, generation) -> 전체 상대경로·content hash 목록`으로 잡았다.
기존 `file_change_plan`의 비교 결과는 변경분 준비에 쓰되, 게시되는 목록은
항상 전체다. 이 구조에서는 삭제한 경로가 부모 snapshot에서 다시 보일 수
없고, 한 hash를 참조하는 다른 worktree나 고정된 옛 요청이 있으면 내용을
수거하지 않는다. 편집 시 전체 목록의 기록 비용은 들지만 overlay 체인,
tombstone 병합, 부모 세대 생존 관리가 없어 정확성 계약이 단순하다.

`core/worktree_wiki_membership.py`는 실제 관측된 wiki 파일의 전체 목록을
받는 순수 planner와 세대 CAS 비교를 제공한다. unreadable 경로는 이전
항목을 보존한다. 기본 브랜치 이름은 선택 힌트일 뿐 가시성 근거가 아니다.
`main`/`master`/다른 이름, 기본 브랜치 판별 실패, detached HEAD, 미커밋
wiki 모두 현재 worktree에서 관측한 파일 목록으로 결정한다. moving HEAD를
세대 식별자로 사용하지 않는다. raw→wiki `raw_generation`과 wiki→search
`generation`은 별도 값이다.

`core/worktree_query_slice.py`는 **QMD와 분리된** SQLite 합성 adapter다.
전체 멤버십 staging → writer CAS(기게시 세대 재선택 거절) → 요청 pin → 멤버십 SQL JOIN을 점수 정렬과
`LIMIT` 전에 적용 → pin 해제 → 참조 없는 내용 정리의 최소 계약을 보인다.
`test/worktree-candidate-c.test.mjs`의 Python fixture 10개는 같은 상대경로의
worktree별 다른 내용, 같은 내용 hash 재사용, 추가·편집·삭제·rename,
브랜치 전환/불명/분리 HEAD/미커밋, 늦은 writer 거절, 이전 요청 pin,
삭제 재출현 방지와 참조 중 GC 금지를 검사한다. 개발 가드 집중 검사와 인접
worktree freshness·요청 pin·private QMD adapter 회귀는 4/4 파일 PASS·
`settled:true`, run `e34a60c9744d4c61ae07de651bfae910`이었다.
추가한 유한 overfetch 반례까지 Candidate C Python 내부 검사는 11건이며,
가드 집중 검사 PASS·`settled:true`(run `8d73ba3e900b43d4b22a48c7e4029dda`).
운영 QMD 스키마나 index에는
연결되지 않는다.

#### 설치된 QMD 2.5.3 배포 코드의 읽기 전용 판정

관리형 runtime의 `prepared.json`이 가리키는 `@tobilu/qmd` 패키지의
`package.json`과 저장소의 `locks/qmd-2.5.3/package-lock.json`이 모두
2.5.3을 지정한다. 해당 배포물 `dist/store.js`와 `dist/cli/qmd.js`를
읽었으며 QMD 명령, 모델, 서버, 운영 DB는 실행하지 않았다.

- `store.js:2601-2643`의 lex 검색은 **전역 FTS 상위 `limit×10`**을 먼저
  CTE에서 자른 다음 `d.collection`으로 필터한다. `structuredSearch`는
  collection마다 `searchFTS(...,20,coll)`을 호출한다(`:3864-3880`).
  다른 worktree 결과가 200개 이상 앞서면 선택 worktree의 결과가 후보에
  들어오지 못한다.
- `store.js:2669-2713`의 vec 검색은 `vectors_vec`에서 컬렉션 조건 없이
  **전역 상위 `limit×3`**을 먼저 뽑고, 뒤의 `documents` JOIN에서
  `d.collection`을 건다. 소스 주석에 vec0와 JOIN을 같은 쿼리에서 하면
  멈춘다고 명시돼 있다. `structuredSearch`는 collection마다
  `searchVec(...,20,coll)`을 호출한다(`:3890-3910`). lex·vec 모두
  현재 `-c`가 후보 C의 검색 전 완전 필터 계약을 만족하지 않는다.
- `store.js:1085-1112`의 임베딩 대기 조회는 활성 문서를 hash로 묶고,
  지정 모델·`embed_fingerprint`의 청크 수가 기대 청크 수를 채웠으면 같은
  hash를 다시 임베딩하지 않는다. 기본 `embed`의 `force`가 false일 때
  물리 재사용 가능성은 소스로 확인된다(`:1150-1163`, CLI `:1715-1738`).
  `hashContent`는 UTF-8로 읽은 **문자열**을 SHA-256으로 해시한다
  (`:972-1003`, `:1732-1736`). 모델/포맷/청크 크기는 fingerprint에
  들어가지만, 문서 제목은 본문에 제목이 없으면 경로에서 나온다
  (`:1750-1769`, `:1255-1275`). 같은 본문을 다른 이름으로 두면 hash는
  공유돼도 실제 임베딩 입력이 경로에 따라 달라질 수 있으므로 동일
  검색 의미까지 보장된다고 단정하지 않는다.
- `store.js:2265-2277`의 `removeCollection`은 대상 문서만 지운 뒤 **다른
  활성 문서가 참조하지 않는** `content`만 제거한다. FTS 삭제 trigger가
  문서 삭제를 반영한다(`:758-762`). 벡터는 이 경로에서 바로 제거하지
  않으며 `cleanupOrphanedVectors`가 활성 문서 참조가 없는 hash를
  정리한다(`:1676-1716`). 특정 collection의 강제 임베딩 초기화도 다른
  활성 collection과 공유하는 hash를 제외한다(`:2780-2836`). 따라서
  현재 코드의 정상 순서에서는 다른 worktree가 참조하는 본문/벡터가
  collection 제거 때문에 지워지지 않는다. 실제 DB에서의 동시성·크래시
  실험은 하지 않았다.
- CLI `collection add`는 같은 path+glob의 두 번째 collection을 거절한다
  (`dist/cli/qmd.js:1456-1491`). SDK의 `addCollection`은 이름 기준으로
  설정을 갱신할 수 있지만(`dist/collections.js:232-240`), CLI `update`는
  등록 collection을 순회해 재색인한다(`dist/cli/qmd.js:498-559`).
  같은 worktree의 옛 불변 세대가 새 파일을 다시 읽지 않게 하려면 신규
  세대만 갱신하는 별도 adapter와 등록·daemon 수명 계약도 필요하다.

**결정:** 현재 QMD 2.5.3의 일반 CLI/SDK 검색 경로는 후보 C의 정확한
lex+vec 범위 제한을 제공하지 않는다. 유한 overfetch는 정답을
보장하지 않으며 `qmd_scope.scoped_hits`의 결과 후 검증도 누락을 복구하지
못한다. 전역 후보를 사실상 전부 뽑는 방식은 이론적으로 가능하더라도
검색 비용이 전체 DB에 비례하고 현재 `structuredSearch`의 collection별
후보 20개 한계도 바꿔야 한다. 정확성과 기존 검색 비용을 함께 유지하려면
QMD 검색 후보 생성 단계의 변경(특히 vec의 collection-aware 후보 추출)과
그 배포·마이그레이션 검증, 또는 B의
worktree별 격리 index와 검색 합성이 필요하다. 어느 경로도 이번
source-only 승인 범위에서 시작하지 않았다. collection 추가 후 daemon의
가시성, 모든 기존 writer 차단·전환도 운영 연결 전에 남은 검증 항목이다.
