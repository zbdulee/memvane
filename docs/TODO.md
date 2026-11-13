# 열린 항목

현재 코드에 남아 있는 미해결 항목만 둔다. 끝난 항목은 지운다.

## 코드

- **`memvane/journal/initial_build.py`의 `retired` 단계 설명이 코드와 다르다.** docstring은 `retired`를 아무 작업도 하지 않은(`registered`) 빌드의 abandon으로만 설명하고 `abandon_unused_initial_build`만 가리킨다. `abandon_inactive_initial_build`는 일부 빌드된 미선택 후보도 retire한다. docstring을 두 경로에 맞춘다.
- **`memvane/host/host_response.py` `_render_context`의 raw 분기.** 위키 전용 색인에서 비위키 행은 계약 위반인데, else 분기가 이를 `Path(path).stem` 제목으로 렌더링해 위반을 숨긴다. 도달 가능성을 확인하고, 도달하면 `_fail('raw_row_in_selected_index')`로 바꾼다. 같은 파일의 header 문구 "verified source documents"를 "wiki cards"로 고친다.

## recall 품질

- **verify defer 루프의 종점.** `wiki_verify_worker`는 검수 직전 원문 freshness가 `FRESH`가 아니면 잡을 `deferred / source_changed_before_verify`로 남기고 큐에 그대로 둔다. 재시도 카운터도 종점도 없어 `recallVerifiedOnly` 아래에서 그 카드는 계속 주입되지 않는다.
  - `content_hash_mismatch`는 검수가 아니라 재컴파일이 필요하다. N회 defer 뒤 compile source 큐로 넘기는 방안이 있다. 단 같은 원문의 compile이 `extractor_failed/source_unreadable`이나 `skipped/verify_inconclusive_suppressed`로 막혀 있으면 루프가 끝나지 않고, `wiki_compile_worker.recover_pending_refreshes`가 매 run 재큐잉하므로 중복 enqueue(이중 과금)가 될 수 있다. `source_unreadable`의 원인부터 진단한다.
  - "N회 defer"를 표현할 상태가 없다. `requeue_lines`는 raw 행을 그대로 다시 쓰고 `_requeue_count`는 compile worker의 orphan claim 경로에만 있다.
  - `missing`은 신선해질 수 없어 영구 대기다. 이 분기가 `record_source_missing`보다 앞에 있다. `wiki_freshness.check_card`는 첫 비-fresh 원문에서 반환하므로 일부 원문만 사라진 경우일 수 있고, 그때 `record_source_missing`은 `all_sources_missing` 가드로 아무것도 쓰지 않는다. 전멸·일부·그 외 세 갈래로 나눈다.
  - 적용 뒤 `verify-queue.jsonl`이 줄고 `verify-log.jsonl`에 `deferred`가 아닌 결과가 나오는지 확인한다.
- **PostToolUse recall 주입을 유지할지.** 편집 hook 주입은 방금 편집한 파일 문맥이라 모델이 이미 쥐고 있고, 연속 편집마다 반복된다. 선택지는 유지, 링크·제목만 남기는 게이트, 폐지다. 판단 전에 집계 단위(프로젝트 전체·단일 세션, 기간)를 통일해 건수와 분량을 다시 잰다.
- **subagent·하네스 프롬프트 주입.** subagent(sidechain) 세션에는 recall 주입이 닿지 않는다. 닿게 할지 정한다. `<task-notification>` 같은 하네스 생성 프롬프트에도 UserPromptSubmit 주입이 발화한다. 거를 기준(태그, 발신 주체)을 정한다.

주입 품질 로드맵이 범위 밖으로 둔 기능을 다시 다룰지도 정한다. 대상은 카드 유효 구간(bitemporal), 카드 간 모순 탐지 lint, 카드 간 관계(그래프), 근거 카드 변경의 stale 전파, 주입이 답변을 개선했는지 재는 평가 지표다.

## git-blob 모델 rollout

[git-blob 설계](plans/2026-11-01-memvane-db-git-blob-model.md) §12의 PR8(DB provenance compile/lifecycle), PR9(frontmatter-free 카드 writer), PR10(retention/GC·운영 확대)이 남았다. 사용자 결정은 같은 문서 §14에 있다.

## 프로젝트 밖 원문 편집

- 대기 큐(64경로)를 넘는 개별 compile 알림은 큐에 추가되지 않고 대상 프로젝트의 full-scan wake만 offer된다. 초과분을 보존할지 정한다. 전체 원문 compile 권한으로 넓히지 않는다. 현재 계약은 [프로젝트 밖 원문 편집 라우팅](out-of-project-edit-routing.md)에 있다.

## 세션 위치와 무관한 recall: hook이 도는 세션이면 recall도 돈다

owner 목표: MemVane은 사용자 전역 플러그인이다. hook이 도는 세션이라면 세션을 어디서 열었든, 작업 대상 프로젝트의 카드가 만들어지고 그 프로젝트의 recall도 주입돼야 한다. 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다.

- **현재 동작.**
  - route 선택은 hook payload의 `cwd` 기준이다(`host_response.select_route(cwd, …)`, `config._project_search_dirs`). cwd에서 HOME까지 위로만 올라가며 가장 가까운 `.memvane/settings.json`을 찾고, 하위 디렉터리는 보지 않는다.
  - 편집 hook(`_edit_targets`)은 세션 cwd가 선택된 프로젝트 안에 있어야 동작한다.
  - 프로젝트 밖 원문 편집 라우팅([계약](out-of-project-edit-routing.md))은 선택된 A 세션에서 선택된 B 원문을 고칠 때 B 워커에 알림을 보낸다. 그래서 B 카드는 만들어진다. 하지만 recall 컨텍스트는 A 색인만 쓰므로 그 세션에서 B 카드는 보이지 않는다.
  - 미온보딩 A 세션은 라우팅에서 빠진다. 예: 여러 작품을 하위 폴더로 둔 monorepo 루트(`.memvane` 없음)에서 연 세션은 활성화된 하위 프로젝트 파일을 고쳐도 카드도 recall도 없다.
- **목표.**
  - 편집: 세션 cwd가 어디든(미온보딩 루트 포함) 편집한 원문이 속한 선택 프로젝트의 compile로 이어진다.
  - recall: 세션이 지금 다루는 선택 프로젝트(들)의 카드가 프롬프트에 주입된다. cwd가 그 프로젝트 밖이어도 그렇다.
- **방향 후보.**
  - 편집 라우팅 대상 확대: 편집 경로에서 가장 가까운 선택 프로젝트를 찾아 알린다. A가 없거나 미온보딩이어도 알린다. 지금 알림 계약의 증명·상한·제외 규칙(숨김 경로, wiki, `skipPaths`, symlink 탈출)은 그대로 쓴다.
  - 세션 작업 집합(working set): 세션 id별로 이번 세션에서 편집·조회한 원문이 속한 선택 프로젝트 목록을 짧은 TTL로 기록한다. recall은 cwd 프로젝트와 작업 집합의 프로젝트를 후보로 쓴다.
  - 하위 프로젝트 탐색: cwd에 설정이 없으면 cwd 아래의 선택 프로젝트를 상한(개수·깊이·시간) 안에서 찾는다. 작업 집합이 비어 있으면 주입하지 않거나 프롬프트에 나온 경로·작품명으로 고른다.
  - 다중 프로젝트 병합: 후보 프로젝트마다 자기 route 증명으로 질의한다. 결과는 프로젝트 이름을 붙여 나누고, 프로젝트별 예산 안에서 병합한다.
- **먼저 정할 계약.**
  - 증명과 신뢰: 프로젝트마다 기존 selected route 증명을 따로 통과해야 한다. 한 프로젝트 증명 실패가 다른 프로젝트 주입을 막지 않는다.
  - hook 예산: 여러 색인을 질의해도 UserPromptSubmit 2초 예산 안에 끝난다. 벡터 질의는 resident service 한 번 호출로 묶을지 정한다.
  - 격리: B 카드를 A 세션에 주입하는 것을 기본 허용할지 정한다. owner 목표는 허용이다. 프로젝트별 opt-out 설정 키가 필요한지도 정한다.
  - 작업 집합 저장 위치·수명: 세션 id, TTL, 호스트별(Claude·Codex·Hermes) 동일 동작, subagent 세션 처리(「recall 품질」의 subagent 주입 항목과 함께 정한다).
  - 선택 기준: cwd 프로젝트, 작업 집합, 프롬프트 언급 경로의 우선순위와 동률 처리.
  - 연결 worktree·preview·blocked 대상의 처리. 지금 라우팅 제외 규칙과 맞춘다.
  - 미온보딩 루트의 SessionStart 안내: 하위에 선택 프로젝트가 있으면 안내 문구를 바꿀지 정한다.
- **검증 기준.**
  - 미온보딩 monorepo 루트 세션에서 하위 선택 프로젝트 원문을 고치면 그 프로젝트 카드가 생긴다. 다음 프롬프트에 그 카드가 주입된다.
  - 선택된 A 세션에서 B 원문을 고치면 B 카드가 생기고, 이후 B 관련 프롬프트에 B 카드가 주입된다.
  - 다중 프로젝트 질의가 hook 예산 안에 끝나고, 한 프로젝트 실패가 다른 프로젝트 주입을 막지 않는다.
- 새 hook action·wake kind·lock이 필요한지는 설계 단계에서 판단한다.

## 선택 프로젝트 레지스트리: 등록 경로를 DB로 관리한다

「세션 위치와 무관한 recall」과 「연결 git worktree의 카드 수명 주기」가 이 레지스트리에 기댄다. 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다.

- **현재 동작.**
  - 세션 프로젝트는 hook payload `cwd`에서 위로 올라가며 `.memvane/settings.json`을 찾아 정한다(`config._project_search_dirs`).
  - 선택 프로젝트 레지스트리 v1(`memvane/registry/memvane_registry.py`, `docs/project-registry.md`)이 선택된 main checkout과 연결 worktree 경로를 조회 색인으로 가진다. 활성화 성공 뒤 등록, restore·retire·abandon 성공 뒤 해제 반영, 선택 route SessionStart·대시보드 조회의 유계 reconcile까지다.
  - 프로젝트 밖 원문 편집 라우팅(`external_edits`)은 backfill이 완료된 레지스트리의 `active` main 행만 후보로 증명하고 state base를 훑지 않는다. 레지스트리가 없거나 읽을 수 없으면 라우팅하지 않는다.
  - 세션 프로젝트 해석은 레지스트리를 쓰지 않는다. 활성 선택 해제(첫 선택 쓰기 뒤 uninstall), 프로젝트 root 이전, schema3 worktree view ID·준비 상태 기록은 남아 있다.
  - resident service는 레지스트리를 reconcile하지 않는다. resident 연결은 resident 변경 승인 시 `memvane_registry.reconcile()` 1줄 호출로 붙인다.
  - 외부 편집의 완료 조회는 편집 경로와 owner root 사이에 다른 프로젝트 폴더가 있으면 라우팅하지 않는다. 열거 경로(backfill 미완료)는 레지스트리 도입 전과 같아서 등록되지 않은 중첩 프로젝트 아래 편집도 바깥 owner로 전달될 수 있다. 열거 경로에도 같은 검사를 둘지는 별도로 정한다.
  - 초기 빌드 pointer(`initial-builds/<projectRoot 해시>.json`), 설치별 state 디렉터리, 연결 worktree view는 레지스트리와 별개로 남는다.
  - uninstall 뒤 정리 단계가 없다. retire·abandon·restore는 저장소와 journal을 남기고, 거절된 prepare도 state 디렉터리를 남긴다(2026-11-10 관측: 잠금 파일만 있는 `efb6dee9…`). 이런 디렉터리도 계속 훑기 대상이다.
- **목표.**
  - 사용자 단위 레지스트리 DB(state base 아래 SQLite)가 등록 경로를 가진다. 항목은 프로젝트 root, worktree root, install id, state root, 종류(main·worktree), 상태다.
  - hook은 cwd·편집 경로·세션 작업 집합을 레지스트리의 최장 접두사 조회로 프로젝트에 대응시킨다. 파일시스템을 훑지 않는다.
  - install·uninstall이 등록 lifecycle을 결정한다.
- **lifecycle.**
  - 등록: 온보딩 activate가 성공하면 프로젝트 root를 등록한다. 연결 worktree view가 처음 게시되면 그 worktree root를 같은 install의 하위 항목으로 등록한다.
  - 해제: uninstall(선택 해제)이 끝나면 그 install의 프로젝트·worktree 항목을 지운다. worktree는 `git worktree remove`·prune이 관측되면 해제한다.
  - 프로젝트 폴더 삭제: 등록된 프로젝트·worktree root가 사라지면 그 항목을 해제 대상으로 표시한다. uninstall과 같은 정리 범위를 적용하되, state 디렉터리·저장소·학습 상태 삭제는 uninstall의 승인 규칙을 따른다. 폴더가 다시 나타나면(같은 경로 재생성, 볼륨 재연결) 자동으로 다시 선택하지 않는다. 같은 install로 되살릴지 새 온보딩으로 볼지 정한다.
  - reconcile 시점: 상주 서비스(`resident_service`)가 시작할 때 레지스트리 항목을 실제 폴더와 맞춘다. root 존재 여부, `.memvane` 존재 여부, inode 변경, journal 선택 상태, worktree 등록 상태(`git worktree list`)를 본다. 서비스가 오래 떠 있으면 주기적으로도 맞춘다. 맞추는 동안 공유 lock을 쥐지 않고(`context/pr10-decision-draft.md` §6 원칙), 항목마다 짧게 끝낸다.
  - 외장·네트워크 볼륨처럼 잠시 사라지는 경로는 즉시 해제하지 않는다. 유예 기간(또는 연속 관측 횟수)을 둔다.
- **먼저 정할 계약.**
  - 권한: 레지스트리는 조회 색인일 뿐 권한을 주지 않는다. 조회한 뒤 기존 selected route 증명(journal·route·attestation)을 그대로 통과해야 한다. 항목과 증명이 어긋나면 그 항목을 건너뛰고 백그라운드 reconcile이 고친다.
  - uninstall의 정의: 선택 해제와 별도로 state 디렉터리·wiki 카드·학습 상태의 정리 범위와 owner 승인 단계를 정한다.
  - 기존 설치 이전: 첫 실행 때 state base를 한 번 훑어 레지스트리를 채운다(backfill).
  - 오래된 항목: 경로 소실, inode 변경, journal 미선택을 판정하는 규칙, 일시적 소실의 유예 기간, 서비스 시작·주기 reconcile의 예산을 정한다.
  - 연결 worktree: 등록 단위와 main 항목과의 관계를 정한다. 「연결 git worktree의 카드 수명 주기」의 계약과 맞춘다.
- **검증 기준.**
  - 프로젝트 해석과 프로젝트 밖 편집 라우팅이 state base 훑기 없이 레지스트리 조회로 끝난다.
  - uninstall 뒤 그 경로의 세션은 미온보딩과 같이 동작하고, 레지스트리와 훑기 대상에서 빠진다.
  - worktree 제거 뒤 그 worktree 항목이 해제된다.
  - 프로젝트 폴더를 지우고 서비스를 다시 시작하면 그 항목이 해제 대상이 되고, 훑기·라우팅·recall 대상에서 빠진다. 잠시 분리된 볼륨은 유예 기간 동안 해제되지 않는다.
  - 레지스트리가 손상되거나 없어도 기존 증명 경로로 안전하게 동작하거나 거절한다.
- 새 hook action·wake kind·lock이 필요한지는 설계 단계에서 판단한다.

## 세대 저장 구조: 전체 사본에서 행 단위 버전으로

### S v1 이후: 공유 content GC

S v1은 view 행·FTS·stage/delta·세대만 GC한다. 여러 view가 공유하는 blob 본문·admission·attestation·source fingerprint·chunk/embedding set·recipe/profile은 보존하므로 현재+직전 세대 밖의 공유 내용도 남는다. PR10 v1 실사용 확인 뒤 아래 조건을 다시 검토한다. 인계 보고서 `/Users/dulee/work/memvane-handoff/context/claude-handoff-s-blob-gc-locks-20261112.md`의 분석은 현재 코드의 정적 추론이며 경합 재현이나 S 구현 검증 결과가 아니다.

활성화 후보는 다음 R1~R6을 모두 검증한다.

- **R1:** 선택 sync의 issued owner·writer scope 안에서 모든 게시·fill이 정상 반환된 뒤 같은 프로세스·스레드에서만 실행하고, 이후 기존 메모리 blob ID·fill target을 재사용하지 않는다. 실패·defer pass에서는 실행하지 않는다.
- **R2:** journal이 선택한 S 형식 schema3 store만 대상으로 하고 inode·incarnation·writer proof를 대조한다. 선택 해제 store와 연결 worktree route는 제외한다.
- **R3:** 각 유계 `BEGIN IMMEDIATE`에서 모든 참조를 다시 확인하고 vector→embedding set→chunk→chunk set→attestation source→attestation→admission→blob 순으로 삭제한다. FK 위반·busy는 전체 rollback 후 defer한다.
- **R4:** set 단위 FK 부재만 보지 않는다. 모든 view의 head·직전, stage, delta, 진행/완료 fill 대상, 외부 record, trust chain과 `coverage=complete`의 암묵 참조에 도달하는 blob을 보호한다. 살아 있는 blob의 모든 recipe/profile set을 보존한다.
- **R5:** orphan prepared/stage만 head/epoch를 transaction에서 재확인한 뒤 유계 batch로 정리하고 crash 뒤 재개를 검증한다.

실제 활성화 전 합성 RED/GREEN: (1) chunk set commit과 embedding callback 사이의 GC, (2) lexical head의 complete/partial fill set과 `coverage=complete`의 묵시 참조, (3) 진행 중 prepare 및 fill 핸들. head 관측과 게시 순서에 따른 결과를 각각 기록하고, 예상 오류·고착은 재현 전 사실로 취급하지 않는다. 현재+직전 view 행 GC와 그 자체의 RED는 S v1 범위다.

## 연결 git worktree의 카드 수명 주기

PR10까지는 하지 않는다. 그 뒤 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다.

- **현재 동작.** 연결 worktree에서는 recall만 동작한다. compile·수명 주기(퇴역·재생성)·dedup scan은 main checkout에서만 돈다(`memvane/wiki/lifecycle/wiki_lifecycle.py`의 `facts["linked"]` skip). schema3 linked route는 sync wake를 남기지만 compile pass를 실행하는 경로는 없다. 그래서 worktree에서 고친 원문은 브랜치가 main에 merge되고 main checkout에서 수명 주기가 돌아야 카드에 반영된다.
- **목표.** worktree에서 작업하는 동안 그 브랜치 기준으로 카드 compile과 수명 주기가 돌게 한다.
- **먼저 정할 계약.**
  - route·binding 증명: worktree writer가 어떤 증명으로 쓰기 권한을 얻는가. main install journal과 자기 view의 관계.
  - 카드 위치와 정본: worktree의 `.memvane/wiki`에 쓰는가 main wiki에 쓰는가. 브랜치 카드와 main 카드가 갈릴 때 어느 쪽이 정본인가.
  - 큐·상태 위치: compile·verify 큐와 claim, lifecycle·dedup scan 상태를 checkout별로 둘지 설치별로 공유할지. 공유하면 checkout 간 경합, 분리하면 같은 원문의 이중 유료 호출이 생긴다.
  - writer 상호 배제: main writer와 worktree writer가 설치 단위 lock을 어떻게 나눠 잡는가.
  - 브랜치 안전: 브랜치 전환·rebase·merge·worktree 삭제 때 카드 퇴역이 왕복하지 않게 대량 삭제 가드와 `git_branch_safety` HOLD 판정을 worktree 기준으로 다시 정한다.
  - 유료 호출 상한: worktree 수만큼 extractor·verify·dedup judge 호출이 늘지 않게 한다.
  - 정리: 사라진 worktree의 큐·상태·카드를 회수하는 방법.
- 새 hook action·wake kind·lock이 필요한지는 설계 단계에서 판단한다.

## 릴리스 QA

- **사용자가 유예한 assertion 5개는 PASS가 아니다**: `RQA-GIT-01`, `RQA-GIT-03`, `RQA-WIKI-01`의 유료 중복 호출 부분, `RQA-WIKI-03`, `RQA-RES-02`. 정식 릴리스 전에 [QA 계약](release-agent-qa.md)대로 실행한다. `RQA-WIKI-01`의 나머지 assertion은 유예 대상이 아니다.
- 사용자 유예를 기록할 상태값이 없다. [QA 계약](release-agent-qa.md) §2와 `docs/templates/release-agent-qa-run.md`에 `DEFERRED BY USER`를 정의할지 정한다.

## 학습 runtime

- 설치 후보에서 학습 중지·재개(stop pipe, `training_cursor.py`의 head slot·그룹 계획·RNG receipt)의 end-to-end 증거가 없다. 실행 중인 학습 자식과 index/update 작업이 OS 자원을 두고 겹칠 때의 부하도 측정하지 않았다.
- 첫 한정 학습 시험을 제안하기 전에 고정할 것: 설치 후보의 adapter·체크포인트·모델 파일 해시, 승인된 합성 입력, 동의·v2 grant와 만료, base artifact와 후보 출력의 소유 경로, OS별 device·메모리 프로필. 시험은 명시적 opt-in으로 `--kind model-test` 개발 가드와 제품 runtime admission을 함께 적용한 짧은 합성 한 그룹 trial이다. 프로세스 전체 CPU/RSS peak, MPS 사용량, swap·pressure·load, 소유 child 정리, stop 지연, 재시작 cursor, 최종 receipt를 관측한다. 가드의 5초 샘플링은 peak를 놓칠 수 있으므로 자식 내 계측 없이 자원 상한 검증으로 승격하지 않는다.
- 정식 릴리스 전 설치 후보에서 `RQA-RES-02`를 통과해야 한다(아래 릴리스 QA).

## 설치

- **`npx skills add` 진입점.** 공통 installer를 재사용하는 가벼운 `npx skills add` 설치 진입점을 검토한다. 구현과 배포는 보류 상태다. [skills CLI](https://skills.sh/docs/cli)([source](https://github.com/vercel-labs/skills))는 skill 디렉터리와 링크만 관리하므로 runtime·모델·DB에는 외부 영구 경로와 installer 소유의 update/remove 수명주기가 필요하다. skill 복사만으로는 Codex 번들 hook 등록이나 runtime postinstall·migration·uninstall이 생기지 않는다. Claude/Codex adapter와 공식 hook trust는 따로 검토한다: [Claude plugin loading](https://code.claude.com/docs/en/plugins/loading), [Claude skills](https://code.claude.com/docs/en/skills), [Codex hooks](https://developers.openai.com/codex/hooks).
