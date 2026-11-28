# MemVane 로드맵


열린 항목의 단일 목록이다. 끝난 항목은 지우고 「출시됨」 표에 한 줄만 남긴다.

## 읽는 법

- **우선순위.** 대형 프로젝트의 recall 정확성 → 업데이트 신뢰성 → 학습 → 정리 순이다.
- **릴리스 번호.** `1.0.x` 패치는 [1.x 불변 목록](dev/contexts/release.md)과 사용자 승인 흐름을 바꾸지 않는 변경이다. 기본값과 동작의 변경을 포함한다. `1.1`은 설계 문서와 독립 리뷰가 먼저 필요한 변경을 담는다. 둘 다 불변 목록을 바꾸지 않는다.
- **불변.** `없음`은 영속 기록·lock·wake kind·settings 키를 건드리지 않는다. `확인`은 설계 단계에서 저촉 여부를 먼저 판정한다. `저촉`은 2.0 항목이다.
- **임베딩 규칙.** 정체성(모델 GGUF·tokenizer·chunker·차원·pooling·정규화·최대 토큰 길이)이 바뀌면 재빌드한다. 실행 방식만 바뀌면 sync owner가 `provider-reattest.json`으로 재봉인한다.
- **버전 혼재.** 한 기계에서 Claude·Codex·Hermes가 서로 다른 1.x 버전을 로드하는 상태가 정상이다. writer·worker 동작을 바꾸는 항목은 낮은 버전 호스트가 같은 store와 기록에 쓰는 경우를 설계에 넣는다.
- **크기.** S = 1~2일, M = 3~5일, L = 1주 이상이거나 설계 문서와 독립 리뷰가 먼저 필요하다.
- **합격.** 항목마다 live에서 재는 기준을 적는다. 출처는 설치 이벤트 로그(`memvane:logs`), store 읽기 전용 질의, 프로세스 목록이다. 기준 프로젝트는 카나리(qmd-auto-context)와 service-engineering(카드 3,751, vector 18,412)이다.
- **hook 지연 목표.** selector를 뺀 recall은 p50 0.7초·p95 1.2초, selector를 포함하면 p50 1.5초·p95 2.5초, 대형 프로젝트(카드 1,000장 초과)는 selector 포함 p50 2.0초·p95 3.0초다. semantic 단계를 포함한 값이다. 닿지 않는 구간은 실측으로 가능한 값을 보고하고 다시 정한다.
- **검증 방식.** 병합 전에는 변경 단위 focused 시험, PR CI(macOS·시스템 Python 3.9 포함), 최종 커밋의 전체 검사, 실제 모델 검사, 독립 리뷰다. 출시 뒤에는 별도 기계의 테스트 계정에서 도는 smoke와 기존 설치의 SLO 확인이 백그라운드로 돈다(`docs/release.md`). 출시 뒤 항목은 병합과 출시를 막지 않는다.
- **모양 제약.** 새 lock·journal record·wake kind·hook action·settings 키·compile 큐 dedup 키는 사용자 결정 사항이다.

## 출시됨

| 버전 | 내용 |
|---|---|
| 1.0.1 | 학습 runtime 활성화 명령, resident 재시도 예산, 원문 불일치 카드의 trust 해제, view 행 GC, Linux CI, hook import 지연 |
| 1.0.2 | 첫 lexical 세대에서 초기 빌드 ready, `progress` outcome, build-status 진행 표시, 호스트 업데이트 백그라운드 인계 |
| 1.0.3 | 임베딩 worker Metal GPU, 재빌드 없는 재봉인, 경량 학습 runtime 활성화, 옛 번들 선점 |
| 1.0.4 | 모든 provider 경로의 재봉인 인정, provider 부재 시 lexical recall |
| 1.0.5 | provider API 인자 불일치 수정 |
| 1.0.6 | vector fill 처리량(cursor 유지, ingest pass 예산, capture·plan 재사용, fill batch), 단계별 시간 이벤트 |
| 1.0.7 | 학습 기본 켜짐(`local-auto-apply`, 기존 settings 키로 끈다) |
| 1.0.8 | vector 세대 단일 게시, recall 단계 계측과 semantic 상한, 재봉인 쌍별 기록(`provider-reattest-pairs.json`), 정체성 manifest의 템플릿·context 여유, 업데이트 인계 마무리, provider smoke 게이트, pending 증명과 selector 유지보수 분리 |
| 1.0.9 | 학습 유지보수의 서비스 연결(heal→warm→프로필), recall 세대 검증 재사용, 학습 capture의 hook 밖 실행 |
| 1.0.10 | macOS Accelerate 기반 semantic 점수 계산(대형 프로젝트 semantic recall, `MEMVANE_VECTOR_ACCEL=off`) |
| 1.0.11 | 학습 포인터 `schema_version`, 학습 서비스 시작 실패 분류와 지연 재시도, `skipPaths` 원문의 최신성 비교 제외, 옛 세션 재시작 안내 |
| 1.0.12 | 학습 기동·warm 시간 예산 분리, 학습 요청 재예약 |
| 1.0.13 | 학습 프로필의 프로젝트 settings(0644) 읽기 |
| 1.0.14 | 미활성 학습 namespace의 adapter 경로 재연결 |
| 1.0.15 | 중단된 기본 학습 등록의 복구, 옛 설치 compile 작업의 예산 소비 차단, 진척 있는 compile pass의 재시도 한도 제외 |
| 1.0.16 | 기본 학습 프로필의 업데이트 후 자동 재승인(MemVane 자기 marketplace 항목만 결속) |
| 1.0.17 | 자동 재승인의 설치 manifest(0644) 읽기 |
| 1.0.18 | 편집 시 recall 주입(Claude·Codex PostToolUse), 편집 학습 사례 수집, prompt 학습 사례 폐기 |
| 1.0.19 | selector 지연(질의·카드 조각 쌍, 후보 10장, 프롬프트 selector 예산 3초), host plugin 목록 기록(selector 유지보수만 느린 CLI를 기다림) |
| 1.0.20 | 호스트 간 자동 재승인의 출처 비교(두 host 모두 공식 MemVane repo) |
| 1.0.21 | teacher 기본 묶음(세션 호스트의 최저가 모델이 카드만 보고 편집 사례 정답 작성, 격리 실행, 기존 local-only 설치 자동 전환, 운영 중 순차 평가 승격, 끄기는 setup `teacher-off`) |
| 1.0.22 | teacher 학습 정지·유실 수정(응답 보관, 업데이트 뒤 후보 승계·옛 승인 cycle 닫기, 판정 후보 닫기, 100건 전 학습 안 함, 최근 사례 우선), 수집 상한·유입·미룸 사유·semantic 시간 기록, 검색 필요 신호 기록 |
| 1.0.23 | teacher 후속 2(평가 중단·일시 자원 실패가 후보를 막지 않음), 준비 lock 경합의 짧은 재시도, 기본 teacher 사례 1,800행 순환 보존, 검색 필요 신호 탐침의 개수 전용 읽기, 학습 일일 관측 줄 |
| 1.0.24 | 연결 worktree 편집의 학습 사례 수집(main 세션과 worktree 세션), trainer 실패 자리와 거절한 검사 기록, busy 재시도 하한·유지보수 미완 사유 |
| 1.1.0 | 재부팅 내성(장치 번호 비교 제거), sync의 main 게시와 연결 view 대기 분리, SLO 요약, 긴 프롬프트 4조각과 세션 기주입 카드 제외, vector arena(대형 프로젝트 semantic 읽기), hook 고정비 축소, selector 정체성 재사용, 학습 상태 기계와 승격 규칙·재시도 상한, 학습 모델 되돌리기, 번들 없음 종료와 state 정리, 모듈 단위 검사 러너와 macOS CI, 출시 smoke 도구 |
| 1.1.1 | 설치별 provider 증명 실패 분류와 재시도, 1시간 뒤 재증명, provider 기동 실패 시 lexical recall, 부분 vector 검색, 같은 원문의 compile·pending 검수 중복 제거 |
| 1.1.2 | 구조적 식별자 단독 어휘 검색, resident 증명 EIO 훅 보호·냉기동·반복 adopt 진단 정리, discovery lock 경합 grant 건너뛰기와 번들 조회 재시도, 예약 실패 롤백·고아 hold 회수·hold 스캔 상한(첫 카드 게시 정체 복구), 출시 smoke 도구와 shim 예열 보강 |
| 1.1.4 | 위키 파이프라인 v2(`stateFormat` 2, `~/.local/state/memvane/v2` 0700 생성·넓은 권한 복구), 훅 경량화(`edit-inbox.jsonl` 단일 append·discovery 중복 스킵), 단일 SQLite 원장(`.memvane/compile/wiki-pipeline.sqlite3`·큐/검수/게시/억제/쿨다운/dedup/생성 이력/보존 정리), 단일 교환 게시·행 단위 복구·교환 미지원 파일시스템 보호, tombstone 제거와 dedup 이름표 이전, 1.x 일회성 마이그레이션, `entry/run.py wiki-ledger` 조회 CLI |
| 1.1.5 | 학습·색인 경합 및 sync owner 점유 계측, 연결 worktree view 대기 분리, recall 지시문 문장 필터링(표지어·코드 펜스 보호), ingest delta와 `source_fingerprints` 재사용, `checkout_view.py`·`shared.py` 분해, 학습 안정화 4종(이중 고장 되돌리기·손상 기본 모델·재시도 계수 영속과 진척·실측 trainer peak 승인과 유휴 모델 해제) |
| 1.1.6 | 로컬 웹 대시보드(UI) Phase 1(좌측 프로젝트·worktree 트리, Overview, Wiki Browser·원문 대조·SafeMarkdown, Recall 인스펙터·무오염 4단계 Query Playground, Runtime·Learning 모니터, opt-in SessionStart 자동 오픈, `memvane:ui` 스킬 및 온디맨드 보안 싱글톤 서버, `diagnostics`·`recall`·`learning`·`resident`·`wiki/pipeline` read-only 파사드), 대시보드 체크아웃 상태 표시(해제·경로 없음 숨김, 상위 `.git` 브랜치)·원문 삭제/변경 배지·디스크 집계 분리, 원문을 고치지 않은 옛 브랜치를 보유 근거에서 뺀 원문 삭제 카드 은퇴, 옛 revision 카드 재추출과 `superseded_by_revision` 은퇴, 검수 행 없는 generated 카드의 cutover 이관과 1회 재적재 |

## 한눈에

| 릴리스 | 항목 | 크기 | 불변 |
|---|---|---|---|
| 1.1.x | L3 검색 필요 판단기 | M | 없음 |
| 1.1.x | 1.1.0 이월 항목 | S~M | 확인 |
| 1.0.x | 2 resident 선행 요청 | M | 없음 |
| 1.0.x | 6 재봉인·온보딩 안내 정리 | S | 없음 |
| 1.0.x | 7 GPU 폴백 후속 | S | 없음 |
| 1.0.x | 8 운영 가시성 | M | 없음 |
| 1.0.x | 9 시험 기반 | M | 없음 |
| 1.0.x | 10 문서와 릴리스 절차 정합 | S | 없음 |
| 1.0.x | 11 방어 심화, 세션당 고정비 | S | 없음 |
| 1.0.x | 13 setup gitignore 안내 보강 | S | 없음 |
| 1.0.x | 14 머신 간 중복 카드 정리 | M | 확인 |
| 1.0.x | 15 학습 유지보수의 전체 시간 상한 | S | 없음 |
| 1.1 | 1 대형 프로젝트 semantic 검색 구조 | L | 확인 |
| 1.1 | 2 worker 안 batch 임베딩 | M | 확인 |
| 1.1 | 3 admission 분리와 busy 사유 | M | 없음 |
| 1.1 | 4 공유 content GC | L | 없음 |
| 1.1 | 5 진단과 deferral 정확성 | M | 없음 |
| 1.1 | 6 recall·wiki 품질 | M | 확인 |
| 1.1 | 7 설치 수명 주기 마무리 | M | 확인 |
| 1.1 | 8 시간 예산 | L | 확인 |
| 롤아웃 | G8 mini | — | — |

```
L2 후속 ── L3
1.1-1 C2 (resident 메모리 vector 검색)
```

## 학습 개편

학습 신호는 편집 시점에서만 얻고 prompt 사례는 쓰지 않는다. 남은 단계는 아래이고 설계 문서와 독립 리뷰를 따른다.

### L3 검색 필요 판단기 (1.1.x)

- **목표.** 검색할 거리가 없는 프롬프트(맞장구·작업 지시)에서 프롬프트 recall을 건너뛴다.
- **범위.** recall 맨 앞에서 "이 프롬프트가 과거 기록 검색이 필요한가"를 판정한다. 문자열 패턴이 아니라 lexical 색인 신호로 시작해 질의 임베딩 위의 경량 분류기로 옮긴다. teacher는 쓰지 않는다. 애매하면 검색한다. 편집 recall에는 적용하지 않는다.
- **의존.** 1.0.22(신호 기록 1주). **크기.** M. **불변.** 없음.
- **합격(live).** 판단기가 건너뛴 프롬프트의 비율과 건너뛴 뒤 같은 세션에서 수동 query가 늘지 않는지를 로그로 본다.

## 1.1.x 열린 항목 (1.1.0 이월)

- **학습 검사.** 여러 조각에 걸친 cursor 재개의 실제 모델 시험, 학습 점검의 일관성 읽기, 쌍 예산의 정확 비교와 12→20 복귀 기준.
- **wiki.** 편집 뒤 원문이 root 안 symlink로 바뀌면 refresh pending이 해결·종결되지 않는다.
- **recall resident/API 방식.** 1.2 후보다.
- **recall 순위.** 선택 reader 경로의 일반어 선택(`memvane/recall/keywords.py`)은 코퍼스 필터 없이 위치로만 자른다. 긴 질의에서 일반어 선택, lexical 일치의 비중과 fusion 가중을 조사한다.
- **세션 밖 변경의 즉시 반영.** 세션 밖 변경(에디터 편집, 터미널의 `git pull`·checkout)은 다음 SessionStart, 프로젝트 안 편집(`index`), Claude의 성공한 Bash(`file-change-event`) 때 full-scan sync로 반영된다. 프롬프트(UserPromptSubmit)는 sync를 깨우지 않아서, 열린 세션에서 에이전트가 편집·Bash를 하기 전까지 색인이 옛 상태로 남는다. 후보는 셋이다. (A) resident가 선택된 프로젝트를 OS 파일 이벤트(macOS FSEvents)로 감시해 sync를 깨운다. (B) recall hook이 sync 요청을 함께 남기고 짧은 간격의 중복은 합친다. (C) resident의 주기 점검. A를 기본, B를 보조로 검토한다.
- **resident 상시 기동(OS 로그인 항목).** 지금 resident는 hook이 처음 필요할 때 띄우고, 재부팅 뒤 첫 프롬프트가 기동과 모델 로드를 기다리며, 죽거나 영구 실패에 빠지면 다음 hook 전까지 되살리는 주체가 없다. OS 로그인 항목으로 상시 기동하는 안을 위 감시 항목과 함께 검토한다. macOS는 launchd 사용자 agent(`~/Library/LaunchAgents`, KeepAlive), Windows는 작업 스케줄러 로그온 작업 또는 시작 프로그램, Linux는 systemd `--user` unit이다(Windows·Linux는 지원 플랫폼이 되면 적용). 검토할 것: 등록·해제를 setup의 명시 승인으로만 하고 플러그인 제거 때 함께 지우는 방법, 플러그인 버전이 바뀔 때 실행 경로를 갱신하는 방법(지금의 번들 인계와의 관계), 쓰지 않는 날의 메모리(resident 약 350MB, 학습 resident는 수 GB), hook 기동 방식과의 공존. 새 로그인 항목과 설정 키는 사용자 결정 사항이다.
- **compile 진척.** compile 진척 streak은 상태 파일을 쓰지 못하는 동안 쌓이지 않는다.
- **시험 격리.** `test_setup_entry_inventory_119`가 Claude Code 안에서 돌면 `CLAUDECODE`·`CLAUDE_CODE_*` 환경 변수 때문에 실패한다. 시험이 host 환경 변수를 지우지 않고, host 판별이 환경 변수에 기대는지 확인한다.
- **리뷰 범위.** `vectorcache/writer.py`의 compaction·append, teacher와 `automatic_staging`의 유료 호출 경로를 독립 리뷰한다.
- **일반 편집 후보의 부분 실패.** 한 원문에서 나온 두 후보 중 하나의 첫 쓰기가 실패하면, 재시도가 원문을 다시 추출하고 남은 후보가 사라진다. 원래 응답과 후보별 완료 기록, 혼합 버전의 재시도 경계를 설계한다.
- **Hermes.** 설치 manifest가 `plugin.json`과 `plugin.yaml`로 갈리고, `pre_llm_call`이 cwd를 넘기지 않아 recall이 일어나지 않는다.

## 1.0.x 패치

번호는 권장 순서다.

### 1.0.x-2 resident 선행 요청

- **현재 동작.** fill은 문서 하나를 임베딩하고 store commit·검증이 끝난 뒤 다음 요청을 보낸다. 임베딩 중에는 Python이, commit 중에는 worker가 논다.
- **목표.** fill window에서 worker가 쉬지 않는다.
- **범위.** 다음 target의 요청을 이전 target의 commit·검증과 겹쳐 보낸다. protocol `1`과 요청 모양을 유지한다. content claim은 임베딩 동안 보유하고, deadline 뒤에 끝난 결과는 게시하지 않으며, provider 호출 경계의 초기 빌드 양보 검사를 선행 요청에도 적용한다.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **합격(live).** service-engineering의 fill pass에서 `sync.profile`의 provider 시간이 pass 벽시계의 70% 이상이다(현재 약 45%). 새 카드 1장당 벽시계가 0.3초 이하다.

### 1.0.x-6 재봉인·온보딩 안내 정리

- **범위.** prepare 뒤 activate 전인 설치도 재봉인 대상에 넣거나, 진행 중 온보딩은 다시 시작한다는 안내를 낸다. 수동 `update` skill이 재봉인 가능한 설치에 "다시 온보딩"을 내지 않는다. `provider.reattest rejected` 이벤트를 설치당 한 번만 남긴다. 재봉인 검사 실패를 resident가 `attestation_invalid`로 분류한다. 유효한 기록이 있으면 sync pass가 owner 획득 없이 재봉인 준비를 건너뛴다. setup skill의 버전 전환 서술을 현재형으로 고친다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격(live).** 업데이트 한 번에 설치당 `provider.reattest` 이벤트가 2건 이하다. 재봉인 가능한 설치에서 `update` skill이 queued를 보고한다.

### 1.0.x-7 GPU 폴백 후속

- **범위.** CPU로 내려간 worker가 resident 수명 동안 CPU에 남지 않게 GPU 복귀 시도를 둔다(상한 있음). CPU 폴백의 thread 수를 성능 코어 수 이하로 둔다. 입력 하나의 결정적 실패와 GPU 실패를 구분한다.
- **의존.** 없음. **크기.** S. **불변.** 확인(worker 바이트 변경은 재봉인 대상이다).
- **합격(live).** 7일 동안 resident worker의 실행 상태가 GPU인 시간 비율이 99% 이상이다. CPU 폴백 중 recall p95가 목표의 1.5배 이하다.

### 1.0.x-8 운영 가시성

- **목표.** 멈춘 이유가 사용자에게 보인다.
- **범위.** 디스크가 가득 찼을 때 wake worker 재기동 루프를 막고 `candidate_storage_full`을 안내한다. 수명 주기 대량 삭제 가드가 보류한 run을 SessionStart 한 줄로 알린다. compile이 돌지 못하는 사유(`needs_extractor`)를 안내한다. 처음 보는 브랜치의 cold view 동안의 안내. 백그라운드가 번들을 찾지 못한 상태(`unavailable`)를 이벤트로 남긴다. 활성화 뒤 첫 backfill의 호스트 CLI 호출 규모를 setup 승인 단계에 적는다. embed runtime을 다시 준비했을 때 기존 설치가 재봉인되는지 재빌드되는지를 setup이 미리 알려 준다.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **합격(live).** 각 상태를 fixture로 만든 SessionStart에서 안내가 정확히 한 줄 나온다. 다음 프로젝트 온보딩(G8 포함)에서 사용자가 로그를 열어야 원인을 알 수 있던 정지가 0건이다.

### 1.0.x-9 시험 기반

- **목표.** 로컬 전체 검사가 20~30분이고, 지원 하한(Python 3.9)과 최신에서 같은 검사가 CI로 돈다.
- **범위.** 일반 `guard --kind test`의 파일 병렬도를 3~4로 올린다(자원 제한이 있는 작업과 단독 실행 규칙은 유지한다). Node 22·24 × Python 3.9·3.13 조합은 GitHub Actions에서 병렬로 돌고 로컬은 Python 3.9와 Node 한 버전으로 돈다. 시험 공백: 쓰기 때 현재 원문 판정의 경계 사례, 온보딩 중간 기록이 재부팅으로 `st_dev`만 바뀐 뒤 재개되는지, Hermes 선택 route 분류 뒤 import 실패, vector page 예산 경계.
- **의존.** 없음. **크기.** M. **불변.** 없음.
- **열린 결정.** 없음. 전체 검사의 파일 병렬도는 3이고, PR은 ubuntu Node 22와 macOS·시스템 Python 3.9로 돈다.
- **합격.** 로컬 전체 검사 벽시계 30분 이하. main CI 전체 조합이 연속 5회 통과한다.

### 1.0.x-10 문서와 릴리스 절차 정합

- **범위.** `docs/release.md`·`docs/release-agent-qa.md`를 현재 검증 방식에 맞춰 줄인다. `recipe_fingerprint`의 loader 문자열이 값이 아닌 라벨이라는 점을 적는다. 없는 진단 이름, minimal settings의 허용 키 5개 정합, `_render_context`의 비위키 행 분기를 확인한다. 저장소 자체 위키에서 없는 동작을 설명하는 카드를 퇴역한다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **열린 결정.** 없음. 설치본 agent QA 계약은 마이너 버전마다 도는 전수 점검이다(`docs/release.md`의 Q6).

### 1.0.x-11 방어 심화, 세션당 고정비

- **범위.** fixture provider seam 게이트의 temp-root 조건. reader 경로 `journal_directory(create=True)`의 경로 모양 검사. journal `_open`의 `mode=ro` 예외 전제를 단정으로 고정. fill call 범위에서 store header 검증 결과를 `schema_version`·`data_version`이 같을 때 재사용한다(카드당 약 10 ms).
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격.** focused 시험. 고정비는 `sync.profile`의 storage 시간이 카드당 10 ms 이상 줄 때만 유지한다.

### 1.0.x-13 setup gitignore 안내 보강

- **운영 근거.** 프로젝트 포인터 외에도 compile 작업 파일(`wiki-pipeline.sqlite3`·`edit-inbox.jsonl`·lock 등)과 wiki 게시 중 임시 숨김 파일(`.publish-*.tmp`)이 Git 미추적 항목으로 나타날 수 있다.
- **범위.** setup의 `.gitignore` 안내에 `.memvane/compile/`과 `.memvane/wiki/**/.*`를 더한다. 기존 안내와 추적 파일은 유지한다.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격.** setup 안내에 두 줄이 각각 한 번 나오고, 적용 뒤 compile과 숨김 임시 파일은 무시되며 일반 wiki 카드와 이미 추적된 파일은 유지된다.

### 1.0.x-14 머신 간 중복 카드 정리

- **운영 근거.** 같은 원문에서 머신마다 이름이 다른 wiki 카드가 생겨 중복이 누적될 수 있다.
- **범위.** 1.0의 사후 dedup 스캔이 실제로 실행되는지 확인한다. 같은 원문 카드의 결정적 경로와 기존 중복 처리 기준을 정하고, 중복이 누적되면 SessionStart에 안내한다.
- **의존.** dedup 스캔 동작 확인. **크기.** M. **불변.** 확인(기존 카드·queue 계약 유지 여부를 판정한다).
- **합격.** 두 머신에서 같은 원문에 대한 카드 경로가 같고, 이름이 다른 기존 중복을 스캔이 발견한다. 누적 조건에서 SessionStart 안내가 나오고 정상 카드가 보존된다.

### 1.0.x-15 학습 유지보수의 전체 시간 상한

- **현재 동작.** 학습 유지보수 한 run은 전체 상한 1,800초(`selector_maintenance.MAINTENANCE_SECONDS`)에서 `maintenance_timeout` 실패로 끝나고 학습 준비 잠금을 반납한다. 다른 설치의 유지보수는 그 잠금을 기다리는 동안 `learning_prepare_busy`로 밀린다.
- **범위.** 상한이 live에서 지켜지는지 확인한다. 단계별 상한과 자식 프로세스 대기의 유계 timeout.
- **의존.** 없음. **크기.** S. **불변.** 없음.
- **합격(live).** 7일 동안 유지보수 run의 최장 시간이 상한 이하이고, 한 설치가 준비 잠금을 상한 넘게 쥐는 사례 0건.

## 롤아웃

### G8 mini

- **조건.** 1.0.18 이상이 이 Mac에서 업데이트 인계(새 버전 SessionStart 뒤 2분 안에 옛 버전 프로세스 0개)를 한 번 채운다. mini의 `claude`·`codex` 설치 경로(Homebrew 포함)와 `settings.json`·`config.toml`의 링크 여부를 먼저 확인한다.
- **절차.** S7 runbook 5절(M1~M8). store는 복사하지 않는다.
- **합격(live).** 새 기계에서 embed runtime 준비부터 첫 프로젝트 recall의 semantic 사용까지 수동 프로세스 개입 0회. mini에서 한 번의 업데이트가 위 인계 기준을 채운다.

## 1.1

### 1.1-1 대형 프로젝트 semantic 검색 구조

- **목표.** vector 100,000개 규모에서도 recall의 semantic 단계가 hook 예산 안에 끝난다.
- **현재 동작.** recall hook은 pin된 세대의 세트 가운데 vector arena가 포함한 것을 arena의 매핑된 행에서 점수 내고, arena에 없는 세트만 store에서 읽는다(`view_accel_search._arena_scores`). arena가 모든 세트를 포함하면 store vector를 읽지 않는다.
- **범위.** 카드 대표 vector는 사용하지 않고 resident 메모리 vector 검색(내용 주소 arena)으로 간다. 설계는 별도 문서에서 다루고 독립 리뷰를 먼저 진행한다.
- **의존.** 없음. **크기.** L.
- **불변.** 확인(store DDL에 색인을 더하면 저촉이다. resident 요청 종류를 더할 때 낮은 버전 resident의 동작을 판정한다. 파생 캐시는 세대 digest에 묶이고 없으면 exact로 돌아가야 한다).
- **합격(live).** service-engineering의 recall에서 semantic 사용 비율 99% 이상, p95가 목표 이하, exact 대비 상위 10개 겹침 9개 이상(표본 100건).

### 1.1-2 worker 안 batch 임베딩

- **목표.** 여러 chunk·문서를 한 번의 worker 호출로 임베딩한다.
- **범위.** `laya_local_embed_worker.mjs`의 batch 처리. 실행 방식 변경이므로 재봉인으로 넘어간다. 정체성(템플릿·최대 토큰·절단 없음)은 그대로다.
- **의존.** 1.0.x-2. **크기.** M. **불변.** 확인(worker 파일 이름과 요청 envelope을 유지한다).
- **합격(live).** 업데이트 뒤 재빌드 0회. 초기 빌드의 fill 처리량이 1.0.x-2 대비 1.5배 이상이다.

### 1.1-3 admission 분리와 busy 사유

- **현재 동작.** 학습과 색인이 같은 wake 큐와 dispatcher lease를 쓴다. 학습 추론 슬롯이 차 있으면 sync·초기 빌드·recall admission이 `learning_*` 사유로 미뤄진다.
- **목표.** 기존 큐·wake kind·lock을 유지한 채 학습이 recall·sync·초기 빌드를 막지 않는다.
- **범위.** admission을 색인용과 학습용으로 나눈다. sync·초기 빌드·recall 경로에서 학습 수요 확인을 뺀다. 학습이 상위 수요에 한 step(5초 이하) 안에 양보한다. 학습 예약 메모리를 회수 가능으로 센다.
- **의존.** 1.1.5(학습·색인 경합 계측). **크기.** M. **불변.** 없음(큐·lease 분리는 2.0이다).
- **합격(live).** 7일 동안 `learning_*` 사유로 미뤄진 recall·sync 0건. 학습 cycle 완료 건수가 분리 전의 80% 이상이다.

### 1.1-4 공유 content GC

- **현재 동작.** 여러 view가 공유하는 blob 본문·admission·attestation·원문 지문·chunk/embedding set은 지우지 않는다.
- **목표.** 어떤 view의 head·직전 세대에서도 닿지 않는 공유 내용을 회수한다.
- **범위.** 선택 sync의 owner·writer scope 안에서 게시·fill이 정상 반환된 뒤 같은 프로세스에서만 실행한다. journal이 선택한 store만 대상으로 한다. 유계 `BEGIN IMMEDIATE`마다 참조를 다시 확인하고 vector → embedding set → chunk → chunk set → attestation → admission → blob 순으로 지운다. head·직전·stage·delta·진행 중 fill 대상·compile intent·trust chain에 닿는 blob을 보호한다. 참조 없는 원문 지문과 소비가 끝난 compile intent 파일을 회수한다.
- **의존.** 1.1.5(ingest delta화), `writer_floor` 판정. **크기.** L(설계 문서와 독립 리뷰 선행). **불변.** 없음(DDL과 compile intent 키 집합을 유지한다).
- **합격(live).** service-engineering의 `source_fingerprints`가 225,612행에서 살아 있는 관측 수의 2배 이하로 준다. store 파일 크기가 30일 동안 카드 수 변화 비율 안에서 움직인다. GC가 돈 pass의 recall p95가 목표 이하다.

### 1.1-5 진단과 deferral 정확성

- **범위.** recall 거절 이벤트의 내부 고정 코드 필드. lexical 0건 recall의 reason은 `no_candidates`다. off-hook 자식 진입점 전체가 `ResourceDeferred`를 deferral로 끝내는지 전수 점검. `normal_writer_deadline`의 sync 분류. compile 중첩 worker의 실패 범주와 backlog 크기 필드.
- **의존.** 1.1.5(연결 worktree view 대기 분리). **크기.** M. **불변.** 없음.
- **합격(live).** 7일 이벤트에서 사유 없는 `failed`·`refused`가 0건이다.

### 1.1-6 recall·wiki 품질

- **범위.** verify defer 루프의 종점(`source_changed_before_verify`, `content_hash_mismatch`, `missing`의 분리). vector 대상에서 빠지는 카드(service-engineering 3,751장 중 568장)의 사유 분류와 대책. 긴 프롬프트의 `laya_selector_invalid_pool`. subagent·하네스 생성 프롬프트의 주입 기준.
- **의존.** 1.1-5. **크기.** M. **불변.** 확인(`.memvane/compile/wiki-pipeline.sqlite3`의 `verify_jobs`에 재시도 상태를 싣는 방식이 원장 DDL 계약에 저촉되는지 판정한다).
- **열린 결정.** subagent·하네스 프롬프트의 주입 기준. 판단 전에 이벤트 로그로 건수와 분량을 잰다.
- **합격(live).** verify 큐에서 14일 넘게 남은 행 0건. vector 대상에서 빠진 카드 비율이 5% 이하이거나 사유별 건수가 이벤트에 남는다.

### 1.1-7 설치 수명 주기 마무리

- **범위.** 해제된 설치 디렉터리의 미리보기와 승인 삭제. uninstall이 지우는 범위의 정의. 플러그인을 제거·비활성한 뒤 남는 pending wake와 resident·monitor의 종료. 레지스트리의 이전 tombstone, 사라진 worktree 행, 조회 경로 Git 호출의 예산. 학습 runtime 세대 GC가 pending 세대를 지우지 않는지 확인.
- **의존.** 없음. **크기.** M. **불변.** 확인(레지스트리 DDL을 유지한다).
- **합격(live).** 플러그인을 비활성한 뒤 10분 안에 MemVane 프로세스가 0개다. 해제된 설치 디렉터리가 승인 삭제 뒤 0개다.

### 1.1-8 시간 예산

- **목표.** 사용자당 lock을 한 번에 쥐는 시간에 상한이 있고, resident 교체 중 vector 폴백 구간이 5초 이하다.
- **범위.** vector window를 여러 프로젝트가 나눠 쓰도록 쪼갠다. embed runtime 준비의 `npm ci`·모델 다운로드를 사용자당 lock 밖으로 옮긴다. resident 교체를 make-before-break로 바꾸고 메모리 admission이 막으면 break-before-make로 돌아간다.
- **의존.** 1.1.5(sync owner 점유 계측), 1.1-3. **크기.** L. **불변.** 확인(resident 교체 절차가 `successor.lock`·protocol `1`의 의미를 바꾸면 저촉이다).
- **합격(live).** 10개 프로젝트 환경에서 편집부터 게시까지 p95 2분 이하. 업데이트 중 `laya_service_preparing` 폴백 recall이 5초 구간 밖에서 0건이다.

## 1.1 이후

| 항목 | 크기 | 불변 | 다시 꺼낼 조건 |
|---|---|---|---|
| 세션 위치와 무관한 recall(미온보딩 루트, 프로젝트 간 편집) | L | 확인 | 1.1-7 뒤. 설계 문서 선행 |
| 연결 worktree의 카드 수명 주기 | L | 확인 | 위 항목 뒤 |
| 대시보드 제어 액션·편집·알림 ([설계](design-local-web-ui.md) M2) | M | 확인 | 1.1.6 Phase 1 대시보드·1.1-7 뒤 |
| 도메인 `domain/` 층 실체화 | L | 없음 | 패치 릴리스 간격이 1주 이상으로 안정된 뒤. 도메인 단위로 나눈다 |
| 원문 소실 보류(`held_missing`)와 삭제 receipt | L | 확인 | 삭제 근거를 store에서 찾아야 하는 사례가 생길 때 |
| 학습 중지·재개의 end-to-end 증거, 학습 자식 부하 측정 | M | 없음 | 1.1-3과 함께 |
| 코드 구조 신호를 쓰는 recall([설계](design-code-structure-recall.md)) | L | 없음 | 카나리와 service-engineering에서 편집 recall이 놓친 직접 import 이웃의 정답 카드를 확인하고, 게시된 출처 또는 명시적 문서 인용으로 연결할 수 있을 때 |
| Python 하한 3.9 → 3.11 | M | 없음 | 1.1.4 다음의 별도 버전. 훅은 bare `python3`을 실행하고 macOS 시스템 Python이 3.9이므로, `hooks/run-hook`의 버전 검출·설치 안내와 CI 행렬(시스템 3.9 → 3.11)을 같은 버전에서 바꾼다. 그 전까지 새 모듈은 3.9 문법(PEP 604는 `from __future__ import annotations`, `match`·`except*` 금지)을 지킨다 |

## 2.0 후보

1.x 불변 목록을 바꾸는 항목이다. 지금은 열지 않는다.

| 항목 | 저촉 지점 |
|---|---|
| 학습 큐와 dispatcher lease 분리 | wake 큐 DB의 위치·DDL, lock 이름. 1.1-3 뒤에도 경합이 실측될 때 |
| lock 범위 축소(`provider.lock`을 설치 단위, sync owner를 프로젝트 단위) | lock 이름과 위치. 1.1-8로 부족할 때 |
| 같은 store 안의 임베딩 정체성 교체(모델·chunker 변경 뒤 store 유지) | route·build·journal의 profile 결속. 지금은 재빌드다 |
| store 안의 영속 근사 검색 색인 | store DDL. 1.1-1의 파생 캐시로 부족할 때 |
| resident protocol 번호 변경이 필요한 검색·batch 위임 | protocol `1`, service 파일 |
| `provider-reattest.json` v1 모양의 변경·폐기 | 1.0.3 이후 호스트가 읽는 기록 |
| 학습 끄기를 한 키로 묶는 설정, hook 지연 목표 설정 등 새 settings 키 | settings 허용 키 집합 |
| 영속 이름 정리(`laya_*.mjs`, wake kind `laya-sync`·`laya-compile`, `laya-install.lock`, `schema3` 식별자) | 파일·kind·lock 이름 |

## 하지 않는 것

| 항목 | 이유 |
|---|---|
| frontmatter 없는 카드 writer | frontmatter 원문 지문을 유지한다 |
| recall 쪽 trust 규칙 변경 | 현재성은 쓰는 쪽이 맡는다 |
| 새 lock으로 공유 content GC 보호 | 그 lock을 모르는 버전에는 효력이 없다 |
| store의 WAL 전환, 단일 FTS + 세대 필터 | rollback journal과 view별 head FTS를 유지한다 |
| retired-wiki 보관본 자동 삭제 | 자동 삭제가 없다 |
| 기존 카드 일괄 재작성, 지문 없는 카드의 소급 백필 | 명시 요청이 있을 때만 한다 |
| 다른 메이저의 state를 읽거나 옮기는 코드 | 메이저가 다르면 다시 온보딩한다 |
| 설치본 전수 QA를 매 출시의 병합 조건으로 쓰는 것 | 병합 전 검사와 출시 뒤 smoke로 검증하고, 전수 QA는 마이너 버전마다 돈다 |

## 관찰

| 항목 | 다시 꺼낼 조건 |
|---|---|
| CPU 병목의 Rust 부분 도입 | 1.0.x-2·3과 1.1-1 뒤에도 Python 연산이 단계 시간의 절반을 넘을 때 |
| 중첩 프로젝트 온보딩, 다른 기계로 store 복사 | 요청이 있을 때 |
| Hermes의 SessionStart 안내 통로, 편집 뒤 컨텍스트 주입 | Hermes가 해당 hook 반환값을 쓰게 될 때 |
| `npx skills add` 설치 진입점 | installer 수명 주기 설계가 선행한다 |
| 카드 유효 구간, 카드 간 모순 lint, stale 전파, 주입 효과 평가 지표 | 사용자가 다시 다루기로 정할 때 |
| embed runtime 세대 GC 자동화 | `gc-index-runtime`으로 부족할 때 |

## 열린 결정

1. `writer_floor` 인상의 형태.
2. subagent·하네스 프롬프트 주입 기준(1.1-6).
3. 프로젝트 settings의 `diagnostics`를 켜 둘지.
4. 임베딩 모델 리비전 갱신과 재임베딩. 다운로드 주소는 고정 sha256과 같은 상류 리비전(`a0f9bb7a85ab…`)에 묶여 있고, 상류 main의 파일은 그 뒤에 바뀌었다(커밋 제목 "Updated model to include dense layers"). 두 파일의 임베딩 차이가 검색 품질에 무엇을 뜻하는지는 확인되지 않았다. 새 리비전으로 올리면 임베딩 정체성이 바뀌어 모든 store를 다시 임베딩해야 한다. 상류가 고정 리비전을 지울 때의 둘째 원천을 둘지도 여기서 정한다.
