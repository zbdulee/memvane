# Git checkout·worktree 검색 동기화 정책

기능 소유권과 점진적 분리 계획은 [아키텍처 경계 설계](plans/2026-10-17-architecture-boundaries.md)에 기록한다.

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
첫 실행부터 `session_start_source`를 명시한 프로젝트는 기존 동작대로 최대
50개 원문을 source queue에 등록할 수 있다. enqueue 뒤 snapshot 저장에 실패하면
재시도에서 같은 원문이 다시 등록될 수 있으며, 유료 추출의 완전한 멱등성은
아직 증명되지 않았다.
위키 source queue를 채우는 경로는 각자 상태를 사용한다. 현재 경로 간 원문
revision 멱등성이 없으므로 둘 이상의 경로에 동의하면 같은 원문이 여러 번
등록돼 유료 추출이 중복될 수 있다.

- 수동 sync: 자체 snapshot과 `post_sync_source` 또는 `post_tool_source` 동의
- SessionStart sync: 별도 snapshot과 `session_start_source` 동의
- PostToolUse 편집 훅: 편집 이벤트와 `post_tool_source` 동의
- 지원 대상 generated story의 `file_change_queue`: 자체 snapshot을 쓰며 event
  회차는 `post_sync_source`·`post_tool_source`, reconcile 회차는
  `session_start_source` 동의를 따른다.

`post_tool_source` 하나로도 PostToolUse와 수동 sync가 함께 허용될 수 있다.
수동 sync와 SessionStart의 이중 등록은 격리 합성 회귀에서 확인했다. 다른
경로의 조합은 코드상 가능성이며, 이를 막는 원문 revision 멱등성은 후속 작업이다.

## 검색 일관성 목표와 후속 설계

다음 항목은 **정책 목표**이며, 이 변경에서 구현한 기능이라고 해석하면 안 된다.

- 검색 가시성은 실제 요청을 수행하는 checkout/worktree별 현재 파일 집합을
  따른다. dirty 작업 파일도 허용된 collection 범위라면 그 worktree의 내용으로
  취급한다. 자동 `git pull`은 하지 않는다.
- 콘텐츠 캐시 재사용 키에는 바이트 해시뿐 아니라 파서, 청킹 및 임베딩 모델의
  버전·설정을 포함한다. rename은 새 경로로 교체하되 같은 콘텐츠의 유효한
  파생물은 재사용할 수 있다. 삭제는 해당 view에서만 제외하며 공유 콘텐츠
  캐시의 GC와 worktree 삭제를 별도 절차로 둔다.
- 신규·수정 파일은 현재 raw 파일 경로를 먼저 사용할 수 있어야 한다. 임베딩과
  위키 생성은 후속 단계다. **현재 SessionStart 경로는 raw 즉시 검색 fallback이나
  새 파일의 즉시 주입을 구현하지 않는다.** 검색 직전 상태 확인도 별도 후속
  작업이다.
- 갱신 중에는 이전 브랜치 결과를 조용히 주입하지 않는다. 한 검색 요청은
  게시된 단일 generation만 사용해야 한다. 준비 중인 generation의 부분 결과를
  현재 view와 섞지 않는다.
- main 공유 위키는 현재 checkout과 모든 관련 원문 의존성이 일치할 때만
  재사용한다. 기존 파일의 해시가 같아도 파일 추가·삭제로 전체 의존 집합이
  바뀌면 무효화한다.
- 읽기 실패, 권한 거부, 일시적 I/O 오류는 파일 삭제의 증거가 아니다. 해당
  경로의 이전 view를 유지하고 오류를 드러낸 뒤 재시도한다. 읽을 수 있는
  collection 스캔에서 파일 부재가 확인되면 현재 view에서 제외한다.

검색 직전 검사, checkout별 게시 generation, 콘텐츠 캐시 키/GC, 공유 위키의
완전한 의존성 판정은 별도 구현·회귀 검증이 필요하다. 현재 QMD 색인 및
recall 경로가 이 계약을 이미 만족한다고 주장하지 않는다.
