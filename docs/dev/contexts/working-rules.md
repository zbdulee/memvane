# 공통 개발 규칙

MemVane 색인 기반 자동 컨텍스트 주입 훅을 **Claude Code · Codex · Hermes Agent · Antigravity(Gemini)** 에서 동작시키는 플러그인 MemVane(플러그인 id `memvane`, marketplace `memvane-marketplace`). 사용자 안내는 `README.md` 참고. 이 문서는 코드 작업 시 알아야 할 구조·명령·함정에 집중한다.

사용자용 문서와 응답에서는 설치 후 운영 인터페이스를 **자연어 요청 또는 노출된 skill 호출** 중심으로 안내한다. `core/`·`skills/`의 shell command는 유지보수/디버깅 인터페이스이므로, 사용자가 명시적으로 요청한 경우나 개발자 문서가 아닌 이상 README 같은 public docs에 직접 실행 흐름으로 노출하지 않는다.

개발 시에는 **Claude Code · Codex · Hermes Agent** 세 host의 동작성을 모두 고려한다. 공통 로직은 `core/`에 두고, host별 adapter/hook 차이(Claude/Codex command hook, Hermes Python plugin 및 observer-only `post_tool_call`)가 깨지지 않는지 함께 확인한다.
## 개발 협업 방식과 Claude 활용

이 절은 프로젝트 개발 작업의 협업 방식이다. 기존 권한·보안·자원 규칙을 완화하지 않으며, 사용자 승인 범위를 넘는 실행은 별도로 확인한다. 현재 사용자의 다른 에이전트 토큰 예산이 부족하므로 Claude를 우선 활용한다.

- 구현, 결함 수정, 테스트 작성, 문서 정리, 근본 원인 분석, 설계·코드 리뷰는 대부분 Claude Opus에 우선 맡긴다. 단순한 한 파일 수정에 과도한 위임을 만들지 않는다.
- 주 에이전트는 작업 범위와 의존성을 정하고 각 Claude 호출에 맡길 범위를 배정한다. 결과의 조율·통합·검토, 필수 저장소 가드 검증, 릴리스 관리, 사용자 보고는 주 에이전트가 직접 책임진다.
- 새 서브에이전트 생성이나 자동 에이전트 분산을 권장하지 않는다. 이미 진행 중인 작업은 결과와 변경을 안전하게 회수하고 파일 충돌 없이 정리한다.
- 각 Claude 호출에는 변경 가능한 파일·모듈, 기대 입력·출력, 완료 조건과 금지 범위를 명시한다. Claude는 위임받은 정확한 범위와 저장소 가드·기존 파일시스템·네트워크·보안 승인 안에서만 파일을 수정하거나 명령을 실행한다. 주 에이전트에게 거부된 접근이나 권한을 Claude를 통해 우회하지 않는다.
- 좁은 Claude 호출은 서로 다른 파일을 다룰 때만 병행한다. 같은 파일을 동시에 수정하지 않는다. 공용 가드 테스트·백업·해시·실제 모델 작업은 위 공용 개발 가드와 자원 규칙을 따른다(일반 focused 검사는 최대 3개 동시, 그 밖의 작업은 단독). 독립 리뷰도 모든 검사 종료 후 순서대로 진행한다. 같은 대상에 대한 중복 리뷰, 이미 끝난 기초 리뷰의 반복, 토큰 소진 자체를 목표로 한 호출은 피한다.
- 외부 모델 호출 전 실행 환경이 지원하는 모델과 옵션을 확인하고, 확인되지 않은 모델 이름을 추정하지 않는다. 실제 모델과 실행 결과를 기록하고 `modelUsage`가 제공되면 함께 남긴다. 관측 대기가 끝나거나 응답이 늦어지면 종료로 간주하지 말고 같은 실행의 프로세스·로그·출력과 최근 진행 여부를 확인한다. 정상 진행 중이면 임의의 시간만을 이유로 중단하지 않고 계속 기다린다. 지연이나 오류가 관측되면 원인을 조사해 사용자에게 알리고 실행 제한 연장, 대기 또는 복구 등 확인된 상태에 맞게 조치한다. 실행 제한은 도구나 개발 가드가 실제로 요구할 때만 적용하고, 시작 전 제한과 연장 가능 여부를 확인한다. 실제 종료 뒤에는 기존 상태와 산출물을 확인하고 중복 호출을 피한다. 개발 가드의 자원 제한과 안전한 프로세스 정리 절차는 유지한다.
- 로그인·인증 실패와 샌드박스 DNS·네트워크 관측 실패를 구분한다. 승인된 정상 실행 경로만 사용하고 권한 거부나 개발 가드를 우회하지 않는다. Claude가 불가능하면 정확한 사유와 대안을 보고하며 사용한 척하지 않는다.
- 외부 모델에는 작업에 필요한 코드와 합성 자료만 전달한다. 비밀·인증 정보·원고·운영 DB 내용은 보내지 않는다. Claude 결과는 그대로 채택하지 않고 코드와 계약을 직접 검토하며, 구체적 지적은 재현한 뒤 최종 코드에 맞는 가드 검사를 수행한다.
- 구현 상태, 검증 근거, 실패한 검사와 그 출력, 남은 차단 항목을 구분해 정직하게 보고한다.
선택된 Laya 위키 검색에서 binding store가 있는 경로는 마지막 완료·게시된 위키 색인만 조회한다. 검색 시 원문 현재성을 재검증하지 않는다. 원문 내용 해시 비교, 변경 큐 등록, 위키 생성·갱신과 문서별 원자적 색인 게시는 인덱스/compile owner가 맡는다. binding 없는 구형 Laya 경로의 live freshness 설명은 이 선택 경로의 계약으로 해석하지 않는다.

## 명령

로컬 `.development-hold.json`이 true인 동안 일반 검사·백업/해시·모델 작업·운영 활성화를 재개하지 않는다. 해당 보류를 해제하는 사용자 지시가 있어야 재개하며, 실행 허가는 자원 기준이나 소유 프로세스 정리 가드를 우회하는 권한이 아니다. 세션별 승인 기록과 기계 상태는 이 공개 문서 대신 로컬 작업 기록에 남긴다.

테스트·백업·해시 같은 무거운 개발 작업은 사용자 전역 `scripts/development_guard.py` 잠금을 거친다. 일반 focused 검사(`npm run test:focused`, `--kind test`)는 worktree/에이전트를 합쳐 최대 3개까지 동시에 돈다. 전체 검사(`npm test`, `--exclusive`)·백업·해시·실제 모델 검사·리뷰·calibration은 단독으로 돌고, 이들이 도는 동안 일반 검사도 시작하지 않는다. 자리가 없으면 exit 75로 거절되며 기다렸다 다시 시도한다. 각 검사의 파일 병렬도는 1이며, 내부 자식 프로세스까지 합산한다. 독립 리뷰도 모든 검사 종료 후 순서대로 진행한다. 보호 장치 자체의 작은 합성 단위 검사(`test:guard-unit`)만 bootstrap 예외다. 실제 프로세스·모델 검증은 OS 관측이 가능한 승인된 일반 호스트에서 가드를 통해 실행한다. 샌드박스의 관측 거부는 명시적으로 보고하고 가드를 끄지 않는다. 런타임 조율은 `docs/runtime-resource-coordination.md`, 보호 장치 기준·체크포인트·샘플링 한계는 `docs/development-resource-safety.md`를 읽는다.

```bash
npm run test:guard-unit                    # 작은 합성 보호 장치 단위 검사만 순차 실행
npm test                                   # guard 단독(--exclusive) + 파일 병렬 1
npm run test:focused -- test/laya-runtime-provision.test.mjs # 같은 guard, 최대 3개 동시, 병렬 1
npm run dev:guard -- --kind backup -- <명령> # 같은 잠금을 단독으로
npm run dev:guard -- --kind hash -- <명령>   # 단독
npm run dev:guard -- --kind model-test -- node --test <실제 모델 시험> # 학습용 자원 제한 유지

bash skills/query/scripts/query.sh <프로젝트> "<질문>"  # 선택된 Laya 경로의 수동 recall
bash skills/update/scripts/update.sh <프로젝트>          # 선택된 MemVane 색인의 full-scan sync wake 큐잉
bash skills/wiki-compile/scripts/wiki-compile.sh <프로젝트> < compact.json  # compact durable summary → wiki_compile.py

# ROOT = 설치된 플러그인 루트(예: 이 버전의 Claude plugin cache 경로). 아래 동작은 --project 없이 실행한다.
python3 -B "$ROOT/core/setup_entry.py" index-runtime-status                    # Laya embed runtime 상태(read-only)
python3 -B "$ROOT/core/setup_entry.py" provision-index-runtime --approve-index-runtime-download [--provider-model-file <gguf>]  # 새 머신의 runtime 구성(npm registry·모델 다운로드)
python3 -B "$ROOT/core/setup_entry.py" gc-index-runtime [--approve-index-runtime-gc]  # 지울 세대 목록 / 승인 시 삭제
```

개발 중에는 변경한 로직과 영향 범위의 focused 검사를 우선 실행한다. 전체 검사는 변경과 focused 검사가 준비된 뒤 머지 직전 최종 커밋을 대상으로 한 번 실행한다. 검사는 해당 커밋의 별도 worktree에서 비동기로 시작할 수 있으며, 실행 중에는 그 worktree를 수정하지 않고 다른 독립 작업을 진행할 수 있다. 단독 가드를 쓰는 전체 검사 중에는 다른 검사·리뷰를 시작하지 않는다. 검사 결과를 확인하고 필요한 독립 리뷰와 후속 조치가 끝나기 전에는 머지하지 않는다. 실패를 고치는 등 제품 코드나 검사에 영향을 주는 변경으로 최종 커밋이 달라지면 새 커밋에 필요한 전체 검증을 다시 실행한다. 동일 커밋에서 필수 Python 버전과 허용된 분할 검사 각각의 결과를 확인한다.
일반 모의·회귀·통합 검사(`--kind test`, CI 포함), 원격 리뷰(`--kind review`), 해시·스테이징(`--kind hash`)은 학습용 OS 압력·CPU/RSS·headroom·swap·load 제한을 적용하지 않는다. 전역 단독 실행, 파일 병렬도 1, 유한 timeout, 프로세스 수와 소유 식별·정리는 유지한다. 실제 모델을 로딩/학습하는 개발 검사(`--kind model-test`)와 백업(`--kind backup`)은 자원 제한을 유지한다. 명시적 calibration envelope도 항상 자원 검사를 유지한다. 제품 런타임의 학습 admission은 이 구분과 무관하게 그대로 적용된다.
테스트/설치를 격리 검증할 때 쓰는 env 가드 (테스트 코드가 이걸로 부작용을 막는다):
- `QMD_SETUP_GUARD_FIXTURE=1` — `npm test`가 켜는 테스트 전용 스위치(`QMD_SANDBOX`와 같은 자리에서 읽힌다). 켜져 있으면 runtime wake를 시작하지 않고, 실제 계정 홈의 Laya embed runtime을 읽거나 쓰지 않는다. 운영 hook에서는 설정하지 말 것.
- `QMD_FORCE_DEAD_PIDS=<pid>[,<pid>…]` — 고아 `*.claimed.*` 회수 테스트 전용. `_is_pid_alive`가 **목록에 정확히 일치하는 pid만** 죽은 것으로 취급한다(전체 무력화가 아니다 — 그래야 실환경에 새도 피해가 그 pid로 한정된다). 테스트가 "확실히 죽은 pid"를 구하려고 방금 종료한 자식의 pid를 쓰면 바쁜 머신에서 재할당되어 flaky해지므로, 가정 대신 이 주입을 쓴다. **실환경에서 절대 설정하지 말 것** — 살아 있는 워커를 죽은 것으로 판정해 진행 중인 배치를 회수하면 같은 잡이 두 번 처리되어 유료 호출이 이중 과금된다
- `QMD_SANDBOX` / `GEMINI_SANDBOX` / `--sandbox` — 디스패처·코어 즉시 무출력 종료

테스트 작성 시 주의: **새 벽시계 단정 전에 "그 회귀를 개수·상태로 표현할 수 있는가"를 먼저 묻는다** — 대개 가능하고 그러면 부하와 무관해진다(예산이 곱해지면 프로브·질의 **개수**가 늘고, 30초 대기는 하네스 timeout이 `res.signal`을 남기고, 백그라운드로 넘겼어야 할 일을 기다렸으면 완료 **마커가 존재**한다). 실측으로 `notEqual(status, 0)`이 공허하게 통과하던 자리가 있었다 — `spawnSync`는 timeout 시 `status: null`·`signal: 'SIGTERM'`을 주므로 버그가 있는 세계에서도 통과했고 벽시계가 유일한 검출자였다. **폴링 대기에 `execFileSync('sleep', …)`를 쓰지 말 것** — 폴 1회당 프로세스 스폰이라 부하가 곧 폴링 비용이고, 관측된 간헐 실패가 전부 이 형태에서 났다. `waitUntil`(동기) / `waitUntilAsync`(자식 프로세스를 다루는 테스트 — 동기 sleep은 SIGCHLD 수확을 막아 좀비를 "살아 있음"으로 읽는다)를 쓴다. **테스트가 제품 데이터를 건드리면 안 된다** — 이벤트 로그는 검증된 install state root(0700, symlink 없는 경로)에만 쓰므로, 테스트는 임시 HOME과 fixture state root(`realpath`로 푼 임시 디렉터리)를 쓰고 소유자의 라이브 로그에 닿지 않는다. 테스트 작성 시 주의: `execFileSync`는 반드시 `encoding:'utf8'`을 줄 것 (없으면 Buffer 반환 → `.trim()` 에러). 병렬 실행에서 `core/__pycache__`가 spurious 실패를 유발할 수 있어 `.gitignore` 처리돼 있다. **node 테스트 프로세스 안에 http 서버를 띄워 훅에 응답시키려 하지 말 것** — `execFileSync`가 자기 event loop를 막아 서버가 요청을 처리하지 못한다. stub 서버가 필요하면 Python driver(스레드 서버 + subprocess)에서 띄운다.
**테스트가 제품 데이터를 건드리면 안 된다** — 이벤트 로그는 검증된 install state root(0700, symlink 없는 경로)에만 쓰므로, 테스트는 임시 HOME과 fixture state root(`realpath`로 푼 임시 디렉터리)를 쓰고 소유자의 라이브 로그에 닿지 않는다. 테스트 작성 시 주의: `execFileSync`는 반드시 `encoding:'utf8'`을 줄 것 (없으면 Buffer 반환 → `.trim()` 에러). 병렬 실행에서 `core/__pycache__`가 spurious 실패를 유발할 수 있어 `.gitignore` 처리돼 있다.
## 아키텍처 (큰 그림)

**2층 구조: 플랫폼 무관 코어 1벌 + 얇은 host adapter.** 인덱스·임베딩 런타임은 Laya가 소유한다(아래 host route, `laya_resident_service.py`).

```
core/      ← 모든 로직. 플랫폼/도메인 무관 core. stdin {prompt,cwd} → stdout 훅 JSON
hooks/     ← Claude/Codex/Gemini run-hook 디스패처 + hooks.json/hooks-codex.json. route 분류 후 선택된 route만 코어로 패스스루
hermes_adapter/ ← Hermes Agent plugin hook adapter(pre_llm_call/on_session_start/pre_tool_call/post_tool_call) → core로 패스스루
```

편집 후 자동 인덱싱: 선택된 route의 PostToolUse `index` action이 Laya full-scan sync owner를 깨운다(`laya_host_response.py`). 성공한 agent Bash(`file-change-event`)도 같은 full-scan wake를 낸다. 실제 인덱싱은 hook 밖의 durable worker가 한다.

host route의 분류·실패 동작은 `host-route.md`를 읽는다.

영속 파일·디렉터리 identity는 canonical 경로와 inode 및 기록된 내용·metadata 필드로 대조한다. macOS APFS의 `st_dev`는 부팅마다 달라질 수 있어 프로세스 간 대조에서 제외한다. 저장된 dev 값·endpoint ID·manifest 바이트와 SHA·scopeId seed는 유지한다. 서로 다른 부팅에 봉인된 두 reader endpoint record의 `*_node`(project·state·저장소)도 inode로 대조한다(`laya_journal_reader.records_agree`: reader 전환·rollback CAS, 선택 검증, reader lease의 journal 이미지 전진, rollback이 다시 발급한 v6 reader의 저장소 대조). 같은 프로세스의 fd/path·전후 관측은 dev도 대조하고, 원문 탐색의 장치 경계는 열린 root fd의 현재 dev를 기준으로 한다.
- **쓰기 반환값을 확인할지 말지는 "조용한 실패가 무엇을 잃는가"로 가른다.** 전부 확인하는 것은 과하고(노이즈이며 기존 판단을 뒤집는다) 전부 무시하는 것은 이 저장소에서 네 번 반복된 버그 클래스다. 기준 셋:
  1. **확인한다 — 작업이 유실되거나 상태가 오염되는 경로.** `stamp_verification`(유료 검수 1회를 쓰고 카드가 `generated`로 남아 영구 비가시), `record_verify_deletion`(원장 없이 삭제 = 설명 불가능한 소실, fail-closed로 삭제 자체를 막는다), `patch_frontmatter_fields`의 status 리셋(새 본문에 `verified`가 남는 stale 검증).
  2. **확인한다 — 거짓 성공 보고.** 그 쓰기가 연산의 **유일한 효과**일 때. 원장 행 하나가 전부인 기록 연산은 실패하면 아무 일도 안 했는데 성공을 출력하게 된다.
  3. **무시한다 — 실패 방향이 안전하고 다음 회차가 재시도하는 경로.** 원자적 쓰기라 원본이 온전하고 손해가 재처리뿐인 곳: `trim_jsonl`의 로그 압축, `compact_manifest`의 `write_jsonl_atomic`, 그리고 `write_state_atomic` 계열(스냅샷·커서). **`write_state_atomic`은 애초에 반환값이 없고 실패를 예외로 던진다** — "삼킨 반환값"이 아니다. 커서 미전진이 원장 중복을 만들지 않는 이유는 커서가 아니라 `needs_record`의 상태 검사다.
  각 호출부에 **분류 근거를 한 줄 주석으로** 남긴다 — 다음 사람이 이 판단을 다시 하지 않게, 그리고 "전부 고쳐라" 스윕이 3번 항목을 건드리지 않게.
## 운영상 함정 (디버깅 시 참고)

- **python 하한은 3.9다 — 새 `core/*.py`에 PEP 604(`X | None`) annotation을 쓰면 `from __future__ import annotations`를 함께 넣어라.** 훅은 `bare python3`을 실행하고(`hooks/run-hook`) macOS는 `/usr/bin/python3`으로 3.9를 탑재한다. annotation은 정의 시점에 평가되므로 future import가 없으면 **모듈 import 자체가 TypeError로 죽어** 훅이 exit 1(host의 "hook failed")이 되고 플러그인이 통째로 무동작한다. 이 클래스는 개발 머신 python이 3.10+면 **전혀 보이지 않는다** — 실측으로 시스템 python 3.9에서 764개 중 365개가 실패하는 동안 3.13에서는 0 실패였다. 이 클래스를 잡는 전용 테스트는 없으므로 리뷰에서 확인하고, `match`/`except*`처럼 future import로 구제되지 않는 3.10+ 전용 구문도 쓰지 않는다. 시스템 python으로 `npm test`를 돌리면 남는 실패는 제품 결함이 아니라 환경 산물이다: PyYAML 대조 테스트(시스템 python에 PyYAML 부재 — 테스트가 가용성을 의도적으로 단정한다).
- **`queryTimeout`(기본 **5초**)의 정의는 `core/config.py`의 `DEFAULT_CONFIG["queryTimeout"]` 하나다.** `context_learning/selection_read.py`가 학습 선택 읽기의 deadline으로 쓴다(0.2~30초 클램프). hook 프로세스 전체 deadline은 `core/hook_budget.py`가 정한다(기본 18초, `QMD_HOOK_TOTAL_SECONDS`로 2~20초). **"3초 예산"을 기본값이나 판정 기준으로 인용하지 말 것** — 근거 없는 수치다. 상세는 `docs/settings.md`의 "blocking 훅의 시간 예산".
- **macOS 시스템 python 3.9의 Apple libsqlite3는 `-wal`·`-shm` 없이 남은 WAL 헤더 DB를 `?mode=ro`로 열지 못한다.** Homebrew python이 닫은 DB를 hook이 읽는 경로가 여기에 걸린다. 기존 DB를 읽기 전용으로 여는 코드는 `laya_store_primitives._query_only_uri`(darwin + 3.9에서 `mode=rw`와 `PRAGMA query_only`)를 쓴다. 새 읽기 경로에 `?mode=ro`를 직접 쓰지 말 것.
- **빈 출력은 정상 동작일 수 있다.** 미온보딩 프로젝트(`legacy` route)·sandbox·결과 0건은 모두 의도적으로 무출력 종료한다. 빈 출력 ≠ 버그. 선택된 route의 모든 hook 호출은 설치 이벤트 로그 `<install state root>/events.jsonl`에 한 줄을 남긴다(`core/event_log.py`, `laya_host_response._event_scope`). recall 줄은 `outcome`(`served`/`empty`/`refused`), 최종 `reason`(`selected`/`no_results_after_filter`/`no_candidates`/refusal code), 경로상의 진단 `notes`(`laya_vector_*`·`laya_selector_*`·`laya_recall_proof_retry`·`laya_wiki_rows_filtered` 등), 실제 주입한 카드 `cards[{path, sha}]`, `selection_fallback`·`vector_fallback`·`degraded`(`bundle_drift`·`worktree`·`reader_endpoint`)를 담고(schema3 reader recall은 `store_format`·`reader_endpoint`도 담는다), 프롬프트는 `{sha256, chars}`로만 남긴다. 설치 단위 파일이라 `install`·`session` 필드로 귀속된다. 쓰기는 best-effort라 hook을 실패시키지 않고 stdout(모델 컨텍스트)엔 나가지 않는다. 읽기는 `memvane:logs` 스킬(`skills/logs/scripts/logs.py`)로 한다.
