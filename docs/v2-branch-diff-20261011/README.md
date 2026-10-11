# feat/v2-pre-release-validation ↔ main 변경 정리

- 비교 범위: `main`(0f277e2) → `feat/v2-pre-release-validation`(92ecd89). 브랜치는 main 위에 커밋 8개를 얹었고 main 쪽 신규 커밋은 없다.
- 변경량: 219 파일, +29,218 / −732.
- 작성 방식: 영역별 서브에이전트가 코드를 읽고 보고서를 썼다. 이어서 적대적 리뷰어 2명이 핵심 주장을 코드·격리 실행으로 다시 판정했다. 판정이 바뀐 문장은 영역 문서에 반영돼 있다.
- 근거 라벨:
  - **실측**: 이 머신에서 실행해 확인했다.
  - **코드 확인**: 해당 줄을 직접 읽었다.
  - **코드 추론**: 경로를 따라가 결론을 냈다.
  - **문서 근거**: 브랜치 docs 서술에 기댄다.
  - **추정**: 근거가 위 넷에 미치지 못한다.

| 문서 | 범위 |
|---|---|
| [01-hooks-backend-recall.md](01-hooks-backend-recall.md) | hook manifest, `run-hook`, `hook_budget`, SessionStart 큐, dirty queue claim/ack, `index_worker.sh`, recall·posttool, `sqlite_read`, daemon |
| [02-install-setup-runtime.md](02-install-setup-runtime.md) | `setup_guard`, setup skill → `setup_entry` → `install_update`, 관리형 QMD runtime, `qmd_route`, 프로젝트 shadow index, 플랫폼·CI |
| [03-topical-wiki.md](03-topical-wiki.md) | topical v2 wiki: Stop → reconcile → create/refresh → 검수 → 유사성 게이트 → 게시 → QMD sync, 턴 예산 |
| [04-context-learning.md](04-context-learning.md) | `contextLearning` capture·liveSelection·autoCycle, Laya 학습·승격, teacher 예산, Laya runtime 설치 |
| [05-timeline-docs-release.md](05-timeline-docs-release.md) | 커밋 타임라인, 브랜치 docs 요지, 요구사항 추적, 버전, `npm test` 결과, 릴리스 전 남은 검증 |
| [06-autonomy-gaps.md](06-autonomy-gaps.md) | setup 외 경로에서 사람 판정을 요구하는 지점, 전체 정지 지점, 자가 복구·표면화 공백과 AI 대체 방안 |

## 한눈에

| 축 | main | 이 브랜치 |
|---|---|---|
| hook 진입 | opt-in 설정만 있으면 core 실행 | 모든 action 앞에 `setup_guard`. pointer가 없거나 레거시 설정이면 exit 0 무출력 |
| hook 이벤트 | SessionStart·UserPromptSubmit·PreToolUse·PostToolUse | 여기에 `Stop` 추가. SessionStart는 `update-queued` + `topical-reconcile`, PostToolUse에 `topical-event` |
| 시간 예산 | 전체 hook deadline 없음(구조적 최악 ≈14s) | recall·posttool에 18s 절대 deadline(`QMD_HOOK_TOTAL_SECONDS` 2~20) + host timeout 22s |
| SessionStart | `update.sh` 동기 실행, 알림이 같은 턴에 출력 | durable job + detached worker. 알림은 worker 로그로만 감 |
| 검색 백엔드 | 전역 데몬 :8483 + `~/.cache/qmd/index.sqlite` | pointer 선택 프로젝트는 프로젝트 shadow DB에 `qmd query` CLI. 데몬은 pointer 없는 경로에만 남음 |
| QMD 설치 | 플러그인은 설치하지 않음 | hook은 여전히 설치하지 않음. setup skill에서 명시 승인하면 관리형 QMD 2.5.3을 lock 고정 설치(macOS arm64) |
| dirty queue | 2컬럼, snapshot → truncate → requeue | 3컬럼 가능(`project_root`), claim/ack, 실패 시 보존 후 재시도 |
| wiki 생성 | per-source compile | per-source compile 유지 + topical v2 병행(Stop 턴 단위, 사람 `distinct` 게이트) |
| 카드 선택 | QMD 순위 + 필터 | 여기에 opt-in Laya 선택(15장 → 0~3장) |

## 기능별 흐름

### Hook 공통 진입

```
host hook → run-hook
  ├ sandbox/headless → exit 0 (topical-stop만 '{}')
  ├ 플러그인 루트 .qmd-local-runtime.env 파싱 (실패 → 전 action exit 0)
  ├ action = update-queued → update_hook_queue.py enqueue (guard를 내부에서 판정)
  └ 그 외 → setup_guard hook|hook-root → ready 아니면 exit 0 (진단 로그 줄 없음)
             → recall · posttool · index · compile · gate · topical-*
```

### SessionStart

```
update-queued (5s)
  ├ setup_required → setup 안내 1회, job 없음
  └ ready → <key>.job.json (O_EXCL) → update.sh --resolve-only(≤1s, opt-in 미결정 안내)
            → detached worker → (pointer 없을 때만 ensure/warm/rotate) → update.sh (≤120s)
            → stdout은 <key>.log[:2048]
            → update.sh --worker: add/update/embed → (autoCycle) → dedup scan → orphan reclaim
topical-reconcile (3s) → topical_hook_queue reconcile  (순서 보장 없음)
```

### UserPromptSubmit (recall)

```
setup_guard → hook_budget.arm(18s, SIGALRM) → qmd_route.project_paths
  ├ pointer 선택 → qmd query CLI (DF 좁히기·shadow 없음)
  └ pointer 없음 → 데몬 /query (+DF 좁히기)
→ 필터·순위 폴백·lex 게이트 → (liveSelection) Laya 0~3장 → stdout 주입 → (capture) seam.observe
```

### PostToolUse → 인덱싱

```
index → dirty queue append(+fsync, 3컬럼) → kick-index
index_worker: claim(.claim.json) → 엔트리별 setup_guard·route → add/update/embed(+reload) → ack
              실패: claim·원본 보존 (busy 75 / 실패 1)
```

### Setup (명시 요청)

```
inspect · check-update · plan (읽기 전용)
→ prepare (journal, 비활성 세대: QMD · Laya · shadow index)
→ activate (별도 요청: 재대조 → QMD pointer → route → settings 복사 → index pointer → Laya pointer → 데몬 reload)
→ rollback (laya → index → qmd baseline 복원. 첫 활성화 롤백 = pointer 삭제 = guard 재차단)
```

### Topical wiki (Stop)

```
Stop → topical_hook_queue.enqueue → detached worker
→ reconcile.start_batch (원문 전체 scan → stale 투영)
→ [반복] budget.reserve → 작업 1건 (refresh 이어하기 / stale refresh / 새 원문 create)
         → 생성 → 카드별 검수 → 사람 distinct 게이트 → publish(verified stamp) → 프로젝트 QMD sync
         → budget.finish
턴당 작업 ≤12, 추정 ≤640센트
```

### Context learning

```
recall(capture) → learning.sqlite3 (프롬프트·후보 평문)
→ 사람 검수(pair label, 요청 gold 0~3) → 불변 manifest·family split
→ SessionStart(autoCycle, setup ready) → run_due_cycle
→ train≥100 / val≥50 / eval≥50 → head-only 학습 → 비퇴행 승격 → active-checkpoint.json
recall(liveSelection) → QMD 15장 → 전제 확인 → live_select.choose → 0~3장 (실패 시 QMD topN≤3)
```

## 먼저 볼 위험

| # | 위험 | 근거 | 라벨 |
|---|---|---|---|
| 1 | 업그레이드하면 main 시절 opt-in 프로젝트 전부가 recall·편집 hook·worker·manual skill 무출력으로 멈춘다. 진단 로그도 남지 않는다. 이 저장소도 `project_index_not_selected`다 | `hooks/run-hook:71-73`, `core/setup_guard.py:48-50` | 실측 |
| 2 | `index_worker.sh`는 배치 안 엔트리 하나가 setup 미완이면 run 전체를 exit 1로 끝내고 claim을 유지한다. 그래서 모든 프로젝트의 편집 후 인덱싱이 멈춘다. optout으로도 풀리지 않는다 | `backend/index_worker.sh:117-125`, `core/dirty_queue_claim.py:104-108` | 코드 확인 + 실측 |
| 3 | SessionStart 지시형 알림(notice_once 16키, dedup resolver 지시)이 모델에 닿지 않는다. TTL marker는 그대로 소진된다 | `core/update_hook_queue.py:150-152,197-202` | 코드 확인 |
| 4 | macOS arm64 밖에서는 pointer를 만들 경로가 없다. 그래서 opt-in 프로젝트가 영구 `setup_required`다 | `core/install_update.py:796,875` | 코드 추론 |
| 5 | 관리형 runtime 검증 실패(Node 교체 등)는 PATH 폴백 없이 전역으로 퍼진다. `QMD_BIN`으로도 우회되지 않고 복구 명령도 없다 | `core/qmd_route.py:34-35`, `core/qmd_runtime.py:184` | 코드 확인 |
| 6 | topical 원문이 `sources/` 밖이면 auto 정책은 이를 받아들이지만 매 wake마다 같은 실패를 반복한다 | `core/wiki_topical.py:157`, `core/topical_hook_queue.py:309` | 코드 확인 |
| 7 | topical 두 번째 카드부터 사람 `distinct` 검토가 필요하다. fresh 카드가 8장을 넘으면 게시가 막힌다(테스트 없음) | `core/wiki_topical_publish.py:77-98` | 코드 확인 |
| 8 | `label-request --enable-teacher`는 teacher 예산 원장을 거치지 않는 유료 경로다 | `core/context_learning_cli.py:120-123` | 코드 확인 |
| 9 | pointer 경로 recall은 DF 좁히기와 lex distrust 판정을 생략한다. 그래서 일반 term 하나만 남는 잡담에도 게이트가 열린다 | `core/recall.py:2134-2135,2243,2745` | 코드 확인(빈도 미측정) |
| 10 | `npm test` 3건이 전역 `~/.cache/qmd/index.sqlite`·`~/.config/qmd/index.yml`을 읽어 개발 머신에서 실패한다. 실 QMD E2E 4건은 Mac mini 절대 경로가 없으면 skip된다 | `core/context_learning/corpus.py:30-47` | 실측 |
| 11 | CLAUDE.md diff가 0줄이다. "전체 hook deadline 없음", "dirty 큐 2컬럼", "SessionStart 지시형 알림"이 코드와 모순된다 | `CLAUDE.md:185` ↔ `core/hook_budget.py:7-9` | 코드 확인 |
| 12 | 운영 수락이 미수행이다. 남은 항목: Codex 실호스트 Stop 자동 실행, 최신 조정기로 npm·uv 신규 설치, Claude 일반 설치 setup/rollback, Hermes 실호스트, 동결 manifest | `docs/requirements-acceptance-trace-20261006.md:28,51,65` | 문서 근거 |

## 검증 상태

- `npm test`(이 머신 1회, Node 24.14.0): 1410 tests, 1398 pass, 5 fail, 7 skip.
  - 3건은 위 #10이다. `XDG_CACHE_HOME`을 격리하면 통과한다(실측).
  - 2건은 단독 재실행에서 통과한다(부하 의존, 추정).
- 매니페스트 8곳의 버전은 `0.35.2-dev.20261004`로 일치한다. 릴리스 bump는 남아 있다.
- 적대적 리뷰에서 바뀐 판정:
  - 로그 회전은 "상실"이 아니다. 설정 없는 폴더 세션으로 범위가 줄었다.
  - Hermes setup 안내 부재는 채널이 없어서가 아니다. 안내를 싣는 구현이 없다.
  - topical 검수 엔진은 Codex 고정이 아니다. `crossEngine: prefer`가 다른 엔진을 먼저 쓴다.
  - Gemini는 출하된 hook 등록이 없어 수동 연결 시에만 해당한다.
  - notice 키 수는 16이다.
  - 커밋 표의 파일 수·update.sh 수치를 정정했다.
