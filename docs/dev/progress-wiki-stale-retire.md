# 위키 stale·은퇴 수정 진행 기록

## 레인 E: 대시보드 위키 배지, 체크아웃 상태 표시, 상위 `.git` 브랜치

기준 main `57ab7f88`. 대시보드는 레지스트리·카드·원장을 읽기만 한다. 쓰기·잠금·프로세스 기동이 없고 유료 호출도 없다. 영속 원장 상태 조합표는 레지스트리 상태별 표시 표로 대신한다.

### 표 E1. 레지스트리 행 → 체크아웃 상태

입력 행은 모두 `registry.api.after_setup`·`registry.api.reconcile`이 실제 파일·route 변화를 관측해 쓴다. 예외는 E1-11의 다른 claimant 행 하나다. onboarding이 선택된 루트에 두 번째 설치를 거절하므로(`project_selection_already_exists`, `initial_build_already_registered`) 레지스트리 자체 conflict 시험과 같은 방식으로 한 행을 넣는다. 두 행의 `conflict` 상태는 setup 등록 writer가 쓴다.

| # | 만드는 사건 | 레지스트리 status/reason | `viewState` | 브랜치 칸 | 점 | 기본 표시 | 유료 |
|---|---|---|---|---|---|---|---|
| E1-1 | main 선택·등록 | active / – (실시간 증명 성공) | `ready` | 브랜치명 | 녹색 | 보임 | 0 |
| E1-2 | 연결 worktree 등록 | active / – | `ready` | 브랜치명 | 녹색 | 보임 | 0 |
| E1-3 | worktree 디렉터리 삭제, git은 아직 나열 | active / – (루트 없음) | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-4 | `git worktree remove` 뒤 reconcile | detached / released | `released` | "해제됨" | muted | 토글 | 0 |
| E1-5 | main 루트 이동, 관측 1회 | missing_pending / – | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-6 | main 루트 이동, 하루 간격 관측 2회 | detached / missing | `path_missing` | "경로 없음" | 빨강 | 토글 | 0 |
| E1-7 | origin restore 뒤 reconcile | detached / released | `released` | "해제됨" | muted | 토글 | 0 |
| E1-8 | 루트 inode 교체 뒤 reconcile | detached / root_moved | `moved` | "이동됨" | muted | 토글 | 0 |
| E1-9 | route 손상, 루트 있음 | active / – (`proof=unproved:…`, 실시간 증명 실패) | `proof_failed` + `stateReason=route_…` | 브랜치명 | 빨강 | 보임 | 0 |
| E1-10 | E1-9 뒤 route 복구 | active / – (`proof`는 아직 `unproved:…`) | `ready` (실시간 증명이 우선) | 브랜치명 | 녹색 | 보임 | 0 |
| E1-11 | 두 설치가 같은 루트를 주장 | conflict / – (양쪽) | `conflict` | "충돌" | 빨강 | 토글 | 0 |
| E1-12 | `detached/withdrawn` | detached / withdrawn | `released` | "해제됨" | muted | 토글 | 0 |
| E1-13 | cold worktree view | – | `view_preparing` | 브랜치명 | amber | 보임 | 0 |
| E1-14 | worktree 상위 디렉터리 권한 거부(chmod 0) | active / – (루트 lstat EACCES) | `unreadable` + `stateReason=EACCES` | "⎇ unknown" | amber | 보임 | 0 |
| E1-15 | E1-9 행, `max_installs=0`(실시간 증명 생략) | active / – (`proof=unproved:<code>`) | `proof_failed` + `stateReason=<code>` | 브랜치명 | 빨강 | 보임 | 0 |
| E1-16 | DDL 밖 조합(`detached`/None, 미래 status 등) | 그 값 | `unknown` + `stateReason=<status>/<reason>` | 브랜치명 칸 `unknown` | amber | 보임 | 0 |

- E1-1~E1-11, E1-14, E1-15: `memvane/ui/tests/integration/test_overview_states.py::test_registry_states_map_to_view_states`가 status/reason, `viewState`, 브랜치, `stateReason`, 노드 수(12)를 단정한다. 어느 노드도 `non-git`을 쓰지 않는다. E1-14의 worktree는 실제 `git worktree add`와 setup 등록으로 만든다.
- E1-16: 현재 DDL CHECK로는 생기지 않아 실제 writer 행이 없다. `ViewStateMappingTests.test_an_unknown_status_or_reason_is_unknown_with_its_values`가 매핑 함수만 단정한다.
- `ready`·`view_preparing`이 아닌 행은 `resolve_target`이 404(`target_unknown`)나 409(`target_unproved`)로 거절한다. 프런트는 그 행의 선택과 Wiki·Recall 바로 가기를 막고 툴팁에 상태·이유를 적는다. KPI는 토글과 상관없이 숨기지 않은 행만 센다.
- E1-12: DDL은 `withdrawn`을 허용하지만 현재 레지스트리 writer(`memvane_registry.py`)와 `git log -S withdrawn -- memvane/registry`에 쓰는 곳이 없다. 실제 writer 행이 없어 시험하지 않고 매핑만 둔다.
- E1-13: `view_preparing`은 recall reader 오류(`journal_reader`)다. 레지스트리만 읽는 Overview가 worktree별로 알 수단이 없어 백엔드는 내지 않는다. 프런트 계약과 점 색(amber)만 유지한다.
- worktree 행의 `proof`는 레지스트리 `_sync_worktrees`가 항상 `ok`로 쓴다. Overview는 git을 실행하지 않으므로 worktree를 실시간으로 증명하지 않는다. 따라서 worktree의 `proof_failed`는 레지스트리 `proof`가 `unproved:`일 때만 나오고 현재 writer로는 생기지 않는다.
- 점은 체크아웃 상태만 칠한다. SLO는 Overview의 SLO 칸과 KPI 배지에만 남는다.

### 표 E2. 카드 freshness → 배지

카드는 project fixture의 카드 writer(`card.dump_source_revisions`로 `sourceRevisions`를 쓴다)가 쓰고 원문은 실제 파일 변경·삭제로 바꾼다.

| # | 상황 | `check_card` | `stale` / `staleReason` | 배지 | 시험 |
|---|---|---|---|---|---|
| E2-1 | 원문 그대로 | fresh | false / null | 없음 | `test_main_list_filters_and_ledger`, `test_stale_cards_name_a_deleted_or_a_changed_source` |
| E2-2 | 원문 해시 변경 | stale / `content_hash_mismatch` | true / `source_changed` | "원문 변경"(amber) | `test_stale_cards_name_a_deleted_or_a_changed_source` |
| E2-3 | 원문 삭제 | stale / `missing` | true / `source_deleted` | "원문 삭제"(red) | 같음 |
| E2-4 | 원문 2개, 첫째 변경·둘째 삭제 | stale / `content_hash_mismatch`(첫 원문에서 멈춤) | true / `source_deleted`(원문별 재확인) | "원문 삭제" | 같음 |
| E2-5 | 판정 불가(allow root 밖, 읽기 실패 등) | unknown | false / null | 없음 | 기존 동작 유지 |

- 필터는 `stale` 하나다. 버튼 툴팁이 두 종류를 설명한다.
- 판정은 원문마다 `check_card`를 원문 하나로 한 번씩 부르는 단일 루프다. 첫 non-fresh 원문이 stale이면 카드가 stale이고(전체 카드 `check_card`와 같은 판정), 그 뒤의 삭제 원문도 같은 루프에서 찾는다. 원문은 많아야 한 번 해시하고 삭제 원문은 해시하지 않는다. `test_each_source_is_checked_once_in_one_pass`가 호출 수(변경·삭제 순 2, 변경·fresh·변경 3, fresh·삭제 2, fresh 1)와 호출마다 원문 1개를 단정한다.
- 원문 삭제 카드의 lifecycle HOLD 사유는 붙이지 않았다. 사유는 `logs/wiki-lifecycle.log`의 자유 문장과 `dry_run`(git 실행) 결과에만 있다. 공개 `api.py`에 읽기 경로가 없고, 로그 문구는 계약 밖이며, Overview·Wiki는 프로세스를 띄우지 않는다.

### 상위 `.git` 탐색

`read_adapters._dotgit`은 루트에서 위로 첫 `.git`을 찾는다. git 기본 탐색처럼 파일 시스템 경계에서 멈추고, HOME 아래 루트는 HOME 위로 올라가지 않는다. 코어는 import하지 않는다. `.git`이 디렉터리면 그대로, 일반 파일이면 `gitdir:` 포인터를 4 KiB 상한으로 읽고, 그 밖(심볼릭 링크 등)이면 `unknown`이다. `HEAD`는 `O_NOFOLLOW`·4 KiB 상한으로 읽는다. 루트가 없거나 관측할 수 없는 행은 브랜치를 읽지 않는다.

코어와의 차이: `store.checkout.checkout_view._dotgit`은 경계·HOME 없이 `/`까지 올라간다. 장치 경계 너머의 `.git`은 코어도 뒤이은 `git rev-parse`가 실패해 브랜치를 얻지 못하므로 결과는 같다. HOME 위 저장소(예: `/Users/.git`)는 코어와 git이 찾지만 UI는 `non-git`으로 보인다. 코어는 고치지 않았다.

- 시험: `test_sse_and_reads.py::test_the_walk_stops_above_home_and_at_a_file_system_boundary`(HOME 위 저장소 → `non-git`, HOME 자체 저장소 → 그 브랜치, `os.lstat`의 `st_dev`를 바꾼 경계 → `non-git`), `test_sse_and_reads.py::test_a_folder_inside_a_checkout_reads_the_nearest_git_above_it`(저장소 하위 폴더 → `main`, 연결 worktree 하위 폴더 → `side`, 상위 `.git` 심볼릭 링크 → `unknown`), `test_overview_states.py::test_a_project_folder_inside_a_repository_reads_the_repository_branch`(저장소 하위 폴더에 온보딩한 실제 설치 → Overview `main`).

### 호출부 (CodeGraphContext `repo_path`=이 worktree, `rg` 보완)

| 바꾼 기호 | 제품 호출부 | 시험 호출부 |
|---|---|---|
| `wiki_adapters._is_stale` → `_stale_reason` | `wiki_adapters._card_meta` ← `wiki_list` ← `server` | `test_wiki_api` |
| `read_adapters.git_dir` | `read_adapters.read_branch` | (간접) |
| `read_adapters.read_branch` | `read_adapters._target_row` ← `overview` | `test_sse_and_reads.BranchTests` 3개 |
| `read_adapters._prove`(시그니처 유지, `_proof` 위임) | `resolve_target`, `runtime_adapters._proved_installs` | `test_overview`, `test_wiki_api`, `test_recall_api`, `test_runtime_learning_api` |
| `read_adapters._proof`(신규) | `_prove`, `overview` | `test_overview_states` |
| `read_adapters._view_state` | `_node_view` | `test_overview_states` |
| `read_adapters._node_view` | `overview`(main·worktree 2곳) | `test_overview`, `test_overview_states` |
| `read_adapters._target_row` | `overview` | 같음 |
| DTO `viewState`·`stateReason`(모든 체크아웃 노드), `staleReason` | `ui/src`: `checkoutState.ts`, `dormantPref.ts`, `SidebarTree`, `OverviewPage`, `Topbar`, `WikiPage` | – |
| `read_adapters._root_state`(신규, `_present` 대체) | `_target_row` | `test_overview_states` |

체크아웃 상태 타입은 `CheckoutViewState`이고 main과 worktree 노드 모두에 붙는다. 이 DTO는 1.x 영속 계약 밖이며 프런트와 백엔드가 한 번들로 함께 배포된다.
