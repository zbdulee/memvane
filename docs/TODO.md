# 열린 항목

현재 코드에 남아 있는 미해결 항목만 둔다. 끝난 항목은 지운다.

## 코드

- **진행 중 작업의 재부팅 경계 identity 복구(A9~A15).** 장치 번호를 포함한 중간 기록의 재개 증명과 회귀 검사를 마련한다.
  대상은 A9 shadow migration intent(`migration_intent_invalid`, sync의 후보 재시작 판정), A10 초기 v5 변환 intent(`initial_conversion_evidence_invalid`), A11 reader image staging note(`reader_image_changed`),
  A12 이동 seal·재료·layout(`reader_move_material_changed`, `reader_move_layout_changed`, `reader_move_source_changed`, `project_dir_move_store_changed`), A13 bridge challenge·receipt(`reader_hosts_pending`),
  A14 설치 proposal(`reviewed_origin_plan_changed`, scopeId seed 유지), A15 legacy probe owner(`legacy_probe_unowned_scratch`)다.
  진행 중 업그레이드의 provider identity가 SHA로만 봉인된 경우(`active_upgrade_new_provider_changed`)도 원본 hash를 유지하는 별도 복구 증명이 필요하다.
- **`core/laya_initial_build.py`의 `retired` 단계 설명이 코드와 다르다.** docstring은 `retired`를 아무 작업도 하지 않은(`registered`) 빌드의 abandon으로만 설명하고 `abandon_unused_initial_build`만 가리킨다. `abandon_inactive_initial_build`는 일부 빌드된 미선택 후보도 retire한다. docstring을 두 경로에 맞춘다.
- **`core/laya_host_response.py` `_render_context`의 raw 분기.** 위키 전용 색인에서 비위키 행은 계약 위반인데, else 분기가 이를 `Path(path).stem` 제목으로 렌더링해 위반을 숨긴다. 도달 가능성을 확인하고, 도달하면 `_fail('raw_row_in_selected_index')`로 바꾼다. 같은 파일의 header 문구 "verified source documents"를 "wiki cards"로 고친다.

## recall 품질

- **대형 설치의 recall provider 준비 및 거절 진단.** 선택된 v6 reader에서 `resident.adopt`가 `adopted`인 뒤에도 `selected_provider_preparing`으로 벡터 검색을 건너뛰는 recall과 `selected_proof_or_query_failed` 거절이 반복된다. 긴 프롬프트의 `laya_selector_invalid_pool`도 함께 관측된다. 서비스 listener 바인딩은 개별 임베딩 요청 성공을 증명하지 않으며, 거절 코드는 원래 예외를 구분하지 않는다. provider 요청 실패·timeout, selector 입력 조건과 거절 원인을 각각 분리해 진단하고, 정상 벡터 recall과 거절 해소를 실제 설치에서 검증한다.
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

## 세대 저장 구조: 전체 사본에서 행 단위 버전으로

PR10 GC 뒤에 진행한다. 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다. 저장 스키마·recall 읽기 경로·봉인 증명이 함께 바뀐다.

- **현재 동작.**
  - 게시(publish)마다 새 세대를 만든다. v6 색인은 세대마다 `gen_fts_N` 테이블 세트, schema3 view store는 `prepare_generation`(`core/laya_view_store.py`)의 새 FTS 가상 테이블에 eligible 카드 본문 **전체**를 다시 넣는다. 카드 한 장만 바뀐 pass도 corpus 전체를 복사한다.
  - 원문 지문 행도 원문이 바뀐 pass마다 모든 raw 원문에 한 행씩 새로 쌓인다(`_schema3_put_sources`). 같은 scan id를 요구하는 세대 검증 때문에 재사용하지 않는다.
- **실측(2026-11-10, service-engineering v6 `laya-build.sqlite`, 읽기 전용).**
  - 세대 356개, 모두 같은 scope, 모두 `published`. digest가 같은 중복은 2쌍이다.
  - 3,105,441페이지 × 4KB = 12.7GB, freelist 0. VACUUM만으로는 회수되는 공간이 없다.
  - 세대당 약 36MB(카드 약 4천 장 × 약 9KB). schema3 기준 합성 실측은 카드 1장당 약 8.3KB/세대(FTS 6.8KB + 세대 행 1.5KB, 카드 바이트의 약 2.8배)다.
  - 세대 행에 생성 시각이 없어 하루 생성 수는 직접 알 수 없다. 356개 ÷ 약 2주 = 평균 하루 약 25개다. 이벤트 로그의 `wake.sync indexed`는 11/4 15회, 11/5 4회, 11/8 3회, 11/10 1회다.
- **영향.**
  - 저장 증가량이 바뀐 카드 수가 아니라 게시 횟수 × corpus 크기에 비례한다.
  - PR10 GC(Q3: 현재 head + 직전 세대 보존)를 적용해도 보존분은 corpus의 2배이고, 다음 GC 전까지 게시마다 corpus 크기만큼 다시 쌓인다.
  - 큰 store는 sync·shadow migration·업그레이드 백업·해시 검증 시간을 함께 늘린다. SE에서 변경 없는 sync는 약 4분, 업그레이드 백업·검증은 20분 이상 걸렸다.
  - GC가 mark-copy라 실행할 때 남길 데이터 크기만큼 여유 디스크가 필요하다.
- **목표.** 증가량과 보존 비용이 바뀐 카드 수에 비례하고, 게시·롤백·GC가 corpus 복사 없이 끝난다.
- **방향 초안.**
  - 읽기·쓰기 격리와 원자적 게시는 SQLite WAL 스냅숏 격리와 트랜잭션 커밋으로 충족한다. 세대마다 별도 사본을 두지 않는다.
  - 카드 행에 유효 구간(`gen_from`, `gen_to`)을 둔다. 바뀐 카드만 새 행으로 추가하고, 이전 행은 `gen_to`를 닫는다. 삭제된 카드는 행을 닫기만 한다.
  - 게시는 현재 세대 번호 한 칸을 바꾸는 것이고, 롤백은 그 번호를 직전 세대로 되돌리는 것이다.
  - FTS 색인은 하나로 두고, 검색 시 현재 세대에서 유효한 행만 필터링한다(external content FTS5 + rowid 필터, 또는 세대 범위 조건 join).
  - GC는 보존할 가장 오래된 세대보다 먼저 닫힌 행을 지우는 단일 삭제다.
  - 원문 지문은 같은 HEAD에서 바이트가 같은 원문이면 이전 scan 지문을 이어받는다(PR10 결정 초안의 R3 선택지 2).
- **먼저 정할 계약.**
  - 봉인 증명: 지금 세대 digest는 세대 전체에 대한 값이다. 카드별 해시의 머클 루트처럼 변경분만 다시 계산하는 구조로 바꿀지, 그때 attestation·journal·route가 무엇을 봉인하는지 정한다.
  - 삭제 방지 트리거와 정확 일치 DDL 검사: GC 삭제 경로만 허용하도록 재설계한다. 구 번들이 새 store를 열 때의 동작(거절 코드)도 정한다.
  - recall 성능: 세대 필터가 붙은 FTS5 질의의 지연을 카드 1천·4천·2만 장에서 실측한다. 기준은 hook 2초 예산 안의 기존 지연이다.
  - shadow migration·reader 전환과의 관계: 새 형식을 schema 버전 올림으로 넣을지, 별도 migration 단계로 넣을지 정한다. rollback 창(Q1 변경 결정: 전환 직후 닫고 이후 직전 세대로만 롤백)과 맞춘다.
  - 동시 읽기: 장기 reader(구 번들 호스트, 늦게 끝나는 recall)가 읽는 세대를 GC가 지우지 않게 하는 lease 규칙을 행 단위 기준으로 다시 정한다.
  - 벡터: member vector·chunk set이 카드 버전 행을 어떻게 참조하는지, 같은 본문 카드의 임베딩 재사용 규칙.
- **검증 기준.**
  - 카드 1장 변경 게시의 store 증가량이 그 카드 크기 수준이다.
  - 롤백·GC 시간이 corpus 크기와 무관하다.
  - SE 규모(카드 약 4천 장)에서 변경 없는 sync가 수 초 안에 끝난다.
  - 기존 recall 품질과 지연이 유지된다.
- 새 hook action·wake kind·lock이 필요한지는 설계 단계에서 판단한다.

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
- 사용자 유예를 기록할 상태값이 없다. [QA 계약](release-agent-qa.md) §2와 `docs/templates/release-agent-qa-run.md`에 `DEFERRED BY USER`를 정의할지 정한다.

## 학습 runtime

- 설치 후보에서 학습 중지·재개(stop pipe, `training_cursor.py`의 head slot·그룹 계획·RNG receipt)의 end-to-end 증거가 없다. 실행 중인 학습 자식과 index/update 작업이 OS 자원을 두고 겹칠 때의 부하도 측정하지 않았다.
- 첫 한정 학습 시험을 제안하기 전에 고정할 것: 설치 후보의 adapter·체크포인트·모델 파일 해시, 승인된 합성 입력, 동의·v2 grant와 만료, base artifact와 후보 출력의 소유 경로, OS별 device·메모리 프로필. 시험은 명시적 opt-in으로 `--kind model-test` 개발 가드와 제품 runtime admission을 함께 적용한 짧은 합성 한 그룹 trial이다. 프로세스 전체 CPU/RSS peak, MPS 사용량, swap·pressure·load, 소유 child 정리, stop 지연, 재시작 cursor, 최종 receipt를 관측한다. 가드의 5초 샘플링은 peak를 놓칠 수 있으므로 자식 내 계측 없이 자원 상한 검증으로 승격하지 않는다.
- 정식 릴리스 전 설치 후보에서 `RQA-RES-02`를 통과해야 한다(아래 릴리스 QA).

## 설치

- **`npx skills add` 진입점.** 공통 installer를 재사용하는 가벼운 `npx skills add` 설치 진입점을 검토한다. 구현과 배포는 보류 상태다. [skills CLI](https://skills.sh/docs/cli)([source](https://github.com/vercel-labs/skills))는 skill 디렉터리와 링크만 관리하므로 runtime·모델·DB에는 외부 영구 경로와 installer 소유의 update/remove 수명주기가 필요하다. skill 복사만으로는 Codex 번들 hook 등록이나 runtime postinstall·migration·uninstall이 생기지 않는다. Claude/Codex adapter와 공식 hook trust는 따로 검토한다: [Claude plugin loading](https://code.claude.com/docs/en/plugins/loading), [Claude skills](https://code.claude.com/docs/en/skills), [Codex hooks](https://developers.openai.com/codex/hooks).
