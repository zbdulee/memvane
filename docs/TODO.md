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

## 세션 위치와 무관한 recall: hook이 도는 세션이면 recall도 돈다

owner 목표: MemVane은 사용자 전역 플러그인이다. hook이 도는 세션이라면 세션을 어디서 열었든, 작업 대상 프로젝트의 카드가 만들어지고 그 프로젝트의 recall도 주입돼야 한다. 설계 문서를 먼저 쓰고 리뷰를 받은 다음 구현한다.

- **현재 동작.**
  - route 선택은 hook payload의 `cwd` 기준이다(`laya_host_response.select_route(cwd, …)`, `config._project_search_dirs`). cwd에서 HOME까지 위로만 올라가며 가장 가까운 `.memvane/settings.json`을 찾고, 하위 디렉터리는 보지 않는다.
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
  - 연결 worktree·preview·blocked·bundle drift 대상의 처리. 지금 라우팅 제외 규칙과 맞춘다.
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
  - 프로젝트 밖 원문 편집 라우팅(`laya_external_edits`)은 편집 hook마다 state base(`~/.local/state/memvane/<install>`)를 최대 128개(`MAX_INSTALLS`)까지 훑는다. 각 install의 journal 메타데이터를 읽고 가장 긴 원문 root 접두사로 대상을 고른다(2초 예산).
  - 초기 빌드 pointer(`initial-builds/<projectRoot 해시>.json`), 설치별 state 디렉터리, 연결 worktree view가 따로 흩어져 있다. 사용자 단위 단일 색인은 없다.
  - uninstall 뒤 정리 단계가 없다. retire·abandon·restore는 저장소와 journal을 남기고, 거절된 prepare도 state 디렉터리를 남긴다(2026-11-10 관측: 잠금 파일만 있는 `efb6dee9…`). 이런 디렉터리도 계속 훑기 대상이다.
- **목표.**
  - 사용자 단위 레지스트리 DB(state base 아래 SQLite)가 등록 경로를 가진다. 항목은 프로젝트 root, worktree root, install id, state root, 종류(main·worktree), 상태다.
  - hook은 cwd·편집 경로·세션 작업 집합을 레지스트리의 최장 접두사 조회로 프로젝트에 대응시킨다. 파일시스템을 훑지 않는다.
  - install·uninstall이 등록 lifecycle을 결정한다.
- **lifecycle.**
  - 등록: 온보딩 activate가 성공하면 프로젝트 root를 등록한다. 연결 worktree view가 처음 게시되면 그 worktree root를 같은 install의 하위 항목으로 등록한다.
  - 해제: uninstall(선택 해제)이 끝나면 그 install의 프로젝트·worktree 항목을 지운다. worktree는 `git worktree remove`·prune이 관측되면 해제한다.
  - 이동: 프로젝트 디렉터리 이동·이름 변경(`laya_project_dir_move`, state move)은 항목을 원자적으로 갱신한다.
  - 프로젝트 폴더 삭제: 등록된 프로젝트·worktree root가 사라지면 그 항목을 해제 대상으로 표시한다. uninstall과 같은 정리 범위를 적용하되, state 디렉터리·저장소·학습 상태 삭제는 uninstall의 승인 규칙을 따른다. 폴더가 다시 나타나면(같은 경로 재생성, 볼륨 재연결) 자동으로 다시 선택하지 않는다. 같은 install로 되살릴지 새 온보딩으로 볼지 정한다.
  - reconcile 시점: 상주 서비스(`laya_resident_service`)가 시작할 때 레지스트리 항목을 실제 폴더와 맞춘다. root 존재 여부, `.memvane` 존재 여부, inode 변경, journal 선택 상태, worktree 등록 상태(`git worktree list`)를 본다. 서비스가 오래 떠 있으면 주기적으로도 맞춘다. 맞추는 동안 공유 lock을 쥐지 않고(`context/pr10-decision-draft.md` §6 원칙), 항목마다 짧게 끝낸다.
  - 외장·네트워크 볼륨처럼 잠시 사라지는 경로는 즉시 해제하지 않는다. 유예 기간(또는 연속 관측 횟수)을 둔다.
- **먼저 정할 계약.**
  - 권한: 레지스트리는 조회 색인일 뿐 권한을 주지 않는다. 조회한 뒤 기존 selected route 증명(journal·route·attestation)을 그대로 통과해야 한다. 항목과 증명이 어긋나면 그 항목을 건너뛰고 백그라운드 reconcile이 고친다.
  - uninstall의 정의와 단일 진입점: 지금은 `restore-index-origin`, `retire-index-origin`, `abandon-*`로 흩어져 있다. 진입점을 하나로 모을지, uninstall이 지우는 범위(route 파일, state 디렉터리, wiki 카드, 학습 상태)와 owner 승인 단계도 정한다.
  - 동시성과 호환: Claude·Codex·Hermes의 서로 다른 번들 버전이 같은 레지스트리를 읽고 쓴다. schema 버전, 잠금, 구 번들이 레지스트리 없이 동작할 때의 fallback(지금의 훑기)을 정한다.
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
- **R2:** journal이 선택한 S 형식 schema3 store만 대상으로 하고 inode·incarnation·writer proof를 대조한다. shadow 후보, 선택 해제 store, 연결 worktree route는 제외한다.
- **R3:** 각 유계 `BEGIN IMMEDIATE`에서 모든 참조를 다시 확인하고 vector→embedding set→chunk→chunk set→attestation source→attestation→admission→blob 순으로 삭제한다. FK 위반·busy는 전체 rollback 후 defer한다.
- **R4:** set 단위 FK 부재만 보지 않는다. 모든 view의 head·직전, stage, delta, 진행/완료 fill 대상, parity 및 외부 record, trust chain과 `coverage=complete`의 암묵 참조에 도달하는 blob을 보호한다. 살아 있는 blob의 모든 recipe/profile set을 보존한다.
- **R5:** orphan prepared/stage만 head/epoch를 transaction에서 재확인한 뒤 유계 batch로 정리하고 crash 뒤 재개를 검증한다.
- **R6:** 남아 있는 모든 구 번들이 실제 S 형식의 매 transaction에서 fail-closed하고 쓰지 않는지 양 Python 교차 시험한 뒤에만 활성화한다. 새 lock·소유 계약 변경이 필요하다고 확인되면 사용자에게 결정받는다.

실제 활성화 전 합성 RED/GREEN: (1) chunk set commit과 embedding callback 사이의 GC, (2) lexical head의 complete/partial fill set과 `coverage=complete`의 묵시 참조, (3) journal CAS 뒤 switched 기록 전 crash 및 stale shadow vector/prepare 핸들. 구 bundle RW 핸들의 형식 fence도 교차 시험한다. head 관측과 게시 순서에 따른 결과를 각각 기록하고, 예상 오류·고착은 재현 전 사실로 취급하지 않는다. 현재+직전 view 행 GC와 그 자체의 RED는 S v1 범위다.

PR6·7 이후, PR8 이전의 S 단계로 진행한다. 설계 문서를 독립 리뷰하고 사용자 승인을 받은 다음 구현한다. 저장 스키마·recall 읽기 경로·봉인 증명이 함께 바뀐다.

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
- **S v1 방향.**
  - SQLite rollback journal과 기존 lock 소유 계약을 유지한다.
  - 카드 행에 유효 구간(`gen_from`, `gen_to`)을 둔다. 바뀐 카드만 새 행으로 추가하고, 이전 행은 `gen_to`를 닫는다. 삭제된 카드는 행을 닫기만 한다.
  - rollback은 마지막 게시의 변경분을 역적용하는 새 게시다.
  - 각 view의 FTS는 head만 담고 게시 transaction에서 변경 카드만 갱신한다.
  - GC는 현재 head와 직전 세대를 보호하면서 view 행을 유계 batch로 회수한다. 공유 content GC는 위 후속 항목으로 미룬다.
  - 원문 content 지문은 HEAD를 포함하지 않고, 경로별 tracked 등 관측 증거를 별도로 보존한다. 세대마다 manifest·files 전체를 복사하지 않는다.
- **먼저 정할 계약.**
  - 봉인 증명: 새 세대 digest와 기존 attestation·journal·route의 결속을 정하고 검증한다.
  - 삭제 방지 트리거와 정확 일치 DDL 검사: GC 삭제 경로만 허용하도록 재설계한다. 구 번들이 새 store를 열 때의 동작(거절 코드)도 정한다.
  - recall 성능: head 전용 FTS5 질의의 지연을 카드 1천·4천·2만 장에서 실측한다. 기준은 hook 2초 예산 안의 기존 지연이다.
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
