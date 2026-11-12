# 선택 프로젝트 레지스트리

`core/memvane_registry.py`는 사용자 단위 조회 색인이다. HOME state base 아래
소유자 전용 SQLite 파일 `~/.local/state/memvane/registry/projects.sqlite` 하나에
선택된 main checkout과 그 연결 git worktree의 경로를 기록한다. 레지스트리 행은
조회 후보일 뿐 권한이 아니다. 소비자는 사용 직전에 기존 선택 route 증명(route
기록·설치 journal·attestation)을 그대로 수행하고, 행만으로 recall·외부 편집
알림·쓰기·재선택을 허용하지 않는다.

레지스트리는 자기 SQLite 파일에만 쓴다. journal·route·ledger·marker·store·큐·
intent를 쓰지 않고, SQLite 자체 파일 잠금 외의 lock, wake kind, hook action을
두지 않는다.

## 행과 상태

행은 install ID, state root, route가 기록한 project root, 파일시스템 표기의
대상 root(`target_root`), 종류(`main`·`worktree`), 부모 install, journal
`scopeId`(incarnation), 대상 root inode, 상태, 사유, 마지막 증명 결과(`proof`),
소실 관측 정보, 마지막 관측 시각, 행 버전을 가진다.

| 상태 | 뜻 |
|---|---|
| `active` | 조회 후보 |
| `missing_pending` | root가 없다. 즉시 조회에서 빠진다 |
| `detached` | 사유 `released`(증명된 해제), `missing`(소실 확정), `root_moved`(같은 경로의 inode 변경) |
| `conflict` | 두 install이 같은 root를 주장한다. 어느 쪽도 후보가 아니다 |

`missing`·`root_moved` 행은 tombstone이다. SessionStart seed와 backfill 같은
암묵 등록은 tombstone을 바꾸지 않고, 같은 경로의 다른 install도 새로 넣지
않는다. 새 route 증명을 가진 명시적 활성화(`activate-index-origin`,
`active`로 끝난 `recover-index-origin`)만 tombstone을 교체한다.

## 쓰기 지점

- 활성화 성공 뒤 `setup_entry`가 `after_setup`으로 route를 다시 증명해 main 행을
  idempotent upsert하고 유계 reconcile을 한 번 돈다.
- `restore-index-origin`·`retire-index-origin`·`abandon-index-build`·
  `abandon-inactive-index-build`가 성공하면 명령 뒤 재관측(route가 그 install을
  가리키지 않고 journal이 `active`·`restored_old`가 아님)이 성립할 때만 그
  install의 행을 `detached`(`released`)로 바꾼다. `blocked`·`recovery_required`·
  `rejected`나 재관측 실패는 행을 바꾸지 않는다. 해제 명령은 행을 새로 넣지 않는다.
- 선택된 main checkout의 SessionStart hook이 자기 행을 seed하고 유계 reconcile을
  한다(`HOOK_ROWS` 4행, `HOOK_BACKFILL_PAGE` 8항목, `HOOK_SECONDS` 0.5초).
- `snapshot()`(대시보드 조회)이 기본 예산(32행, backfill 32항목, 2초)으로
  reconcile한 뒤 읽는다.

어느 쓰기도 명령 결과·exit code·hook 출력을 바꾸지 않는다. 레지스트리 쓰기
실패는 다음 기회의 reconcile이 복구한다. state root가 HOME state base 바로 아래가
아닌 설치(fixture state root 등)는 기록하지 않는다. resident service는
레지스트리를 reconcile하지 않는다.

## Reconcile

- 한 번의 pass는 저장된 커서 다음부터 `active`·`missing_pending` main 행을 행 수와
  시간 예산 안에서 확인하고, 남은 예산으로 backfill한다. 한 행의 실패는 다음 행을
  막지 않는다. 파일·git·journal 검사는 트랜잭션 밖에서 하고, 저장은 관측 때의 행
  버전 compare-and-set이다. 경쟁에서 진 행은 다음 pass가 다시 본다.
- root가 없으면 즉시 `missing_pending`이다. 60초 이상 떨어진 독립 관측 2회와 최초
  소실 뒤 24시간이 모두 지나면 `detached`(`missing`)다. 유예 중 같은 inode와 선택
  증명이 돌아오면 `active`로 복구한다. `detached` 뒤에는 자동 복귀하지 않는다.
  권한 오류 같은 관측 불가는 소실로 세지 않는다. 저장된 소실 시각이 미래면 그
  관측을 첫 관측으로 다시 센다.
- inode가 바뀐 root는 `detached`(`root_moved`)다. 프로젝트 root 이동은 자동으로
  경로를 바꾸지 않는다.
- route 증명이 실패하면 해제가 증명된 경우에만 `detached`(`released`)이고, 그 밖의
  실패는 상태를 두고 `proof`에 `unproved:<사유>`를 남긴다. 같은 install의 route가
  다른 state root를 가리키면(state move 완료) state root를 갱신한다.
- main 행을 확인하거나 등록할 때 유계 `git worktree list`와
  `laya_index_host_route.select_route`의 `worktree` 판정(같은 common git dir, 자기
  route 없음, 같은 settings 정체성)으로 연결 worktree를 `worktree` 행으로 등록한다.
  성공한 목록에서 사라진 worktree만 `detached`(`released`)다. git이 답하지 못하면
  worktree 행을 바꾸지 않는다. main이 `detached`가 되면 그 install의 worktree 행도
  같은 사유로 `detached`다. worktree 행의 schema3 view ID와 view 준비 상태는 아직
  기록하지 않는다.
- backfill은 현재 state base와 이전 state base(`~/.local/state/laya`)를 각자의
  커서로 페이지 단위로 훑고, journal이 선택 상태인 install을 route 증명 뒤 암묵
  등록한다. 열거가 끝까지 성공한 base만 완료 시각을 얻는다. 열거 실패·중단은 완료가
  아니다. 완료 뒤에도 `RESCAN_SECONDS`(600초)마다 다시 훑어 구 번들이 만든 설치를
  찾는다. 두 base에 같은 install이 있으면 route가 가리키는 state root만 등록된다.

## 소비자

- `routing_rows()`: `active` main 행과 backfill 완료 여부.
- `lookup(path)`: 경로 구성요소 경계의 최장 일치. 최장 일치 root에 행이 둘 이상이거나
  `conflict`면 `ambiguous`다.
- `snapshot()`: 대시보드용 전체 행·base별 완료 시각·`stale` 표시. 손상·미지원
  schema·안전하지 않은 경로는 `recovery_required`로 보인다.
- `laya_external_edits`의 대상 탐색은 backfill이 완료된 정상 레지스트리에서 state
  base를 열거하지 않고 `active` main 행의 state root만 증명한다. backfill이 미완료면
  기존 유계 열거에 레지스트리의 tombstone·`conflict`·비활성 행을 적용한다.
  레지스트리가 없거나 손상·busy·미지원 schema·안전하지 않으면 열거로 대체하지 않고
  라우팅하지 않는다.

## 실패와 호환

- 부재·손상·busy·미지원 schema·안전하지 않은 경로는 `RegistryUnavailable`이다.
  hook 읽기는 busy 대기 50ms로 끝난다. 레지스트리를 삭제·재생성하지 않고, 미지원
  schema나 손상된 파일에는 쓰지 않는다. 0바이트 파일은 쓰는 쪽이 schema를 만든다.
- SQLite 기본 rollback journal 모드를 써서 py3.9 hook이 새 python이 쓴 파일을 읽는다.
- 구 번들은 레지스트리를 읽지 않는다. `registry/` 디렉터리는 install ID 형식이 아니라
  구 번들의 state base 열거에서 install로 다뤄지지 않는다. 구 번들의 설치·해제는
  다음 backfill·reconcile이 반영한다. tombstone은 구 번들 동작을 막지 않는다.
- 같은 프로젝트의 직접 세션은 레지스트리와 무관하게 기존 route 증명으로 동작한다.

## 진단

`python3 -B core/memvane_registry.py status`는 reconcile 없이 `snapshot`을 JSON으로
출력한다.
