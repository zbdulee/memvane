# Git checkout·worktree 검색 동기화 정책

이 정책은 선택된 Laya route에만 적용된다. route가 없는 프로젝트는 스캔·색인·
위키 source queue 등록·recall을 하지 않는다.

선택된 Laya route(binding store가 있는 v2)의 recall은 hook 안에서 모델 작업 없이
완료된 index generation 하나를 pin하고 위키 카드만 검색한다. 색인 DB에 게시된
위키 본문과 해시·세대·범위·명시 제외를 확인하며, 현재 원문 파일이나 pending
refresh 원장을 다시 읽지 않는다. 원문 변경 뒤 위키 생성·색인 게시가 끝나기
전에는 마지막 완료본이 검색된다. binding store가 없는 구형 선택 경로는 질의 시
live scan을 사용한다. 흐름은 [위키 색인과 검색 주입](architecture/flows/03-wiki-index-search.md)에
정리돼 있다.

## 현재 반영 범위

플러그인은 Git 명령을 실행해 원격 변경을 받아오지 않는다. 사용자가 `git pull`,
checkout, 파일 편집을 마친 뒤 다음 hook 이벤트가 `laya-sync` wake를 기록한다.

- SessionStart(`update-queued`)
- PostToolUse 편집(`index`): 프로젝트 안 경로만 대상이다. 프로젝트 밖 경로는
  어디로도 라우팅하지 않으며, 그 프로젝트 자신의 full-scan sync가 맞춘다.
- Claude의 성공한 Bash(`file-change-event`)

hook은 wake만 기록한다. off-hook owner(`core/laya_product_sync_worker.py`)가
자원 승인을 받은 뒤 bound 원문 범위 전체를 스캔하고, 생성·수정·삭제·이름 변경을
반영한 full-membership generation을 게시한다. 실패하거나 유예된 시도는 wake를
남기고 dispatcher가 다시 시도한다. 따라서 hook 호출 자체가 즉시 검색 가능성을
보장하지는 않는다.

sync가 성공하면(`indexed` 또는 `unchanged`) 같은 worker가 bounded raw 범위를
스캔해 path/hash 쌍을 비공개 cursor와 비교한다(`core/laya_wiki_discovery.py`).
raw 본문은 Laya membership·FTS·vector에 들어가지 않는다. 바뀐 revision은
`wiki_compile_enqueue.enqueue_changed_sources`의 trigger 정책으로 넘기고, 삭제된
경로는 버리며, 위키만 바뀐 변경은 무시한다. cursor나 grant 상태가 손상되거나
알 수 없으면 아무것도 enqueue하지 않고 새 기준점으로 초기화하지도 않는다.

위키 컴파일은 별도 동의다. 위키 compile grant는 SessionStart(`session_start`)와
성공한 file-change 이벤트(`sync`)에서만 기록되며, `index` 이벤트나 wiki 카드
sync는 grant를 남기지 않는다. grant가 없으면 revision은 pending으로 남는다.
SessionStart grant는 `compile.triggers`의 `session_start_source`만 인정하고,
sync grant는 `post_sync_source` 또는 `post_tool_source`를 인정한다. minimal
settings의 `compile.triggers`는 `post_tool_source`·`post_sync_source`·`manual`이다.

위키 source queue를 채우는 경로는 각자 상태를 사용한다. 해시를 계산하는
경로(raw discovery)의 행은 원문 revision(`contentRevision`)을 가진다. enqueue는
`(collection, path, revision, trigger)`가 같은 행이 아직 live queue에 있으면 새
행을 넣지 않는다(`wiki_source_intent.revision_key`). 이 키는 trigger를 포함하고
이미 claim·소진된 행은 보지 못하므로 경로 간·회차 간 중복 등록은 막지 않는다.
PostToolUse(`compile`) 행에는 revision이 없다. compile worker는 신뢰 카드가 같은
revision을 이미 증명하면 trigger와 무관하게 extractor를 실행하지 않는다
(`already_compiled_revision`).

## linked git worktree

route record와 selection marker는 추적되지 않는 런타임 파일이라 linked worktree에는
자체 route가 없다. 다음 조건이 모두 맞으면 worktree cwd는 `worktree` route로
분류된다(`laya_index_host_route._worktree_route`).

- git이 linked worktree로 보고하고, 공통 git dir이 main checkout의 git dir이다.
- cwd와 worktree 루트 사이에 route record나 selection marker가 없다.
- worktree 안 프로젝트가 main 프로젝트와 같은 상대 경로에 있다.
- main 프로젝트가 정확히 `candidate`로 분류된다.
- 양쪽 settings가 같은 이름·collection·경로·role·위키 경로를 유도한다.

`worktree` route의 검색은 main checkout이 마지막으로 게시한 위키 색인을 읽기
전용으로 쓴다. sync, wiki compile, wiki lifecycle, 학습은 main checkout에서만
실행되므로, worktree에서만 바뀐 내용은 이 색인에 반영되지 않는다. 조건이 맞지
않으면 worktree는 자기 cwd 기준 분류를 그대로 따른다.

## 검색 일관성 목표와 후속 설계

다음 항목은 **정책 목표**이며, 구현된 기능이라고 해석하면 안 된다.

- 검색 가시성은 실제 요청을 수행하는 checkout/worktree별 현재 파일 집합을
  따른다. dirty 작업 파일도 허용된 collection 범위라면 그 worktree의 내용으로
  취급한다. 자동 `git pull`은 하지 않는다.
- 콘텐츠 캐시 재사용 키에는 바이트 해시뿐 아니라 파서, 청킹 및 임베딩 모델의
  버전·설정을 포함한다. rename은 새 경로로 교체하되 같은 콘텐츠의 유효한
  파생물은 재사용할 수 있다. 삭제는 해당 view에서만 제외하며 공유 콘텐츠
  캐시의 GC와 worktree 삭제를 별도 절차로 둔다.
- 신규·수정 파일은 현재 raw 파일 경로를 먼저 사용할 수 있어야 한다. 임베딩과
  위키 생성은 후속 단계다. **현재 경로는 raw 즉시 검색 fallback이나 새 파일의
  즉시 주입을 구현하지 않는다.** 검색은 게시된 위키 generation만 읽는다.
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
판정은 별도 구현·회귀 검증이 필요하다. 현재 Laya 색인 및 recall 경로가 이
계약을 이미 만족한다고 주장하지 않는다.
