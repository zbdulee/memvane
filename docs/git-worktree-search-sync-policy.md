# Git checkout·worktree 검색 동기화 정책

SessionStart 스캔, 위키 source queue 등록과 단계 A의 반환 결과 최신성 검사는
legacy QMD route의 정책이다. 단계 B1·B2a의 등록 소유자 라우팅은 공유 store의
등록 정보를 판정 근거로 쓰며, 선택된 Laya route도 `laya_owner_projection`을 거쳐
같은 판정(`change_owner.resolve_change`)을 사용한다.

선택된 Laya route(binding store가 있는 v2)의 recall은 hook 안에서 모델 작업 없이
완료된 index generation 하나를 pin하고 위키 카드만 검색한다. 색인 DB에 게시된
위키 본문과 해시·세대·범위·명시 제외를 확인하며, 현재 원문 파일이나 pending
refresh 원장을 다시 읽지 않는다. 원문 변경 뒤 위키 생성·색인 게시가 끝나기
전에는 마지막 완료본이 검색된다. binding store가 없는 구형 선택 경로는 질의 시
live scan을 사용한다. 흐름은 [위키 색인과 검색 주입](architecture/flows/03-wiki-index-search.md)에
정리돼 있다.

## 현재 반영 범위

플러그인은 Git 명령을 실행해 원격 변경을 받아오지 않는다. 사용자가 `git pull`,
checkout, 파일 편집을 마친 뒤 SessionStart가 현재 프로젝트의 허용된 collection
경로를 스캔한다. `sessionStartSync`의 기본값은 `true`다. 설정 파일에서
`"sessionStartSync": false`로 명시한 프로젝트는 기존 결정을 보존하며 감지하지
않는다(`session_start_sync_disabled`). bool이 아닌 값은 기본값으로 되돌리지 않고
OFF로 처리한다. 이 경우 stderr 경고와 `ok: false`,
`reason: "session_start_sync_invalid"`를 보고하고 종료 코드는 0이다. OFF이면
`session_start_source` 동의가 있어도 이 SessionStart 위키 큐 등록은 실행되지
않는다. SessionStart 훅에서는 stderr가 훅 로그에 기록되고 설정 오류는 별도
알림으로도 표시된다. `events`에서 `sessionStart`를 끈 프로젝트도 훅이 시작되지 않는다.
설정 파일을 자동으로 다시 쓰는 마이그레이션은 없다.

스캔은 파일의 생성·수정·삭제를 collection별 스냅샷과 비교해 인덱싱 대상
collection을 기존 dirty queue에 넣는다. SessionStart는 매 회차 허용된
collection의 모든 Markdown 파일을 읽어 SHA-256을 계산하므로 동일한 크기와
mtime으로 바뀐 파일도 감지한다. 변경이 없는 회차에도 Markdown 총 크기에
비례하는 읽기·해시 비용이 든다. 수동 sync는 이 내용 해시 비교를 켜지 않는다.
첫 실행에 이전 스냅샷이 없고
별도의 SessionStart 위키 컴파일 동의가 없으면, 이미 완료된 QMD update/embed
뒤 현재 상태를 기준점으로만 기록한다. 이후 변경부터 큐에 넣는다. 스캔의
일부 파일·하위 폴더의 I/O 오류는 삭제로 기록하지 않고 그 경로의 이전
스냅샷을 유지한다. 루트 전체를 읽을 수 없으면 해당 회차를 실패시킨다.
collection 루트 자체가 없어졌을 때도 삭제로 확정하지 않고 이전 상태와
`missing_root` 경고를 유지한다.
기존 snapshot 파일이 있는데 읽을 수 없거나 형식이 맞지 않으면 원본 바이트를
보존하고 이전 상태나 첫 실행으로 취급하지 않는다. 수동 sync(`--dry-run`과
`--baseline-only` 포함)와 SessionStart 모두 스캔·dirty queue·위키 source queue
등록 없이 `state_unreadable`(종료 코드 3)로 끝난다. 자동 복구는 하지 않는다.
SessionStart 훅은 이 결과를 훅 로그에 기록하고 계속 진행하므로, 손상 상태의
세부 사유는 해당 로그에서 확인해야 한다.
동기화는 QMD update/embed가 성공한 뒤에 수행하며, 기존 setup/자원 가드가
준비되지 않으면 진행하지 않는다. 따라서 SessionStart 호출 자체가 즉시 검색
가능성을 보장하지는 않는다.

위키 컴파일은 별도 동의다. `compile.mode`가 켜져 있고
`compile.triggers`에 `session_start_source`가 명시된 경우에만 새로 감지한
원문을 위키 source queue에 넣는다. 이 옵션을 생략하거나 `compile.mode: off`인
기존 프로젝트는 파일 감지만 적용된다. `capture`, `autoCycle`, 모델 학습 설정은
이 옵션과 무관하며 변경하지 않는다.
첫 실행부터 `session_start_source`를 명시한 프로젝트는 최대 50개 원문을 source
queue에 등록할 수 있다. enqueue 뒤 snapshot 저장에 실패하면 재시도에서 같은
원문이 다시 등록될 수 있다.
위키 source queue를 채우는 경로는 각자 상태를 사용한다. 해시를 계산하는
경로(SessionStart sync, file-change worker, 수동 sync)의 행은 원문
revision(`contentRevision`)을 가진다. enqueue는 `(collection, path, revision,
trigger)`가 같은 행이 아직 live queue에 있으면 새 행을 넣지 않는다
(`wiki_source_intent.revision_key`). 이 키는 trigger를 포함하고 이미 claim·소진된
행은 보지 못하므로 경로 간·회차 간 중복 등록은 막지 않는다. PostToolUse 행에는
revision이 없다. 선택된 Laya route의 compile worker는 신뢰 카드가 같은 revision을
이미 증명하면 trigger와 무관하게 extractor를 실행하지 않는다
(`already_compiled_revision`). legacy QMD route에는 이 검사가 없으므로 둘 이상의
경로에 동의하면 같은 원문의 유료 추출이 중복될 수 있다.

- 수동 sync: 자체 snapshot과 `post_sync_source` 또는 `post_tool_source` 동의
- SessionStart sync: 별도 snapshot과 `session_start_source` 동의
- PostToolUse 편집 훅: 편집 이벤트와 `post_tool_source` 동의
- 지원 대상 generated story의 `file_change_queue`: 자체 snapshot을 쓰며 event
  회차는 `post_sync_source`·`post_tool_source`, reconcile 회차는
  `session_start_source` 동의를 따른다.

`post_tool_source` 하나로도 PostToolUse와 수동 sync가 함께 허용될 수 있다.

## 검색 일관성 목표와 후속 설계

다음 항목은 QMD route의 **정책 목표**이며, 구현된 기능이라고 해석하면 안 된다.

- 검색 가시성은 실제 요청을 수행하는 checkout/worktree별 현재 파일 집합을
  따른다. dirty 작업 파일도 허용된 collection 범위라면 그 worktree의 내용으로
  취급한다. 자동 `git pull`은 하지 않는다.
- 콘텐츠 캐시 재사용 키에는 바이트 해시뿐 아니라 파서, 청킹 및 임베딩 모델의
  버전·설정을 포함한다. rename은 새 경로로 교체하되 같은 콘텐츠의 유효한
  파생물은 재사용할 수 있다. 삭제는 해당 view에서만 제외하며 공유 콘텐츠
  캐시의 GC와 worktree 삭제를 별도 절차로 둔다.
- 신규·수정 파일은 현재 raw 파일 경로를 먼저 사용할 수 있어야 한다. 임베딩과
  위키 생성은 후속 단계다. **현재 SessionStart 경로는 raw 즉시 검색 fallback이나
  새 파일의 즉시 주입을 구현하지 않는다.** 검색 직전에는 반환된 결과만
  확인한다(아래 단계 A). 반환되지 않은 새 파일의 확인은 후속 작업이다.
- 갱신 중에는 이전 브랜치 결과를 조용히 주입하지 않는다. 한 검색 요청은
  게시된 단일 generation만 사용해야 한다. 준비 중인 generation의 부분 결과를
  현재 view와 섞지 않는다.
- main 공유 위키는 현재 checkout과 모든 관련 원문 의존성이 일치할 때만
  재사용한다. 기존 파일의 해시가 같아도 파일 추가·삭제로 전체 의존 집합이
  바뀌면 무효화한다.
- 읽기 실패, 권한 거부, 일시적 I/O 오류는 파일 삭제의 증거가 아니다. 해당
  경로의 이전 view를 유지하고 오류를 드러낸 뒤 재시도한다. 읽을 수 있는
  collection 스캔에서 파일 부재가 확인되면 현재 view에서 제외한다.

checkout별 게시 generation, 콘텐츠 캐시 키/GC, 공유 위키의 완전한 의존성
판정은 별도 구현·회귀 검증이 필요하다. 현재 QMD 색인 및 recall 경로가 이
계약을 이미 만족한다고 주장하지 않는다.

## 단계 A: 반환된 결과 단위 최신성 검사 (구현됨)

recall은 QMD가 **반환한 결과만** 주입 전에 다시 확인한다. 공유 저장소와 선택된
프로젝트 로컬 색인 두 라이브 경로 모두 `qmd_scope.scoped_hits`를 적용한다.

- 범위: 결과 경로를 현재 payload `cwd`에서 찾은 프로젝트의 허용 collection
  경로로 해석한다. `..`·절대 경로·역슬래시, 미등록 collection, collection 밖으로
  해석되는 symlink와 collection 내부의 symlink도 제외한다. 따라서 `cwd`가 다른 worktree로 바뀌면 그
  worktree의 파일만 읽고, 이전 checkout의 경로는 주입하지 않는다.
- 내용: 현재 파일의 SHA-256이 색인의 활성 문서 행 해시와 같을 때만 주입한다.
  수정(`modified`), 삭제(`missing`), 색인 행 없음(`not_indexed`), 2 MiB 초과
  (`oversize`), 읽기 오류(`read_error`), 색인을 읽을 수 없음(`index_unavailable`)은
  해당 결과만 제외한다. 바이트가 같은 브랜치 복귀·detached HEAD·미추적 파일은
  Git 정보 없이 내용으로 판정한다.
- 비용: 반환된 행(최대 256행, 파일당 2 MiB)만 읽는다. 프로젝트 전체 스캔, Git
  호출, dirty queue 등록, 색인 쓰기를 하지 않는다. 색인은 읽기 전용으로 연다.
- SessionStart와 무관: 스냅샷이 없거나 `sessionStartSync: false`여도 해시가
  일치하는 결과는 그대로 주입한다.
- 진단: `QMD_RECALL_LOG` 선택 줄에 `index_guard`(`shared`|`project_local`|
  `fixture`|`none`), `index_guard_checked`, `index_guard_dropped`,
  `index_guard_drop_reasons`를 남긴다. `none`은 검증할 색인이 없는 경로(테스트
  fixture나 전역 daemon 경로)이며 최신성을 뜻하지 않는다.

이 단계가 보장하지 않는 것:

- 새 파일이나 새 내용은 색인이 갱신되기 전까지 검색되지 않는다. 반환되지 않은
  파일을 찾아 주입하지 않는다.
- 이미 주입된 이전 컨텍스트를 회수하지 않는다.
- 해시 확인과 본문 읽기 사이, 또는 QMD 검색과 해시 확인 사이의 동시 편집에
  대한 원자성은 없다. 한 요청이 하나의 게시 generation을 쓴다는 보장도 없다.
- QMD가 경로를 정규화해 저장하면(대소문자 등) 실제 파일과 맞지 않아
  `missing`으로 제외될 수 있다(fail-closed).
- 전역 daemon 경로(선택된 색인 없음)는 검사하지 않는다. 운영 환경은 공유
  저장소 경로를 쓴다.

## 단계 B1: 현재 존재하는 파일의 등록 소유자 라우팅 (구현됨)

편집 이벤트의 파일이 현재 세션 프로젝트 collection 밖에 있어도, **이미
승인·등록된** 공유 store collection의 정규 파일이면 그 소유 프로젝트의 색인
dirty queue에 넣는다. 현재 세션 검색 scope는 여전히 실제 질의 cwd로 정한다.
대상 프로젝트의 최신 설정·승인 hash·indexed role·`postToolUse` 동의를
재검증한다. 미등록·OFF·모호한 중첩/중복 소유자·symlink는 보류한다. 새
worktree 승인이나 외부 경로 자동 등록은 하지 않는다.

이 단계는 색인 **요청 라우팅**이며 색인 갱신 완료나 새 내용의 검색 가능성은
증명하지 않는다.

## 단계 B2a: 사라진 경로의 등록 소유자 라우팅 (구현됨)

삭제된 파일이나 이동한 파일의 이전 경로는 현재 메타데이터가 없으므로 현재 등록
정보만으로 라우팅하지 않는다. 앞서 수락된 이벤트가 그 symlink 없는 경로를 같은
승인·색인·등록 collection의 정규 파일로 기록했고, 그 관찰이 같은 등록
revision(store generation, 승인 맵, registry 파일 객체)과 같은 승인 설정 digest에
묶여 있을 때만 라우팅한다. 관찰 원장은 증거일 뿐이며, 현재 등록과 소유 프로젝트
설정은 존재하는 파일과 같은 방식으로 다시 증명한다. 이동의 양쪽 경로는 각각
판정한다(`core/change_owner.py`).

worktree별 단일 게시 generation과 늦은 worker 차단은 QMD route에 구현돼 있지 않다.
