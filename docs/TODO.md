# 열린 항목

현재 코드에 남아 있는 미해결 항목만 둔다. 끝난 항목은 지운다.

## 코드

- **`core/laya_initial_build.py`의 `retired` 단계 설명이 코드와 다르다.** docstring은 `retired`를 아무 작업도 하지 않은(`registered`) 빌드의 abandon으로만 설명하고 `abandon_unused_initial_build`만 가리킨다. `abandon_inactive_initial_build`는 일부 빌드된 미선택 후보도 retire한다. docstring을 두 경로에 맞춘다.
- **`core/laya_host_response.py` `_render_context`의 raw 분기.** 위키 전용 색인에서 비위키 행은 계약 위반인데, else 분기가 이를 `Path(path).stem` 제목으로 렌더링해 위반을 숨긴다. 도달 가능성을 확인하고, 도달하면 `_fail('raw_row_in_selected_index')`로 바꾼다. 같은 파일의 header 문구 "verified source documents"를 "wiki cards"로 고친다.

## recall 품질

[recall 효용 회복 계획](plans/2026-08-19-recall-utility-recovery-plan.md)의 열린 항목 세 개다.

- verify defer 루프의 종점.
- PostToolUse recall 경로를 유지할지.
- subagent(sidechain)·하네스 기계 프롬프트에도 주입할지.

주입 품질 로드맵이 범위 밖으로 둔 기능을 다시 다룰지도 정한다. 대상은 카드 유효 구간(bitemporal), 카드 간 모순 탐지 lint, 카드 간 관계(그래프), 근거 카드 변경의 stale 전파, 주입이 답변을 개선했는지 재는 평가 지표다.

## 연결 git worktree의 카드 수명 주기

PR10까지는 하지 않는다. 그 뒤 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다.

- **현재 동작.** 연결 worktree에서는 recall만 동작한다. compile·수명 주기(퇴역·재생성)·dedup scan은 main checkout에서만 돈다(`core/wiki_lifecycle.py`의 `facts["linked"]` skip). schema3 linked route는 sync wake를 남기지만 compile pass를 실행하는 경로는 없다. 그래서 worktree에서 고친 원문은 브랜치가 main에 merge되고 main checkout에서 수명 주기가 돌아야 카드에 반영된다.
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
- `docs/templates/release-agent-qa-run.md` §5에 `LI-*` 7행이 없고, 상태값에 `DEFERRED BY USER`가 없다.

## 설치

- **`npx skills add` 진입점.** 공통 installer를 재사용하는 가벼운 `npx skills add` 설치 진입점을 검토한다. 구현과 배포는 보류 상태다. [skills CLI](https://skills.sh/docs/cli)([source](https://github.com/vercel-labs/skills))는 skill 디렉터리와 링크만 관리하므로 runtime·모델·DB에는 외부 영구 경로와 installer 소유의 update/remove 수명주기가 필요하다. skill 복사만으로는 Codex 번들 hook 등록이나 runtime postinstall·migration·uninstall이 생기지 않는다. Claude/Codex adapter와 공식 hook trust는 따로 검토한다: [Claude plugin loading](https://code.claude.com/docs/en/plugins/loading), [Claude skills](https://code.claude.com/docs/en/skills), [Codex hooks](https://developers.openai.com/codex/hooks).
