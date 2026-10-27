# 열린 항목

현재 코드에 남아 있는 미해결 항목만 둔다. 끝난 항목은 지운다.

## 코드

- **`core/sync.py` pending marker 복구 경로 없음.** `run()`은 collection root 해석과 dirty-index 작업보다 먼저 pending marker를 확인하고, marker가 있으면 `sync_recovery_required`(exit 4)를 반환한다. `baseline_only`와 일반 QMD 인덱싱도 함께 멈춘다. 공식 복구 명령은 없다. `write_pending_marker`와 `clear_pending_marker` 사이의 중단이나 `persist` 성공 뒤 `clear_pending_marker` 실패만으로 프로젝트의 sync가 막힌다. marker의 적용 범위를 좁히고 복구 절차를 만든다. marker를 손으로 지워 우회하지 않는다.
- **`core/laya_initial_build.py`의 `retired` 단계 설명이 코드와 다르다.** docstring은 `retired`를 아무 작업도 하지 않은(`registered`) 빌드의 abandon으로만 설명하고 `abandon_unused_initial_build`만 가리킨다. `abandon_inactive_initial_build`는 일부 빌드된 미선택 후보도 retire한다. docstring을 두 경로에 맞춘다.
- **`core/laya_host_response.py` `_render_context`의 raw 분기.** 위키 전용 색인에서 비위키 행은 계약 위반인데, else 분기가 이를 `Path(path).stem` 제목으로 렌더링해 위반을 숨긴다. 도달 가능성을 확인하고, 도달하면 `_fail('raw_row_in_selected_index')`로 바꾼다. 같은 파일의 header 문구 "verified source documents"를 "wiki cards"로 고친다.
- **수동 skill과 hook의 route 차이.** `skills/query`·`sync`·`update`·`wiki-compile` wrapper는 QMD 경로를 실행하고 hook의 Laya route 분류(`core/laya_index_host_route.py classify-hook`)를 하지 않는다. Laya가 선택된 프로젝트에서 hook은 QMD fallback을 거부하지만 skill은 QMD 경로를 탄다. skill을 route 분류에 맞출지 정한다.

## recall 품질

[recall 효용 회복 계획](plans/2026-08-19-recall-utility-recovery-plan.md)의 열린 항목 세 개다.

- verify defer 루프의 종점.
- PostToolUse recall 경로를 유지할지.
- subagent(sidechain)·하네스 기계 프롬프트에도 주입할지.

주입 품질 로드맵이 범위 밖으로 둔 기능을 다시 다룰지도 정한다. 대상은 카드 유효 구간(bitemporal), 카드 간 모순 탐지 lint, 카드 간 관계(그래프), 근거 카드 변경의 stale 전파, 주입이 답변을 개선했는지 재는 평가 지표다.

## 릴리스 QA

- **사용자가 유예한 assertion 5개는 PASS가 아니다**: `RQA-GIT-01`, `RQA-GIT-03`, `RQA-WIKI-01`의 유료 중복 호출 부분, `RQA-WIKI-03`, `RQA-RES-02`. 정식 릴리스 전에 [QA 계약](release-agent-qa.md)대로 실행한다. `RQA-WIKI-01`의 나머지 assertion은 유예 대상이 아니다.
- `docs/templates/release-agent-qa-run.md` §5에 `LI-*` 7행이 없고, 상태값에 `DEFERRED BY USER`가 없다.

## 설치

- **`npx skills add` 진입점.** 공통 installer를 재사용하는 가벼운 `npx skills add` 설치 진입점을 검토한다. 구현과 배포는 보류 상태다. [skills CLI](https://skills.sh/docs/cli)([source](https://github.com/vercel-labs/skills))는 skill 디렉터리와 링크만 관리하므로 runtime·모델·DB에는 외부 영구 경로와 installer 소유의 update/remove 수명주기가 필요하다. skill 복사만으로는 Codex 번들 hook 등록이나 runtime postinstall·migration·uninstall이 생기지 않는다. Claude/Codex adapter와 공식 hook trust는 따로 검토한다: [Claude plugin loading](https://code.claude.com/docs/en/plugins/loading), [Claude skills](https://code.claude.com/docs/en/skills), [Codex hooks](https://developers.openai.com/codex/hooks).
