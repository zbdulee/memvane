# 커밋 타임라인 · 문서 · 매니페스트 · 릴리스 준비 상태 (main 0f277e2 → HEAD 92ecd89)

## 1. 한 줄 요약

브랜치는 v2 topical wiki·로컬 학습(Laya)·관리형 QMD 설치/전환(setup)·Stop hook·업그레이드 가드를 8커밋(219 파일, +29,218/−732)으로 추가했다. 매니페스트 7곳 버전은 `0.35.2-dev.20261004`로 일치한다. 그러나 이 머신에서 `npm test`는 **1410 중 5 fail**(3건은 전역 qmd 인덱스를 읽는 비격리 테스트로 재현, 2건은 부하 의존)이다. 요구사항 추적 문서는 "운영 수락: 아직 미수행"(`docs/requirements-acceptance-trace-20261006.md:28`)이라고 스스로 선언한다. CLAUDE.md는 한 줄도 바뀌지 않았다.

## 2. 커밋별 의도와 범위

| SHA | 날짜 | 의도 (한 줄) | 범위 (`git show --stat`) |
|---|---|---|---|
| 73986ad | 10-06 | v2 topical wiki + 로컬 continual learning(Laya) + 관리형 QMD 설치기/프로젝트 index pointer + hook 예산·SessionStart durable queue를 "개발 브랜치 전용"으로 일괄 커밋. 본문: "Operational migration, activation, and rollback remain separate." | 168 파일, +21,842/−158. `core/context_learning/*`(28개: py 27 + Laya lock 1), `core/wiki_topical*`(12개: `wiki_topical.py` + `wiki_topical_*.py` 11), `core/qmd_installer.py`·`qmd_runtime.py`·`qmd_route.py`·`runtime_update.py`·`hook_budget.py`·`update_hook_queue.py`·`topical_hook_queue.py`, `locks/qmd-2.5.3/package-lock.json`(3,791줄), Laya lock, hooks.json/hooks-codex.json, docs 5개, 버전 bump 7곳 |
| 2962427 | 10-06 | CI(GitHub runner) 이식성: GNU/BSD `stat` 분기, Node 24 테스트 shim, 로컬 QMD snapshot 부재 시 skip. 본문 "Full serial: 1369 pass, 2 skip, 0 fail" | 4 파일 (`core/update.sh`, 테스트 fixture 3) |
| d3975a8 | 10-06 | topical fake-QMD 회귀를 이식 가능하게(가짜 QMD CLI/offline shim) | 8 파일, 테스트·`test_support/` 전용 |
| 15cd3db | 10-06 | topical worker 완료 상태를 기다리도록 테스트 수정 | `test/wiki_topical_reconcile_cases.py` 1 파일 |
| baa18f5 | 10-07 | 검토형 v2 setup·migration 조정기(`core/install_update.py` 913줄), WAL-안전 SQLite 읽기(`sqlite_read.py`), wiki 변경 잠금(`wiki_mutation_lock.py`), `wiki_init.py`, `update.sh` 정리(이 커밋 +218/−383, 브랜치 전체 +267/−390), setup skill 신설, README 설치 안내 개편 | 38 파일, +3,618/−508 |
| 400fd06 | 10-07 | setup cutover fixture를 CI 플랫폼 간 실행 가능하게 | 테스트 fixture 2 파일 |
| a0ffa23 | 10-07 | 관리형 QMD Node 지원을 22/24/26으로 정렬(`qmd_runtime.SUPPORTED_NODE_MAJORS`), Node 20 거부, CI matrix `[20,22]`→`[22,24,26]`, `package.json` `engines` 추가 | 9 파일 (`.github/workflows/test.yml`, README, `core/qmd_installer.py`, `core/qmd_runtime.py`, docs 2, SKILL.md) |
| 92ecd89 | 10-08 | setup 하드닝 + Stop job 복구: `setup_entry.py`(354줄, 요청 자동 생성), `setup_guard.py`(업그레이드 가드), `topical_stop_budget.py`(턴 단위 12작업/640센트 원장), `dirty_queue_claim.py`, hook 진입점 전부에 setup guard, `npm test`에 `QMD_SETUP_GUARD_FIXTURE=1` | 53 파일, +3,540/−190 |

커밋 본문은 73986ad·2962427 둘만 있고 나머지 6개는 제목뿐이다(`git log main..HEAD`). 리뷰 이력·검증 수치는 커밋이 아니라 docs의 날짜 문단에 쌓여 있다.

## 3. 문서 변화

### 3.1 신규 docs 7개 요지

| 문서 | 요지 |
|---|---|
| `docs/continual-learning.md` (206줄, 영어) | 로컬 continual context learning. `contextLearning.capture`(후보 풀 기록), 0–3장 선택 gold와 후보 라벨 분리(:12-23), 프롬프트 2,000자·후보 600자·compact body 16KiB 상한(:25-44), 로컬 cycle의 train≥100/val≥50/eval≥50 승격 규칙(:60-70), 95% 목표는 **학습 간격**만 조절(24h–14d, :72-82), `liveSelection: true`면 QMD 15개→Laya 0–3장(:97-104), 외부 teacher는 명시 정책·예산 하에서만(:106-116), Laya 0.3.20 adapter·관리형 runtime 설치 실측(:130-206). "development only"(:1) |
| `docs/hook-budget-and-recovery-20261006.md` (51줄) | `UserPromptSubmit`·`posttool`에 **총 18초 단일 deadline**(`QMD_HOOK_TOTAL_SECONDS` 2–20, host timeout 22초, :11-21). SessionStart는 durable job만 기록하고 독립 worker 실행(:23-33). Stop/SessionStart의 topical job 큐(:35-44). Codex 실호스트에서 종료 후 worker 완료 실측, **Claude teardown 미검증**(:50-51) |
| `docs/install-update-contract-20261006.md` (52줄) | setup 사용자 진입(자연어 → `setup` skill → `core/setup_entry.py`, :5-7), 지원 범위(macOS arm64, Node 22/24/26, :13), 요청 스키마 `qmd-install-request-v1`(:21-30), cutover 증명·rollback 규칙(:32-34, :42-48), `--bootstrap-empty-wiki`(:17) |
| `docs/install-update-writer-error-audit-20261006.md` (33줄) | 모든 제품 wiki writer와 잠금 경로 표(:5-16), 잠금 순서 install journal → project wiki(:18), cutover 실패별 결과·상태 표(:20-31) |
| `docs/operational-flow-map-20261006.md` (81줄, 한국어) | 네 흐름(적재·생성/수정/삭제, 검색·주입, SessionStart, 적응형 학습)의 "현재 일반 경로 / v2 격리 경로 / 미활성 연결" 구분(:3). 이후 10-07 Stop 다중 작업·턴 예산·원장 v3 보정 문단(:71-81) |
| `docs/qmd-installer-scope-20261006.md` (102줄, 영어) | QMD 2.5.3 lock 설치기(SHA, 289 패키지, 실측 다운로드 크기, :10-53), 프로젝트 index pointer cutover(`.auto-context/qmd-index-active.json`, :55-85), 플러그인 활성화 전 hook 효과 검토 요구(:95-102) |
| `docs/requirements-acceptance-trace-20261006.md` (75줄) | 요구사항 14행 표(:5-22) + 수락 단계 정의(:24-28) + 10-07 보정 문단 다수(:34-75). 아래 4절 |

### 3.2 README.md 사용자 안내 변화

- **qmd 수동 설치 지시 삭제**: main의 `bun add -g @tobilu/qmd@2.5.3` / `npm install -g …` 블록이 사라지고 "먼저 플러그인 설치 → `setup` 스킬에 상태 확인 요청"으로 바뀌었다(`README.md:45-50`).
- **관리형 설치는 macOS arm64 전용**, Linux·기타 아키텍처는 `>=2.5.3 <3.0.0` 전역 qmd CLI를 별도 설치(`README.md:26-27`, `:55-58`).
- **Node 정책**: 22·24 LTS 권장, Node 20 거부(`README.md:29-31`, `:51-54`).
- **자연어 설치 흐름**: "설치 상태 보여줘 / 격리해서 준비해줘 / 적용해줘 / 되돌려줘"(`README.md:79-85`). v1 wiki 카드는 v2 대응 검토 전까지 전환 보류, Laya 준비 ≠ 학습 활성화(`README.md:82-83`).
- README에 **없는 것**: (a) 업그레이드 후 기존 프로젝트가 명시 setup 전까지 자동 기능을 멈춘다는 사실(S-03, `core/setup_guard.py:1-6`), (b) 새로 등록되는 `Stop`·`topical-event`·`topical-reconcile`·`update-queued` hook(`hooks/hooks.json`), (c) recall 총 deadline 18초. `docs/qmd-installer-scope-20261006.md:95-102`는 "설치 전 hook 효과를 검토하라"고만 한다.

### 3.3 CLAUDE.md / AGENTS.md

- **CLAUDE.md diff 0줄** (`git diff main...HEAD -- CLAUDE.md` 빈 출력). setup_guard·qmd_route·topical·context_learning·Stop hook·`QMD_SETUP_GUARD_FIXTURE` 모두 없다.
- 그 결과 CLAUDE.md가 코드와 **모순**된다: `CLAUDE.md:185` "전체 훅 deadline은 의도적으로 없다" ↔ `core/hook_budget.py:7-9`(`DEFAULT_SECONDS = 18.0`, `MAX_SECONDS = 20.0`) + hooks.json `"timeout": 22`.
- AGENTS.md만 +1줄: `QMD_SETUP_GUARD_FIXTURE=1` 설명("운영 hook에서는 설정하지 말 것", `AGENTS.md:40`).

### 3.4 문서 간 모순: Node 26

- "native probe 아직 필요": `README.md:29-30`, `README.md:51-52`, `docs/qmd-installer-scope-20261006.md:16-17`.
- "26.10.0 native probe 통과" 또는 무조건 지원: `docs/install-update-contract-20261006.md:13`, `skills/setup/SKILL.md:12`("accepts Node 22/24 LTS and Node 26 Current").
- a0ffa23이 네 곳을 같이 고쳤고, 92ecd89는 contract와 SKILL.md만 다시 고쳤다. 사용자용 README와 installer-scope 문서가 남아 계약 문서와 어긋난다.

## 4. 요구사항·수락 추적 (requirements-acceptance-trace)

### 4.1 정의

- `검증됨`은 **격리 합성 프로젝트에서 entrypoint가 실행됐다**는 뜻이고 설치·운영 활성화의 증거가 아니다(`trace:3`).
- 수락 3단계: 정적/단위 → 격리 E2E(실제 `run-hook`·로컬 QMD CLI, 합성 프로젝트) → **운영 수락 "아직 미수행"**(`trace:24-28`).

### 4.2 요구사항 표 (14행)

| ID | 수락 기준 (요약) | 상태 | 남은 제한 (문서 근거) |
|---|---|---|---|
| W-01 | 원문 C/U/D 감지 → 역참조로 영향 카드 탐지 | 격리 E2E | worker당 원문 1개·카드 ≤12, distinct 판결 대기, OS/shell 변경은 다음 경계에서 검출(`trace:7`) |
| W-02 | 생성 전후 유사 카드 검색·중복/충돌 판단 후 검증 카드만 게시 | 격리 E2E | 의미 판정은 owner-only pair-hash 검토 없으면 미결, 자동 병합·teacher 호출 없음(`trace:8`) |
| W-03 | 다중 출처 중 하나 삭제 시 재생성, 마지막 삭제 시 색인 제거 | 격리 E2E + 실 Claude 합성(`trace:49`) | worker당 카드 1, 생존 출처 ≤3(`trace:9`) |
| W-04 | 중단 후 완료 단계만 재사용, uncertain 호출 비반복 | 격리 E2E | 파일 게시와 SQLite는 단일 트랜잭션 아님, 미확정 model call은 사람 판단 대기(`trace:10`) |
| R-01 | 프롬프트 → QMD ≤15 → Laya 0–3장 → 본문 | 합성 hook + 실 Laya 1회 | **미학습 base head가 관련 카드에도 0장** → 품질 증거 아님, 운영 checkpoint·비공개 프롬프트 미실행(`trace:11`) |
| R-02 | 편집 후 입력은 추가/수정분만 | 단위 | 다른 편집 도구 입력 품질 의존(`trace:12`) |
| R-03 (제안) | 선택기 실패 시 topN≤3 복귀 | 합성 | 로그 경로 없으면 진단 파일 없음(`trace:13`) |
| C-01 | 후보 모집단·설정 변경 추적, 과거 평가 혼입 차단 | 단위 | 자동 재검색·재라벨링 없음(`trace:14`) |
| C-02 | 학습/검증/평가 분리 후 로컬 학습·승격 | 합성 250문항 | 비공개 문서·실요청 학습 미수행(`trace:15`) |
| C-03 | 95% 목표 근접 시 학습 간격 조절 | 단위 | 통계적 95% 달성 선언 아님(`trace:16`) |
| S-01 | SessionStart는 opt-in 후 비동기 one-shot | 격리 E2E | embed 완료 ≠ enqueue 완료, 두 SessionStart hook 순서 보장 없음, 기존 안내는 worker 로그에만(`trace:17`) |
| S-02 | 설치·업데이트·v1 migration이 설정·DB 보존, 전환 전 corpus 증명 | 합성 + 실 QMD 2.5.3 재사용 | **신규 npm/uv coordinator 실행·실제 운영 migration 미실행**(`trace:18`) |
| S-03 | 기존 프로젝트는 명시 setup까지 자동 기능 정지, 원본·job 보존 | 합성 | 이미 구버전 코드를 적재한 세션엔 미적용, 새 plugin host 생존·운영 migration 미검증(`trace:19`) |
| H-01 | Codex setup·hook·opt-in/out·복구·rollback | 합성 + 이전 복사본(400fd06) 실호스트 | 현재 queue/setup 수정본은 실호스트 미증명(`trace:20`) |
| H-02 | Claude setup·hook 등 Codex와 동일 상태 공유 | 합성 + 실 Claude 2.1.292 합성 | 일반 설치된 plugin의 Claude setup/rollback 미검증(`trace:21`) |
| I-01 (제안) | 관리형 Laya runtime 설치/활성화/rollback 또는 증명 재사용 | 실 비활성 설치 + MPS smoke | 운영 `active.json` 미생성, 새 coordinator에서 실 uv/모델 미실행(`trace:22`) |

### 4.3 미충족·보류 항목 (표 + 보정 문단 통합)

1. **운영 수락 전체 미수행** — 비공개 원문·실요청·활성 checkpoint·전역 plugin/hook·운영 DB 미사용(`trace:28`, `trace:51`).
2. **신규 `npm ci`/`uv`를 최신 setup 조정기로 실행한 적 없음** — 과거 실설치는 "하위 계층 증거"(`trace:61`, `docs/install-update-writer-error-audit-20261006.md:33`).
3. **최신 조정기의 Laya reuse 적용/복귀 미검증** — 자동 승인 검토가 거부(`trace:61`).
4. **Codex 최신 코드의 Stop 자동 발화 미검증** — 합성 실행에서 Stop hook 흔적 없음, 직접 `run-hook topical-stop codex` 호출로만 확인(`trace:65`, `docs/operational-flow-map-20261006.md:69`, `trace:69`, `trace:73`).
5. **Hermes/Gemini 실호스트 미검증**(`trace:51`). Hermes는 `hermes_adapter/core_bridge.py`에 setup guard만 추가됐고 topical/Stop 경로는 없다.
6. **R-01 품질** — base head 0장 선택, 품질 통과 증거 아님(`trace:11`).
7. **설치된 플러그인 캐시가 수정 전 버전** — "새 독립 리뷰가 통과한 뒤 공식 명령으로 다시 갱신해야 한다"(`trace:75`).
8. **Claude 수정 후 재실행 일부 미검증** — 자동 승인 검토 거부(`trace:49`, `docs/operational-flow-map-20261006.md:59`). 단 Claude 호스트 PID 종료 후 worker 완료는 `trace:59`·`:63`에서 별도 관측됐다(W-04/S-01의 "Claude teardown 미검증"은 표 셀이 이후 문단보다 낡았다).
9. **동결 증거 manifest가 저장소에 없음** — 문서는 "별도 manifest의 최신 로그"(`trace:53`, `:69`, `:73`)를 기준으로 하라는데 `git ls-files`에 해당 파일이 없다. 증거 경로는 `/tmp/qmd-v2-empty-bootstrap-…`(`trace:59`)·Mac mini 경로뿐이다.
10. **최종 전체 회귀 수치가 문서에 고정돼 있지 않다** — `trace:51`의 "1391 pass·2 skip·0 fail"은 이후 수정 전 실행이라고 `trace:53`이 스스로 밝힌다.

## 5. 버전·매니페스트

| 파일 | main | HEAD |
|---|---|---|
| `package.json` | 0.35.0 | 0.35.2-dev.20261004 (+ `engines.node: ^22 \|\| ^24 \|\| ^26`, test 스크립트에 `QMD_SETUP_GUARD_FIXTURE=1`) |
| `plugin.json` | 0.35.0 | 0.35.2-dev.20261004 (설명에 `setup` skill 추가) |
| `plugin.yaml` | "0.35.0" | "0.35.2-dev.20261004" |
| `.claude-plugin/plugin.json` | 0.35.0 | 0.35.2-dev.20261004 (설명 갱신) |
| `.claude-plugin/marketplace.json` | 0.35.0 | 0.35.2-dev.20261004 (설명 갱신) |
| `.codex-plugin/plugin.json` | 0.35.0 | 0.35.2-dev.20261004 (설명 갱신) |
| `.agents/plugins/marketplace.json` | 0.35.0 | 0.35.2-dev.20261004 (설명 갱신) |
| `test/probe-manifest.test.mjs` | `'0.35.0'` ×2 | `'0.35.2-dev.20261004'` ×2 |

- CLAUDE.md "버전 bump 체크리스트" 8곳 전부 일치한다. 8개 JSON 매니페스트(hooks 포함)는 `JSON.parse` 통과.
- 버전 문자열에 `-dev.20261004` 접미가 붙어 있고 날짜가 커밋(10-06~10-08)보다 이르다. 릴리스 문자열로의 bump가 남아 있다.
- hooks 매니페스트(Claude·Codex 동일 구조): SessionStart가 `update` → `update-queued`(5s) + `topical-reconcile`(3s)로 갈리고, recall 22s, gate 3s, PostToolUse에 `topical-event`(5s) 추가, **`Stop` → `topical-stop`(3s) 신설**. 모든 command에 `timeout`이 붙었다.

## 6. 테스트 실행 결과 (`npm test`, 이 머신 1회)

환경: MacBook-Pro-6 (커밋 작성 호스트는 `Duleeui-Macmini.local`), Node v24.14.0, Python 3.13.12. 명령은 `package.json`의 스크립트 그대로(`QMD_SETUP_GUARD_FIXTURE=1 QMD_RECALL_LOG= node --test`). 전체 로그: (작성 시 로컬 실행 로그, 저장소 미포함).

**결과: exit 1 — tests 1410 / pass 1398 / fail 5 / skipped 7 / 104초.**

| 실패 테스트 | 첫 에러 줄 | 분류 |
|---|---|---|
| `test/context-learning-pool.test.mjs:8` eligible returned pool observes before topN/cutoff… | `assert sample['schema_version']==2 and len(sample['candidates'])==5` AssertionError | **재현(확정 원인 아래)** |
| `test/context-learning-top15.test.mjs:4` Top15 shadow expansion preserves hook bytes… | `AssertionError: 0 != 15` | **재현** |
| `test/context-learning.test.mjs:61` Claude and Codex payload capture preserves exact output… | `assert body['host']==host and len(body['candidates'])==1` AssertionError | **재현** |
| `test/context-learning-transport.test.mjs:4` bounded opt-in teacher transport uses mock CLIs… | `AssertionError: 'failed' != 'completed'` | 단독 재실행 통과 → 부하 의존(추정) |
| `test/qmd-lex-probe-routing.test.mjs:5` selected project lexical gate probes selected DB… | `assert recall.run_lex_probe(...) == 1` AssertionError | 단독 재실행 통과 → 부하 의존(추정) |

**재현 3건의 원인(확인)**: `core/context_learning/corpus.py:30-34`의 `snapshot`이 `qmd_route.project_paths(root)`로 인덱스 경로를 정하고, `core/qmd_route.py:92-93`이 pointer 없고 `XDG_CACHE_HOME`/`INDEX_PATH` 미설정 시 `~/.cache/qmd/index.sqlite`(이 머신에 1.3GB 실 인덱스 존재)를 쓴다. snapshot은 `QMD_CONFIG_DIR`(기본 `~/.config/qmd`, `core/qmd_route.py:94-95`)의 `index.yml`도 있어야 None이 아니다(`core/context_learning/corpus.py:45-47`). 두 파일이 있으면 합성 collection 문서가 0행이어도 fingerprint는 생기고 `indexed_hashes` 조회가 빗나가 `core/context_learning/seam.py:112-113`의 `candidate_index_stale`로 모든 후보가 `snapshot_failures`로 빠진다(재현 출력: `"snapshot_failures": [{"reason": "candidate_index_stale"}]`, candidates 0). `XDG_CACHE_HOME=<빈 임시 디렉터리>`로 같은 3 파일을 돌리면 **5/5 pass**. CI처럼 전역 qmd가 없는 머신에서는 snapshot이 None이라 통과한다. 즉 테스트가 **사용자 실 QMD 인덱스를 읽어 판정이 뒤집히는 비격리** 문제이고, CLAUDE.md가 `QMD_RECALL_LOG`·포트 8483에 적용한 "테스트가 제품 데이터를 건드리면 안 된다" 규칙과 같은 클래스다(쓰기는 없음, 읽기만).

**skip 7개**: 라이브 데몬 1, 기존 Laya runtime opt-in 1, 전용 QMD/Node 부재 1, 그리고 **"isolated offline QMD runtime unavailable" 4개**(publish·multisource refresh·similarity·two-card). 이 4개 fixture는 Mac mini 절대경로 `/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529`를 기본값으로 하드코딩한다(`test/fixtures/wiki-topical-publish-e2e.py:15-21`). 이 머신에 그 경로가 없다. v2 핵심 E2E(실 QMD 게시·재생성)는 기본 스위트에서 Mac mini에서만 돈다.

결론: 커밋 본문의 "1369 pass·0 fail"(2962427)과 `trace:51`의 "1391 pass·2 skip·0 fail"은 **작성 호스트 전용 수치**다. 머신 의존성이 양방향이다: 전역 qmd가 있으면 실패, 오프라인 snapshot이 없으면 skip.

## 7. 릴리스 준비 상태 (`docs/release.md` 체크리스트 기준)

| 항목 (`docs/release.md:5-12`) | 상태 |
|---|---|
| 1. 버전이 모든 매니페스트와 일치 | 일치. 단 `-dev.20261004` 접미, 릴리스 버전 미정 |
| 2. `npm test` + `git diff --check` | `git diff --check main...HEAD` 통과. `npm test`는 이 머신 5 fail(위 6절) |
| 3. JSON 매니페스트 parse | 8개 통과 |
| 4. 새 임시 프로젝트에서 Claude·Codex·Hermes 마켓플레이스 설치 후 session start·recall·opt-in gate 확인 | **미수행**. trace 문서 운영 수락 미수행(`trace:28`), Hermes 실호스트 미검증(`trace:51`), Codex 최신 Stop 미검증(`trace:65`) |
| 5. wiki compile 광고 시 일회 안내·비활성 경로 확인 | 문서 근거 없음(이 브랜치 문서가 다루지 않음) |
| 6. GitHub Release 노트(호환성·사용자 변화·업그레이드·알려진 한계) | 미작성. 업그레이드 노트에 들어가야 할 S-03(기존 프로젝트 자동 기능 정지 → setup 필요)이 README에 없다 |

브랜치 이름의 "pre-release-validation"이 가리키는 **릴리스 전 남은 검증**(문서 근거):
1. 최신 수정본 독립 리뷰 → 공식 명령으로 플러그인 캐시 재갱신 → 사용자 hook 신뢰(`trace:69`, `:73`, `:75`).
2. 그 위에서 Codex Stop 자동 발화 확인(`trace:65`, `docs/operational-flow-map-20261006.md:69`).
3. 최신 setup 조정기로 신규 `npm ci`·`uv` 실행, Laya prepare/activate/rollback(`trace:18`, `:22`, `:61`).
4. 일반 설치된 플러그인의 Claude host setup/rollback(`trace:21`).
5. Hermes/Gemini 실호스트(`trace:51`).
6. 운영 프로젝트 pointer 전환·비공개 자료 품질(`trace:28`, `:43`, `:51`).
7. 최종 동결 manifest와 전체 회귀 로그(저장소 밖, `trace:53`, `:73`).
8. hook 효과(SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop) 검토 후 대상 프로젝트 명시 선택(`docs/qmd-installer-scope-20261006.md:95-102`).

## 8. 위험·미해결

- **CLAUDE.md 미갱신**: 29k줄 변경 동안 에이전트 지침 파일이 그대로다. `CLAUDE.md:185` "전체 훅 deadline 없음"이 `core/hook_budget.py:7-9`와 정면 모순. 다음 작업자가 이 지침으로 예산·구조를 판단하면 틀린다.
- **비격리 테스트 3건**: 전역 `~/.cache/qmd/index.sqlite`를 읽는다(6절). 개발자 머신 `npm test`가 상시 빨갛다. 커밋 게이트 신호가 깨진다.
- **머신 고정 fixture**: v2 실 QMD E2E 4개가 Mac mini 절대경로에 묶여 다른 머신·CI에서 조용히 skip(`test/fixtures/wiki-topical-publish-e2e.py:15-16` 등).
- **제품 코드 안의 테스트 우회**: `core/setup_guard.py:21`이 `QMD_SETUP_GUARD_FIXTURE=1`이면 무조건 `ready`를 반환하고, `npm test` 전체가 이 값으로 돈다. 즉 기본 스위트는 업그레이드 가드를 끈 채 기존 테스트를 통과시킨다(가드 자체는 `test/setup-upgrade-guard.test.mjs`가 값을 비우고 검증). 운영 env에 새면 가드가 무력화된다(AGENTS.md:40 경고만 있음).
- **Hermes fail-closed**: `hermes_adapter/core_bridge.py`의 `_setup_allowed`는 guard 실행 실패·2초 timeout 시 False → hook 무동작. Hermes 실호스트 미검증(`trace:51`)이라 오탐 시 Hermes에서 플러그인이 조용히 꺼질 수 있다(추정).
- **문서 모순**: Node 26(3.4절 — README·installer-scope vs contract·SKILL.md), trace 표 셀(W-04/S-01 "Claude 미검증")이 이후 문단(`trace:59`, `:63`)보다 낡음.
- **README 업그레이드 안내 부재**: 기존 사용자는 업그레이드 직후 recall·인덱싱이 멈추고 SessionStart 1회 안내만 받는다(`docs/operational-flow-map-20261006.md:17`). README·릴리스 노트에 없다.
