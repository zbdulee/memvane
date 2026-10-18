# 같은 세션 파일 변경과 worktree 검색 경계 (2026-10-17)

이 문서는 rc48 이후 작업의 단계별 계약이다. 아래 **목표**는 구현 완료를 뜻하지 않는다.
등록·경로 관리와 물리 DB 통합을 분리한 결정은
[중앙 등록 관리 비교](2026-10-17-central-registration-options.md)에 기록한다.
QMD 2.5.3의 C2 비활성 게시 경로와 실제 adapter 제약은
[게시 view 구현 조사](2026-10-17-qmd-published-view-options.md)에 기록한다.
운영 Git 훅 설치, 자동 `git pull`, 사용자 OFF 설정 변경, 외부 경로의 자동 등록은
이 계획에 포함되지 않는다. 여섯 기능의 상태 소유권은
[아키텍처 경계](2026-10-17-architecture-boundaries.md)를 따른다.

## 현재 사실과 미해결 문제

- rc48은 SessionStart의 성공한 QMD update/embed 뒤 허용 collection의 Markdown
  생성·수정·삭제를 해시 스냅샷과 비교하고, 인덱싱 collection을 dirty queue에 넣는다.
  같은 세션에서 pull/checkout 직후 검색 전 전체 변경 집합을 확인하거나, 새 파일의
  즉시 검색을 보장하지 않는다.
- PostToolUse의 `index_enqueue.py`는 호스트 payload의 `cwd`에서 설정을 찾고,
  편집 경로를 그 프로젝트의 collection에만 대조한다. 다른 **이미 등록된** 프로젝트
  파일을 절대 경로로 편집해도 그 프로젝트로 자동 라우팅하지 않는다. Claude의
  Bash 변경 관찰(`file_change_queue.py`)은 현재 프로젝트 내부 collection에
  한정된다. Codex 훅 설정에는 Bash 변경 관찰이 없다.
- 현재 `skipPaths`는 recall 결과 필터이며 QMD 인덱싱 제외 정책이 아니다.
  현재 인덱싱 범위는 주로 `collectionPaths`가 정한다. 새로운 소유권 라우터가
  `skipPaths`를 인덱싱 OFF로 간주해서도, OFF/미등록 폴더를 켜서도 안 된다.
- `qmd_scope.scoped_hits`는 반환된 개별 파일의 현재 해시를 색인 해시와
  대조한다. 이전에는 공유 QMD 저장소 경로에만 적용됐고, 단계 A부터 선택된
  프로젝트 로컬 색인 경로에도 적용한다(아래 단계 A 구현 기록). 이것은
  **반환되지 않은 새 파일**이나 한 요청의 게시 generation을 증명하지 않는다.
  기존에 주입된 컨텍스트를 회수할 수도 없다.

## 목표 정책: 변경 소유권과 검색 범위는 별개

1. 변경 이벤트는 호스트의 세션 시작 경로가 아니라, 변경 파일의 **정규화된 실제
   경로**가 속한 이미 등록·승인된 인덱싱 collection의 소유 프로젝트로 라우팅한다.
   현재 세션의 검색 범위는 실제 질의 payload의 작업 경로와 승인된 프로젝트
   route로 정하며, 외부 파일 편집만으로 전환하지 않는다. 일회성 shell `cd`는
   호스트 세션의 작업 경로 전환 증거가 아니다.
2. 소유 후보가 0개면 등록하지 않고 무동작·진단한다. 둘 이상이면 임의의
   최장 경로 추측 대신 모호성으로 보류한다. 중첩 프로젝트, 같은 경로의 중복
   collection, worktree 사이 이름 충돌은 명시적 소유 증거가 있어야 한다.
3. 삭제는 파일이 사라진 **이전 경로의 등록 소유권/스냅샷**으로 판정한다. 이동은
   이전 경로의 삭제와 새 경로의 생성을 각각 승인된 소유자로 처리한다. 링크
   자체와 링크 대상의 소유권을 혼동하지 않으며, 범위 밖으로 향하는 symlink나
   해석 실패는 삭제/이동의 확정 증거로 취급하지 않는다.
4. 미커밋·미추적 파일은 Git 기록이 아니라 현재 허용 파일 집합으로 판정한다.
   detached HEAD도 같은 원칙이다. Git identity는 진단용 보조 증거일 뿐
   내용·범위 검사와 QMD 게시 증거를 대체하지 않는다.
5. pull·브랜치 변경·새 worktree 생성은 파일 집합 변경이고, **실제 호스트 작업
   경로 전환**은 질의 scope 변경이다. 각 worktree는 별도 프로젝트 identity와
   승인된 collection 소유권을 가진다. 새 worktree를 자동 승인하지 않는다.

## 단계별 구현과 완료 기준

검색의 **반환 결과 유효성**과 파일 동기화의 **변경 발견·색인 갱신**은 별도
구현·검증 단위다. 단계 A는 이미 반환된 오래된 결과를 막는다. 새 내용이 실제
검색 결과로 나타나는 책임은 단계 B의 안전한 변경 소유권·enqueue와 단계 C의
색인 게시에 있다. 최종 설치 후보의 실제 검색 E2E는 둘을 연결해 검증한다.
단계 A 통과를 새 파일의 최신 검색 보증으로 확대하지 않는다.

| 단계 | 구현 범위 | 완료 기준 |
|---|---|---|
| A: 검색 안전 최소 단계 | 검색 직전 읽기 전용 파일/색인 최신성 판정, 실패 시 오래된 결과 제외와 진단. 기존 파일 동기화 상태를 변경하지 않는다. | 합성 pull·브랜치·worktree·dirty/untracked·손상 상태 회귀, 검색 hook 예산 준수. 누락된 새 파일의 즉시 검색은 주장하지 않는다. |
| B: 이벤트 소유권 | 등록 collection의 소유권 조회를 파일 동기화 유스케이스에 두고 Claude/Codex 편집 경로를 현재 cwd와 독립적으로 매핑. 삭제·이동 이전 소유 증거를 영속화. | 외부 등록 경로·미등록 경로·중첩·symlink·OFF의 합성 회귀. 소유 후보가 불확실하면 queue를 쓰지 않는다. |
| C: 게시 검색 view | worktree별 비활성 QMD index 준비, 검증 후 원자적 포인터 전환, 요청당 하나의 게시 generation 고정. 늦은 worker는 이전/다른 view를 게시하지 못한다. | 실제 QMD 2.5.3 설치본에서 pull/브랜치/worktree CUD 검색 결과 E2E, 전환 중 검색/재시도/rollback 증거. |
| D: 재사용 최적화 | 내용·파서·청킹·임베딩 모델 fingerprint가 모두 맞을 때만 임베딩 재사용. raw 신규 파일 fallback과 공유 위키 의존 집합 판정. | cache hit와 오염 방지·성능·출처 테스트. 자동 학습과 독립. |

단계 B는 안전하게 쪼갠다. B1은 **현재 존재하는 정규 파일**만 이미 승인·등록된
외부 collection 소유자로 연결한다. 삭제와 이동의 이전 경로는 단순 현재
registry만으로 삭제 전 파일 종류와 소유권을 증명할 수 없으므로 B2에서 이전
snapshot 증거를 이용한다. B1은 현재 세션 검색 scope, 위키 유료 작업, 미등록
worktree 승인을 바꾸지 않는다. B1만으로 같은 세션 신규 파일이 QMD에 게시됐다고
말할 수 없으며 C의 게시 결과와 E2E가 필요하다.

### 단계 B1 구현 기록 (2026-10-17)

- `change_owner.py`가 이미 활성화된 공유 store의 승인 프로젝트·collection
  registry를 읽고 현재 소유자 설정, OFF, 이벤트, indexed role, symlink와 경로
  모호성을 재검증한다. `index_enqueue.py`는 현재 cwd collection 밖의 **존재하는
  정규 파일**만 검증된 대상 프로젝트의 기존 dirty queue로 라우팅한다. 같은
  상대 경로의 다른 worktree를 자동 등록하거나 현재 세션 검색 scope를 바꾸지
  않는다. 큐 직전 파일 inode·설정 hash·소유자를 재확인한다.
- 합성 회귀: `test/change-owner.test.mjs`와 기존 `test/index-enqueue.test.mjs`를
  개발 가드로 실행해 외부 등록 파일, 현재/대상 분리, 미등록·OFF·symlink·중첩·
  중복 collection·낡은 승인·이전 이동 경로 보류를 확인했다. QA fixture 회귀와
  함께 13/13 통과, receipt `cf8bae04415e4b08bce3be90b3265b06`.
- 한계: 삭제·이동 이전 경로의 snapshot 소유 증거(B2), Claude Bash/Codex shell
  변경 관찰, 요청당 단일 게시 view(C), 실제 호스트와 QMD 검색 결과는 미검증이다.
  현재 cwd 소유 경로의 레거시 링크 처리와 공유 store를 읽을 수 없을 때의
  기존 경로 동작은 이번 좁은 변경에 포함하지 않았다. 큐 append 직전 검사 뒤
  발생하는 경합도 아직 남는다. 매 편집의 공유 store `select()` 검증 비용은
  실제 호스트 훅 예산에서 측정하지 않았다.

### 단계 B2a 구현 기록 (2026-10-17, 합성 검증 통과)

- `change_owner.py`가 승인된 present 정규 파일 편집을 개인 공유 store의 제한된
  관측 기록에 남긴다. 사라진 경로는 이 이전 관측, 동일한 승인 설정 hash 및
  등록 revision이 있을 때만 기존 registry·소유 설정을 재검증해 라우팅한다.
  이동의 이전·새 경로는 독립적으로 판정하며, 증거가 없으면 그쪽은 보류한다.
- 등록 revision은 활성 store 세대, 승인 목록 및 registry 파일 identity에
  결속된다. 같은 등록 재시도는 registry를 다시 쓰지 않고, retire·재등록 같은
  실제 변경은 새 파일로 기록한다. 관측 파일은 권한·크기·개수·symlink를
  제한하고, 손상되면 기존 증거를 신뢰하지 않는다.
- 개발 가드 집중 검사: B2a·index enqueue·C1 합성 경합·기존 파일 감지 등
  44/44 통과(receipt `43665fa1a8684aa6a5c4b5ae237d5de1`), 공유 store
  등록·retire·enrollment·setup·cutover 6/6 통과(receipt
  `f897cdfb7a184088b6456c4dfa723372`). 둘 다 `settled:true`.
  증거 ledger와 registry의 원자 교체 뒤 디렉터리 동기화를 추가한 후 관련
  좁은 회귀 4/4도 통과했다(receipt `681e1265358d47d48dadb3452f0ef78f`,
  `settled:true`).
- 범위: 이전에 이 플러그인의 PostToolUse가 받아들인 파일에만 관측이 있다.
  SessionStart snapshot, Claude Bash, Codex shell 변경이나 현재 cwd 소유
  경로의 레거시 삭제에는 이 증거 경로가 적용되지 않는다. registry의 다른
  변경도 전체 관측을 보수적으로 무효화한다. 큐 직전 검증과 append 사이의
  경합, 실제 훅 지연, Linux CI·최종 설치본은 미검증이다.

### 단계 A 구현 기록 (2026-10-17, 소스 변경·합성 회귀 통과)

- 적용 판단: 프로젝트 로컬 색인도 같은 QMD `documents(collection, path, hash,
  active)` 스키마를 쓰고, 범위 해석은 같은 `resolve_paths` 계약을 따른다. 결과의
  경로·해시만 검사하므로 두 라이브 경로에 모두 유효하다. 검사할 색인이 없는
  fixture·전역 daemon 경로는 기존대로 검사하지 않고 `index_guard: none`으로 남긴다.
- 변경: `core/recall.py`가 공유·프로젝트 로컬 경로의 1차 결과와 hierarchical
  raw backfill 결과에 `qmd_scope.scoped_hits`를 적용한다. `core/qmd_scope.py`는
  결과별 제외 사유를 세고(`modified`, `missing`, `not_indexed`, `out_of_scope`,
  `unsafe_path`, `foreign_collection`, `malformed`, `oversize`, `read_error`,
  `index_unavailable`, `scope_unavailable`, `scope_refused`, `row_limit`),
  검사 행을 256개로 제한한다. 사유는 recall 선택 로그에 기록된다.
- 테스트 경로: `QMD_QUERY_FIXTURE`가 있을 때만 `QMD_QUERY_FIXTURE_INDEX`가
  가리키는 합성 SQLite 색인으로 같은 검사를 한다. 운영 QMD DB를 열지 않는다.
- 동작 변화: 프로젝트 로컬 경로의 raw 결과 제목은 QMD 제목 대신 검증된 파일
  이름(stem)이 된다(공유 경로와 동일). `test/fixtures/qmd-pointer-e2e.py`의
  합성 색인에 `hash` 열을 추가하고 기대 제목을 이에 맞췄다.
- 회귀: `test/same-session-worktree-freshness.test.mjs`(수정·삭제·같은 내용 복귀·
  미추적·범위 밖·collection 내부 symlink·worktree 전환·색인 불가·fixture
  무검사 표시·경계 계수). 개발 가드로 해당 테스트, QA fixture 테스트와 기존
  `qmd-pointer-e2e` 합성 테스트를 집중 실행해 4/4 통과했다. receipt:
  `7169f9665761462cb046e9f08126823a`. 실제 QMD·호스트 훅 검증은 아니다.
- 남은 한계: 반환되지 않은 새 파일의 즉시 검색, 이미 주입된 컨텍스트의 회수,
  검색·해시·본문 읽기 사이 동시 편집의 원자성, 요청당 단일 generation은
  단계 B–C의 범위다. QMD가 정규화해 저장한 경로는 fail-closed로 제외될 수 있다.

단계 A만으로 같은 세션의 최신 파일이 검색된다고 말할 수 없다. 단계 C의
실제 설치본 검색 결과와 새 호스트 세션 QA가 정식 릴리즈의 관찰 가능한 관문이다.
이미 주입된 이전 컨텍스트는 철회할 수 없으므로, 변경 감지 시 현재 세션에
명시적으로 재검토 안내를 제공하는 방식은 단계 A의 UX 검증에서 결정한다.

### 단계 C1 요청 경계와 합성 게시 판정 (합성 검증 통과)

- `published_view.py`는 소유 프로젝트·등록 incarnation·worktree·설정 revision·
  store generation·내용 revision을 비교하는 순수 CAS 판정을 제공한다.
  `published-view-race` 합성 fixture는 옛 작업 A를 명시적 장벽에서 멈춘 뒤
  새 작업 B를 먼저 게시하고 A의 게시 거절·재시도 상태를 검사한다.
- 실제 recall은 시작한 route·endpoint·설정 identity를 출력 직전에 다시 읽고
  다르면 결과를 보류한다. 이는 요청 도중 **identity 전환**만 감지한다.
- `test/published-view-race.test.mjs`를 포함한 개발 가드 집중 검사 44/44가
  통과했다(receipt `43665fa1a8684aa6a5c4b5ae237d5de1`). 합성 CAS와
  request identity 단위 시험이며, 운영 DB 게시 검증은 아니다.
- 운영 `index_worker.sh`는 활성 공유 DB를 직접 갱신한다. 따라서 C1은 DB 내용의
  원자적 게시나 늦은 worker 쓰기 차단을 구현하지 않는다. worktree별 비활성 DB
  준비·검증, 운영 게시 CAS/복구, daemon 교체와 요청 전체 generation 고정은
  단계 C2에서 실제 QMD 2.5.3 설치본으로 확인해야 한다. C1 합성 통과를 C 단계
  완료나 최신 파일 검색 보증으로 표시하지 않는다.

## QMD 2.5.3과 플러그인 책임

[QMD 2.5.3 문서](https://github.com/tobi/qmd/blob/v2.5.3/README.md)는 collection
등록·제한 검색, `update`, `embed`, 이름 붙은 `--index`, 콘텐츠 해시 기반
chunk/vector 저장을 설명한다. 같은 문서에 worktree별 게시 generation이나
요청 중 포인터 고정 보장은 명시돼 있지 않다. 따라서 기능 존재를 가정해
운영 데이터에 실험하지 않는다. 플러그인은 등록 소유권·파일 snapshot·검색
scope·게시 증거를 담당하고, QMD의 실제 지원 범위는 격리 설치본과 합성
index에서 확인한다.

## 릴리즈와 검증 기록

정식 릴리즈 판단은 소스 테스트 외에 최종 설치 후보의 실제 Claude/Codex 새
세션, pull/브랜치/worktree의 추가·수정·삭제 검색 결과, 설치·업데이트·복구,
QMD/Laya 준비, 위키 중복/출처, 자원 가드와 일반 작업 비차단 QA가 필요하다.
실제 학습·모델 다운로드·운영 DB 변경은 이번 단계의 검증이 아니다.
완료되지 않은 단계와 실제 호스트 검증은 PASS로 기록하지 않는다.

2026-10-17 전체 `npm test`는 **PASS가 아니다**. 합성
`test/shared-install.test.mjs`의 baseline이 자체 120초 제한을 넘었고,
테스트 본체 종료 후에도 이 실행 소유의 QMD HTTP 자식 1개가 남아 가드가
완료를 보류했다. 가드의 정상 중단 경로로 정리하여 receipt
`71d0a9fda5614206b4e13428657991e6`은 `stopped/settled:true`이며
자식 소멸을 확인했다. 같은 baseline만 개발 가드에서 다시 실행한 결과
19개 합성 사례가 119,998ms에 통과했다(receipt
`b78e33f38e2b46e5ba0879394c2caf38`). 제한에 너무 가까우므로 안정적
전체 통과로 간주하지 않는다. 실제 설치본·호스트 QA는 별개로 미실행이다.

### `shared-install` 시간·자식 수명 진단 후속 (2026-10-17)

- 하위 사례 계측을 넣어 단독 실행한 baseline 19개는 118,153ms에 통과했다.
  가장 느린 단일 사례는 `test_modern_v1_wiki_reuses_existing_store_without_bulk_migration`
  13,002ms였고, 나머지 사례도 각자 전체 합성 설치·재개를 수행한다. 기존
  120,000ms는 **19개 누적**에 걸려 있어 1.8초의 여유만 남았다. source 4개는
  53,582ms에 통과했다(receipt `b05a98e8822e476da88e861336c0b770`).
- `test/shared-install.test.mjs`는 이제 정확한 사례 목록(19+4)을 확인하고
  각 사례를 별도 Python 프로세스에서 **25,000ms 개별 제한**으로 실행한다.
  전체 사례를 한 번에 120초로 묶지 않는다. 기존 Python group CLI는 유지하고,
  `--list`/`--case`와 사례명·단조 시계 진단을 추가했다. 변경 뒤 개발 가드에서
  23개 하위 사례와 두 그룹을 모두 통과했고 `settled:true`였다(receipt
  `383045baea6f432c9f5a795a322630dc`). 최대 단일 사례는 source의
  `test_continuously_changing_source_defers_and_retains_queue_for_resume` 약 18.98초다.
- **상한의 의미:** 목록 확인은 그룹당 15초, 각 사례는 Python 프로세스당 25초다.
  23개가 모두 상한에 닿으면 사례 실행만 이론상 575초까지 걸리므로 이 wrapper
  안에는 종전 120초짜리 그룹 상한이 없다. 전체 `npm test`/집중 검사에는
  `scripts/development_guard.py`의 **1,800초 전역 wall-time 상한**과
  2~10초 주기 소유 프로세스 관측·정리 및 단독 실행 lock이 적용된다.
  `spawnSync`의 사례 timeout은 직접 Python 자식을 끝내지만, 의도적으로
  분리된 writer/daemon을 모두 소유 확인해 끝낸다는 보증은 아니다.
- **남은 별도 문제:** 이전 전체 검사에서 약 318초째 시작된 QMD HTTP 자식의
  발생 테스트는 아직 증명되지 않았다. Python fixture의 worker는 의도적으로
  별도 session에서 지속되며 timeout 시 직접 자식만 SIGTERM을 받는다.
  fixture 소유 PID·시작 시각·HOME과 QMD endpoint 연결이 입증되지 않아
  임의의 process kill은 추가하지 않았다. 이전 전체 실행의 자식은 가드 정상
  중단으로 정리됐고, 이번 단독 실행 가드는 자식 없이 종료했다. 이 불확실성을
  전체 suite PASS나 자식 수명 문제 해결로 표시하지 않는다.

### 전체 회귀 재현과 fixture 격리 (2026-10-17)

- 첫 `npm test`(가드 run `23b9102e77244644af74a866ff43e8ca`)에서
  약 323초에 실제 관리 QMD HTTP 자식 PID 18987이 **이번 가드의 소유**로
  관측됐다. PPID 1로 재부모화됐지만 PGID는 테스트 runner의 1322였다.
  `dispatcher.test.mjs`의 posttool fixture가 실제 `backend_manager.sh ensure`를
  백그라운드 호출할 수 있는 경로를 확인했다. 당시 Node worker가 종료돼
  PID→테스트 파일명을 직접 채집하지 못했으므로 단일 호출의 생성 증명으로
  과장하지 않는다. 합성 dispatcher 기본 manager를 `/bin/true`로 고정했다.
- 첫 runner는 PyYAML 부재로 아래 4건이 실패한 뒤 종료했다. 소유 QMD 자식이
  남아 이번 실행의 가드 PID만 확인 후 SIGTERM을 보내 가드의 identity 기반
  정리 경로로 종료했다. checkpoint는 `stopped/interrupted/settled:true`였고
  소유 PID 18987의 소멸을 따로 확인했다. 다른 운영 PID를 신호하지 않았다.
- 수정 후 dispatcher·C1·C2a·`shadow_index` 집중 검사 20/20 통과,
  가드 `passed/settled:true`(run `2a9a0bbfc6f44f30b3d0eb430d673a5e`).
  이어진 두 번째 전체 회귀(가드 run `85cd4ca95228499f945f87f177c0f591`)는
  **1,580건 중 1,574 PASS·4 FAIL·2 SKIP**였다. 실패 4건은 모두
  `test/recall-injection-body.test.mjs`에서 `yaml`을 import하거나
  PyYAML 표준 파서 대조를 요구한다. 이 호스트의 `python3`은 Command Line
  Tools Python 3.9.6이며 `yaml` 모듈이 없다. 두 번째 실행은 약 1,242초에
  `failed/settled:true`, 소유 프로세스 0개로 종료했고 실제 QMD HTTP 자식은
  재발하지 않았다. 이는 전체 **PASS가 아니며** 운영 host QA도 아니다.
- 저장소에는 Python 의존성 선언과 `.venv`가 없고, PATH·일반적인 설치 경로에서
  PyYAML을 가진 대체 Python을 찾지 못했다. PyYAML 설치가 필요하면 시스템
  Python이나 운영 런타임 대신 이 회귀 전용 격리 venv에만 한정해야 한다.
  이번 단계에서 패키지 설치나 환경 변경은 하지 않았다.

### 승인된 테스트 전용 Python 환경 (후속)

사용자 승인 뒤 개발 작업 폴더의
`/Users/dulee/Documents/Codex/2026-10-17/task/.qmd-test-venv`를 새로 만들고
공식 [PyPI PyYAML](https://pypi.org/project/PyYAML/)의 **6.0.3**, CPython 3.9
macOS ARM64 wheel만 `--no-deps --only-binary=:all: --no-cache-dir`로 설치했다.
실제 import 경로는 이 venv의 `lib/python3.9/site-packages/yaml/__init__.py`다.
전역 Python·운영 플러그인과 다른 패키지는 변경하지 않았다. 이 venv의
`bin`을 `PATH` 앞에 둔 개발 가드 집중 검사에서 이전 실패 4건을 포함한
`test/recall-injection-body.test.mjs` **47/47 PASS**, `settled:true`였다
(run `31747bacc38f4a64a61c30abaa471f5d`). 전체 회귀 재검증은 별도다.

### 전체 합성 회귀 완료 (후속)

venv 자체를 `PATH` 앞에 둘 경우 `test/helpers/uid_home.py`의 개인 사용자
site 전제가 깨져 shared-install fixture가 실패했다. 시스템 Command Line Tools
Python 3.9.6을 그대로 사용하고 이번 검사 명령에 한해서 venv의
`lib/python3.9/site-packages`를 `PYTHONPATH`로 제공해 PyYAML만 import했다.
운영 Python 설치나 프로젝트 설정은 바꾸지 않았다.

학습·위키·설치·런타임 판단 경계의 신규 검사 4개는 fixture의 tuple-key dict
생성 오류를 고친 뒤 4/4 통과했다. 영향 영역 집중 검사 90개 중 89개가 통과했고
기존 `runtime_work.MAX_WAKES`를 임시 변경하는 사례 한 건이 새 정책 모듈에서
반영되지 않는 문제를 발견했다. 정책 함수에 현재 용량 값을 넘기도록 수정한 뒤
런타임 집중 검사 2/2(그 안의 Python 111개 사례 포함)가 통과했다(receipt
`fee2ead5722e40e9a733ae27ee874535`, `settled:true`).

최종 `npm test`는 **1,584건 중 1,582 PASS·0 FAIL·2 SKIP**,
가드 `passed/settled:true`로 종료했다(run
`42d0c8947e4f48f5b7b187cd61d3ec1f`). 설치 baseline 19건·소스 안정성
4건도 전체 실행 안에서 통과했다. 이는 소스와 합성 fixture 검증이며,
실제 공유 DB 게시 전환·새 Claude/Codex 세션·운영 QMD/Laya 모델 검증은
아직 남아 있다.
