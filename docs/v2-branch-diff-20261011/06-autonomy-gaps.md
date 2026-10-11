# 자율성 갭: 사람 없이 돌아야 하는 경로에서 사람을 요구하는 지점

## 기준

setup(설치·opt-in·마이그레이션)을 제외한 hook, recall, wiki 생성·검수·정리, 학습은 사람이 개입하지 않고 끝나야 한다. 사람이 하던 판정은 agent 또는 AI가 맡는다. 이 기준으로 브랜치 코드(92ecd89)를 대조한 결과를 정리한다.

근거 라벨은 [README](README.md)와 같다.

## 이미 AI가 처리하는 것

| 판정 | 담당 | 근거 |
|---|---|---|
| 카드 내용이 원문과 맞는가 (per-source) | `wiki_verify_worker`. 다른 엔진으로 원문 대조. pass → `verified`, fail·inconclusive → 삭제와 억제 마커 | `core/wiki_verify_worker.py` · 코드 확인 |
| 카드 내용이 원문과 맞는가 (topical) | backend 검수. claim별 인용 span 대조 결과를 불변 attestation으로 남긴다 | `core/wiki_topical_backend.py:245-310` · 코드 확인 |
| 두 카드가 같은 사실인가 (per-source) | `wiki_dedup_judge`. LLM 본문 대 본문 판정, 예산·교차 엔진·억제 마커 포함 | `core/wiki_dedup_judge.py` · 코드 확인 |
| 중복 카드 병합 | `wiki-dedup-resolver` agent | `agents/wiki-dedup-resolver.md` · 코드 확인 |

## 사람이 판정하는 지점과 AI 대체 방안

| # | 지점 | 현재 동작 | AI 대체에 쓸 기존 수단 | 남는 일 | 근거 |
|---|---|---|---|---|---|
| A1 | topical 유사성 게이트 | 사람이 `topical-similarity-review.json`에 쌍마다 `distinct`를 기록해야 게시된다. 두 번째 카드부터 해당한다. 다른 fresh 카드가 8장을 넘으면 게시가 막힌다 | `wiki_dedup_judge` | 게이트에 judge를 연결한다. `duplicate`면 병합하거나 폐기하고, `distinct`면 게시한다. 결과는 쌍 해시 억제 마커로 남긴다 | `core/wiki_topical_similarity.py:26-55,118-155`, `core/wiki_topical_publish.py:77-98` · 코드 확인 |
| A2 | topical 검수 fail·inconclusive | `created_backend_pass_required`, `pending_review`로 정지한다 | compile의 `onFail`/`onInconclusive: delete`, `verify-skipped.jsonl` 억제 마커 | 같은 규칙으로 staged 카드를 폐기하고, 원문 hash 기준으로 억제한다 | `core/wiki_topical_create.py:183-184` · 코드 확인 |
| A3 | topical uncertain 호출 | 중단된 호출은 `uncertain`으로 남고 재호출이 금지돼 정지한다 | verify의 후보 단위 cooldown, 다음 run에서 다음 엔진으로 넘어가는 구조 | 시간 기반 재시도나 다른 엔진 전환을 넣는다. 이중 과금 상한은 attempt 예산을 유지한다 | `core/wiki_topical_backend.py:146-152`, `core/wiki_topical_refresh.py:107-109` · 코드 확인 |
| A4 | topical 생성 예산 초과 | `10×(생성시도+카드수) > maxEstimatedCents`이면 `estimated_budget_exceeded`로 정지한다. 같은 원문이면 매번 같은 결과가 나온다 | compile의 `cards_per_source_cap`(초과분 skip, requeue 없음) | 카드 수를 상한으로 잘라 진행하거나 그 원문만 억제한다 | `core/wiki_topical_create.py:170-171` · 코드 확인 |
| A5 | dedup 큐 정리 | `wiki-dedup-resolver`는 SessionStart 알림이 스폰하는데, 알림이 worker 로그로만 가서 큐가 소비되지 않는다 | `wiki-dedup-resolver` agent | 스폰 지시를 host 턴에 전달하는 채널을 복구하거나 worker가 직접 실행한다 | `core/update_hook_queue.py:150-152,197-202`, `core/update.sh:1651-1659` · 코드 확인 |
| A6 | 원문 소실 카드 | `repoint`/`dismiss`는 사용자 확인 뒤에 적용된다. 그동안 원문 없는 verified 카드가 계속 주입된다 | `wiki_source_repair` 후보 제안, `wiki_verify_worker` | 후보로 재지정한 뒤 검수를 돌린다. pass면 재지정을 확정하고, fail이면 퇴역시킨다. 후보가 없으면 일정 기간 뒤 퇴역시킨다 | `core/wiki_source_missing.py`, `core/wiki_source_repair.py` · 코드 확인 |
| A7 | merge-needed 충돌 | 명시 targetPath 충돌은 `merge-needed.jsonl`에 기록만 된다. 후보 내용은 반영되지 않는다 | dedup resolver 패턴 | resolver가 이 큐도 처리한다 | `core/wiki_compile.py` `explicit_target_agrees` · 코드 확인 |
| A8 | Laya 학습 정답 | 정답 저장에 검수자 ID가 필수다. teacher 라벨은 provisional이라 정답이 되지 않는다. corpus가 바뀌면 옛 sample은 사람 재검토 큐로 간다. 정답이 train ≥100 · val ≥50 · eval ≥50이 되기 전에는 학습이 시작되지 않는다 | teacher(claude/codex), `budgeted-second-opinion`, 생성·검토 모델 분리 | 두 엔진의 판정이 일치하면 정답으로 승격하는 정책을 만든다. corpus 변경 시에는 AI가 다시 라벨링한다. 승격 게이트(비퇴행)는 유지한다 | `core/context_learning/selection_gold.py:1-5`, `core/context_learning/local_cycle.py:278-282`, `core/context_learning/corpus.py:64-96` · 코드 확인 |

setup 영역의 v1→v2 매핑(`--reviewed-v2-mappings`)도 사람 검토를 요구한다. 기준상 제외 대상이지만, AI 매칭과 검수로 대체할 수 있다.

## 하나가 막히면 전체가 멈추는 지점

AI가 판정하지 못한 항목은 격리하고 다음 작업으로 넘어가야 한다. 다음 두 곳은 항목 하나가 전체를 멈춘다.

| # | 지점 | 동작 | 근거 |
|---|---|---|---|
| B1 | topical 작업 큐 | `auto_refresh_pending`은 남아 있는 create/refresh journal을 항상 먼저 이어서 처리하고, `_drain_auto`는 첫 `pending_review`에서 반환한다. journal은 완료되거나 그 원문이 바뀔 때만 지워진다. 그래서 A1~A4 중 하나에 걸린 원문이 이후의 모든 원문·stale 카드 처리를 막는다 | `core/wiki_topical_refresh.py:418-430`, `core/topical_hook_queue.py:139-142`, `core/wiki_topical_create.py:98,106,205,215` · 코드 확인 |
| B2 | index_worker | 큐 엔트리 하나가 setup 미완 프로젝트이면 run 전체가 exit 1로 끝나고 claim을 유지한다. 그러면 모든 프로젝트의 편집 후 인덱싱이 멈춘다. optout으로도 풀리지 않는다 | `backend/index_worker.sh:117-125` · 코드 확인 + 실측 |

대체 방향: 막힌 항목을 별도 원장으로 옮기고(원인·원문 hash 포함), 원문이 바뀌면 다시 큐에 넣는다. 큐는 다음 항목으로 진행한다.

## 자가 복구가 없는 장애

| # | 지점 | 동작 | 근거 |
|---|---|---|---|
| C1 | 관리형 QMD runtime | Node나 qmd 패키지가 바뀌면 `select()`가 실패한다. PATH 폴백이 없어 모든 프로젝트의 recall·update·데몬이 멈춘다. `QMD_BIN`으로도 우회되지 않고, `qmd_runtime.rollback`을 호출하는 곳이 없다 | `core/qmd_route.py:34-35`, `core/qmd_runtime.py:184` · 코드 확인 |
| C2 | 프로젝트 pointer 손상 | recall은 `project_index_invalid`로 무출력이 되고, update는 ABORT한다. 복구 경로는 setup 재실행뿐이다 | `core/recall.py:2149-2153`, `core/update.sh:1117-1124` · 코드 확인 |
| C3 | topical 원문 경로 | auto 정책은 임의 sourceRoots를 받지만 `safe_source`는 `sources/` 아래만 허용한다. 다른 경로이면 wake마다 같은 실패를 반복한다 | `core/wiki_topical.py:157`, `core/topical_hook_queue.py:309` · 코드 확인 |
| C4 | 임베딩 모델 교체 | shadow DB가 준비 시점과 다른 모델 지문을 거부하므로 setup을 다시 해야 할 가능성이 높다 | `core/runtime_update.py` `select_runtime` · 추정 |

대체 방향: C1은 직전 세대로 자동 롤백하고 알린다. C2는 이전 pointer(`previous`)로 되돌린다. C3은 정책 검증 단계에서 거부해 job을 만들지 않는다. C4는 새 모델로 shadow 세대를 자동 재생성한다.

## 판정 실패를 아무도 모르는 지점

사람이 보지 않으므로 AI 판정이 실패하면 agent에게 전달돼야 한다. 다음 경로가 끊겨 있다.

| # | 지점 | 동작 | 근거 |
|---|---|---|---|
| D1 | SessionStart 알림 | `notice_once` 16개 키(daemon-down, 색인 큐 적체, compile 설정 오류, unregister 실패, orphan 회수 실패, wiki-dedup, source-missing 등)가 worker 로그로만 간다. TTL marker는 소진된다 | `core/update_hook_queue.py:150-152,197-202` · 코드 확인 |
| D2 | topical 보류 | `pending_review`는 `topical-hook-status.json`에만 기록된다. update.sh와 recall에서의 참조는 0건이다 | grep 결과 · 코드 확인 |
| D3 | setup_guard 차단 | 진단 로그를 한 줄도 남기지 않아 "hook 미실행"과 구분되지 않는다 | `hooks/run-hook:71-73` · 실측 |
| D4 | 로컬 env 파싱 실패 | 모든 hook이 로그 없이 exit 0으로 끝난다 | `hooks/run-hook:43-55` · 코드 확인 |

대체 방향: worker 결과를 상태 파일에 쌓는다. 다음 UserPromptSubmit이나 SessionStart의 동기 출력이 그 파일을 읽어 agent 지시로 전달하고, 전달한 뒤에 marker를 소진한다. A5(dedup resolver 스폰)도 이 채널에 의존한다.

## 플랫폼 제약

| # | 지점 | 동작 | 근거 |
|---|---|---|---|
| E1 | 비 macOS arm64 (92ecd89) | `setup_guard`는 프로젝트 pointer를 요구한다. pointer를 만드는 `activate_shadow_index`의 유일한 호출부는 `supported_platform()`(darwin + arm64) 게이트 뒤에 있다. 그래서 Linux·Intel Mac의 opt-in 프로젝트는 영구히 `setup_required`이고 모든 hook이 무출력이다. README가 안내하는 "전역 qmd 수동 설치 경로"로는 guard가 풀리지 않는다 | `core/install_update.py:58-59,796,875`, `core/setup_guard.py` · 코드 추론 |
| E2 | 모든 플랫폼 (ff7e0db) | 이후 커밋 `e17927b`에서 guard 판정이 `qmd_route.project_paths(..., isolated=True)`로 바뀌었다. 운영 경로에서는 이 함수가 프로젝트 pointer를 보지 않고 플러그인 공용 store(`qmd_shared_store.select`, `~/Library/Application Support/qmd-auto-context/shared-qmd`)만 고른다. 이 store를 만드는 `stage_empty`/`activate_empty`의 호출부는 테스트 fixture(`test/fixtures/prepare-shared-qmd-store.py`)뿐이고, setup 경로(`setup_entry`, `install_update`)는 이 store를 만들지 않는다. 격리 HOME에서 모던 설정 프로젝트의 `status`는 `plugin_qmd_store_not_selected`다. 즉 이 시점에는 macOS arm64를 포함한 모든 플랫폼에서 opt-in 프로젝트를 ready로 만들 제품 경로가 없다 | `core/setup_guard.py:49-59`, `core/qmd_route.py:66-78`, `core/qmd_shared_store.py:25-26,382-435` · 코드 확인 + 실측 |
| E3 | Laya runtime | hash lock이 `laya-macos-arm64-py312.lock` 하나뿐이다. 다른 플랫폼에는 설치 경로가 없다 | `core/context_learning/locks/` · 코드 확인 |
| E4 | CI | CI는 ubuntu에서만 돌고 관리형 경로는 darwin arm64에서만 동작한다. 그래서 지원 플랫폼 경로를 CI가 실행하지 않는다 | `.github/workflows/test.yml` · 코드 확인 |

대체 방향: 지원 플랫폼 밖에서는 관리형 설치 없이 전역 qmd를 재사용해 store나 pointer를 만드는 경로를 연다. 그 경로가 없으면 플러그인 설치 시점에 미지원임을 알리고 hook을 등록하지 않는다. 공용 store 생성은 setup 경로에 연결하고, 그 경로를 CI 매트릭스에 넣는다.

## 우선순위

1. **E2 공용 store 생성 경로.** 현재 HEAD에서는 어떤 플랫폼도 setup을 끝낼 수 없다. 다른 항목보다 먼저다.
2. **D1 알림 채널 복구.** A5, A6과 모든 실패 표면화의 전제다.
3. **B1 topical 격리.** A1~A4가 남아 있어도 파이프라인 전체가 멈추지 않게 한다.
4. **A1·A2 topical 판정을 AI로.** 이미 있는 `wiki_dedup_judge`와 verify 삭제 규칙을 재사용한다.
5. **B2 index_worker skip-and-preserve.**
6. **C1·C2 runtime·pointer 자동 롤백.**
7. **A6·A7 source repair와 merge-needed 자동화.** 검수를 게이트로 두면 잘못된 재지정은 fail로 걸러진다.
8. **E1·E3 비 arm64 지원.** 전역 qmd 재사용 경로를 만들거나 미지원을 명시한다.
9. **A8 Laya 정답 정책.** AI 합의로 정답을 승격하는 정책을 만들거나, liveSelection을 실험 기능으로 분리한다.
