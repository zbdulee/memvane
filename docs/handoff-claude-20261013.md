# Claude 인수인계 — QMD auto-context v2 (2026-10-13)

## 목적과 범위

개발 브랜치의 QMD auto-context v2를 Mac mini의 실제 학습 이력을 보존하며 검증한다. 코드는 플랫폼 공통 `core/`, Claude·Codex의 얇은 command hook, Hermes adapter, 관리형 QMD/Laya 런타임으로 구성된다. 학습은 프로젝트 ID와 검색 범위를 분리한 공용 저장소에 기록하고, 후보 모델은 검증·승격 전까지 staged 상태로 둔다. 구조와 계약은 [학습 개발 문서](continual-learning.md), [v2 차이 분석](v2-branch-diff-20261011/README.md), [설치·업데이트 계약](install-update-contract-20261006.md)을 참고한다.

이 문서는 **재개 지점**이다. 새로운 집필 100화 계획은 취소되었다. 실제 학습 동작의 최소 검증은 완료됐지만 성능 향상은 입증되지 않았다. 사용자는 Codex 주간 잔여량이 29%였을 때 추가 작업을 멈췄고, 이제 Claude에서 이어갈 계획이다. 이 문서 작성·커밋을 위해 데이터 이전, 모델 적용, 새 학습, 외부 모델 호출은 하지 않았다.

## 코드와 검증 상태

- 저장소: `https://github.com/zbdulee/qmd-auto-context.git`; 개발 브랜치: `feat/v2-pre-release-validation`.
- 이 인수인계의 코드 기준: `faf67742b4542bf9301c68ef18969970473bd923` (`0.35.2-dev.20261012-fantasy-pilot-v50-rc40-late-gold-wake`). 문서 작성 전 GitHub 원격 브랜치 SHA가 이 값과 일치했다. Mac mini의 설치된 Codex·Claude 플러그인 캐시는 2026-10-12 체크포인트에서 rc40 해시 일치로 기록됐다. 기존 Claude 세션은 재시작해야 새 플러그인을 읽을 수 있다.
- 기준 커밋의 CI `38211395694`는 통과했다고 체크포인트에 기록되어 있다. 이번 문서 커밋은 코드 테스트를 다시 실행하지 않았다. 문서 커밋의 CI 결과는 별도로 확인해야 한다.
- 최소 실험은 실제 편집 사례 10개에서 **train gold 5 / 독립 validation gold 5**로 완료됐다. `cohort-6728087fed5166053046`의 `candidate.artifact`는 파일 해시가 확인된 **staged·미적용** 상태다. 해당 5개 validation 질의에서 old/new의 선택 **문서 ID 집합 정확 일치**는 각각 **1/5 (20%)**였다. 이는 문서별 정확도도 성능 개선도 아니다.
- 정식 품질 게이트는 **train 100, train family 5, validation 50, validation family 5, empty-answer validation 10**이다. 최소 진단의 관측치는 각각 5, 5, 5, 5, 0이므로 정식 통과로 해석하지 않는다. 운영 자료 전체에 대한 품질이나 일반화, MacBook 신규 설치, 실제 공용 경로 이전·연결은 미검증이다.

## 학습 데이터와 중단 지점

- 최근 로컬 체크포인트: `~/Documents/Codex/2026-10-12/task-2/qmd-rc40-candidate/PAUSE-CHECKPOINT.md`. 이후 사용량 중단 기록: `~/Documents/Codex/2026-10-13/task/QMD-SHARED-LEARNING-MIGRATION-PAUSE.md`. 각 문서의 상태 수치는 **기록 시점의 관측**이며 재개 시 실제 파일과 DB로 재검증한다. 비공개 데이터 자체는 Git에 넣지 않는다.
- 현재 원본: `~/work/novel/.qmd-fantasy-shared-20261008/`; DB: `shared-learning.sqlite3`. 공용 목표: `~/Library/Application Support/qmd-auto-context/learning/`. 이전은 **아직 시작하지 않았다**. 기존 프로젝트 ID 기록은 `413531c6-ee3f-49c1-915a-fa248bc87a70`이다. 실제 registry에서 다시 확인한다.
- 마지막 shutdown 검사(2026-10-12 10:03 UTC): 열린 gold receipt 0, admitted pending 0, topical in-flight 0, hook job 0, worker 종료, 이전부터 보류된 K42 source 1건. 새 편집이 일어나면 hook이 다시 동작할 수 있으므로 중복 worker를 시작하기 전 상태를 확인한다. 이 인수인계에서는 실시간 큐를 재조회하지 않았다.
- 후보 artifact와 이전 시점의 모든 학습 이력·정답지·영속 큐·receipt·모델 참조·필요 산출물을 보존한다. 활성 실험 모델 포인터는 체크포인트 시점에 없었고 `modelApplied=false`였다. 오래된 prepared generation `g-2e0a373031ee42008566ae83bf113f83`은 최신 원본 검증 없이 활성화하지 않는다.
- 이전 전 일관된 SQLite 백업과 파일 inventory/해시를 확보하고, 프로젝트 ID·source version·레코드 수·내용 해시·receipt·모델 참조 및 경로를 대조한다. 대상에서 재실행 중복 방지, 프로젝트별 검색 범위, 런타임의 실제 읽기/쓰기, 되돌리기를 확인한 뒤 전환한다. 원본은 삭제하지 않는다. 되돌릴 수 있는 백업과 이전 포인터 상태를 유지한다. 경로 변경이 권한을 넓힌다고 가정하지 않는다.

## 다음 작업 순서

1. **Mac mini 공용 학습 저장소 이전.** 실제 코드의 `history_migration`/`history_activation`/`history_lifecycle`와 `storage_layout` 계약을 확인하고 최신 원본을 백업·대조한다. 새 prepared generation을 검증해 필요한 경우에만 활성화한다. 데이터 보존, 프로젝트 경계, 영속 큐, 런타임 연결, rollback을 확인한다. 모델 적용이나 새 학습은 하지 않는다.
2. **MacBook 신규 설치와 모델 준비, Codex·Claude 검증.** Mac mini의 이전을 끝낸 후 별도 환경에서 설치·마이그레이션·실제 host hook과 검색을 검증한다. 설치된 모델 경로와 권한은 그 환경에서 확인한다. 검증용 승인과 제품 setup의 권한 게이트를 각각 지킨다.
3. **agy 최종 리뷰.** 공식 payload adapter가 없는 현재 AGY 자동 posttool/index/compile은 보류 상태다. 최종 코드·테스트·문서 리뷰에서 이 제한을 다시 확인한다.

남은 제한: 최신 coordinator를 통한 신규 npm/uv 설치와 운영 마이그레이션, MacBook 실설치, 여러 host의 실제 Stop/teardown 동작, 정식 학습 품질 게이트, 성능 향상은 아직 확인되지 않았다. 기존 분석 문서는 시점이 달라 일부 표의 검증 상태가 후속 실측보다 낡을 수 있으므로 위 체크포인트와 실제 상태를 우선한다. **worktree에서의 프로젝트·플러그인 경로 해석과 `git pull` 뒤 변경 감지/재조정 대응은 TODO**로 남겨 두고, 별도 설계·회귀 검증 전까지 보장하지 않는다. 권한 차단은 우회하지 말고 정확한 작업·대상을 기록한다. main 병합, 릴리스, 모델 적용, 외부 데이터 업로드는 이 인수인계의 범위 밖이다.

## 2026-10-13 Claude 재개 결과 — 1단계 진행 상황

브랜치 `feat/v2-claude-handoff-20261013`(기준 `89075e0`)에서 진행했다. 실제 원본 DB, 공용 학습 저장소, 플러그인 캐시, 모델은 변경하지 않았다. 아래 원본 불변은 작업 전후 파일별 SHA-256 inventory로 확인했다.

**실측 상태 (2026-10-13)**
- 실행 중인 worker나 DB를 연 프로세스가 없었다. gold receipt 176건은 전부 committed(열린 것 0), deferred capture 11건은 전부 completed다. staged `cohort-6728087fed5166053046/candidate.artifact`의 SHA-256 `afb0c18e…`는 기록과 일치한다. registry의 프로젝트 ID `413531c6-…` → `/Users/dulee/work/novel`도 기록과 일치한다.
- 이전 원본은 **두 개**다. shared `…/.qmd-fantasy-shared-20261008/shared-learning.sqlite3`와 private `…/.qmd-fantasy-private-20261008/<sha>/learning.sqlite3`이며, 둘 다 `journal_mode=delete`다.
- `g-2e0a373031ee42008566ae83bf113f83`(10-09 준비)은 낡았다. 이후 shared cases 63→203, private samples 111→429, corpus_review_queue 3,304→49,054로 늘었고 테이블 7개가 새로 생겼다. **활성화하지 말 것.** 새로 `prepare`해야 한다.

**백업**: `~/Documents/Codex/2026-10-13/task/qmd-learning-backup-20261013/`
- 두 저장소 디렉터리 전체 사본(바이트 동일 확인)
- SQLite 일관성 백업 2개(`integrity_check` ok)
- `novel/.auto-context/settings.json`
- 파일별 해시 inventory와 `key-hashes.sha256`

**코드 수정 (커밋 3개)**
- `aa77a03`: `prepare`가 BLOB 컬럼(`source_card_versions`, `topical_worker_versions`) 때문에 `TypeError`로 실패하던 문제를 고쳤다. 원본에 해당 행이 1건과 29건 있었다. BLOB이 없는 테이블의 digest는 그대로라 기존 manifest도 검증된다. 다른 프로젝트의 카드 등록 행은 `card_registration_scope_mismatch`로 거부한다.
- `37eb191`: `activate()`에 봉인(`sealing`) 단계를 넣었다.
  - fence를 건 뒤 레거시 DB 두 개를 `0400`으로 바꾼다. 이유: 옛 host 코드(v12/v44 캐시)가 lock이나 pointer 확인 없이 `CREATE TABLE IF NOT EXISTS`를 실행한다. private DB에는 아직 `teacher_jobs` 등 5개 테이블이 없어서, fence가 걸리지 않은 테이블에 쓰기가 생기고 그 쓰기는 유실된다.
  - lsof로 쓰기 fd를 확인하고, schema digest와 inventory를 재검증한다. 실패하면 원래 권한으로 복원하고 fence를 제거한다.
  - recover/rollback은 봉인 상태를 검증한다.
  - 새 오류 코드: `history_legacy_writer_open`, `history_legacy_writer_probe_unavailable`, `history_legacy_schema_changed`, `history_legacy_unsealed`.
- `bf1e218`: `runtime_authorization`의 레거시 원장 직접 읽기 2곳을 `read_database`로 옮겼다. `isolated_train`과 `derived_split`은 호출부가 활성 history를 거부하고 파일이 SHA로 고정돼 있어 그대로 뒀다.
- 실제 원본으로 임시 홈(`home=`)에서 `prepare`를 돌렸고 `verify_prepared`도 통과했다(cases 203, samples 429, `sources_current: True`).
- 전체 `npm test`: 1,478건 중 1,472건 통과. 실패 4건은 시스템 python에 PyYAML이 없어서 생기는 환경 산물이다.

**실제 전환까지 남은 게이트 (소유자 결정 필요)**
1. `activate()`의 실제 홈 무조건 거부(`history_legacy_ddl_unfenced`)를 명시적 승인 인자로 바꾸는 코드 변경. 아직 하지 않았다.
2. 이 빌드를 Claude·Codex 플러그인 캐시 양쪽에 설치. rc40과 코드가 달라 host 일치 검사가 거부한다.
3. v12/v44 또는 rc5~rc39로 시작한 모든 세션이 재시작됐다는 소유자 확인. 봉인은 레거시 쓰기만 막고, 옛 코드가 새 저장소에 쓰는 것은 막지 못한다.
4. 백업을 갱신한 뒤 새 `prepare` → `verify_prepared` → lifecycle request·host ack → `activate`. rollback은 새 저장소에 쓰기가 생기기 전까지만 가능하다.

**알려진 낮은 등급 사항**
- 활성화된 뒤 외부 작업으로 레거시 파일 권한이 바뀌면 recover/rollback이 `history_legacy_unsealed`로 거부한다. 해결은 손으로 다시 `chmod 0400` 하는 것이다.
- 레거시 DB가 WAL 모드이면 봉인 뒤 검증이 실패로 닫힌다. 현재 DB는 `delete` 모드라 해당하지 않는다.
- 봉인 이전 형식의 sealed-phase 상태 파일은 읽히지 않는다. 실제 홈 활성화가 불가능했으므로 실사용 상태는 없다.

## 2026-10-13 12:14 KST — Mac mini 공용 학습 저장소 활성화 완료

**현재 상태 (소유자가 `stat`·상태 파일로 확인)**
- 활성 generation: `g-a511b279646649d0bcea5c309aa4a571`. 위치는 `~/Library/Application Support/qmd-auto-context/learning/migrations/`.
- 포인터 `history-active.json`이 기록돼 있고, 상태 파일은 `phase: active`다.
- 레거시 원본 두 개는 `0400`으로 봉인됐다. 원래 권한은 상태 파일 `seal.modes`에 `[0600, 0644]`로 기록돼 있다.
- 런타임 읽기·쓰기는 포인터를 따라 새 history로 간다.
- activate 이후의 쓰기 거부 확인과 `recover()` 검증은 세션 권한 분류기에 막혀 실행하지 못했다. 다음 세션에서 읽기 전용으로 확인할 것: `selected_database` 경로, history 행 수, fence trigger 수.

**활성화 전 검증**
- 준비: 11:25 백업 직후 prepare를 실행했다.
- 대조: 백업 사본과 행 단위로 비교해 shared 21개 테이블 2,283행, private 10개 테이블 53,740행이 모두 일치했다.
- 그 밖의 확인 결과:
  - `quick_check` ok, 외래키 위반 0
  - gold receipt 176건 committed, deferred capture 11건 completed
  - staged `cohort-6728087f…` artifact 해시 `afb0c18e…` 일치
- 설치: rc42 = `ada16fc`(`0.35.2-dev.20261013-claude-handoff-v52-rc42-claude-load-root`). CI `38340033880`이 Node 22/24/26 모두 통과했다.
- 두 host의 ack는 새 headless 세션의 실제 SessionStart hook이 기록했다(nonce `9b46b0da`, 지문 `ab04634b`). 기록된 경로는 다음과 같다.
  - Claude: `…/qmd-v44-local-marketplace/plugins/qmd-auto-context-v52-rc42-claude-load-root`. Claude는 디렉터리 marketplace 플러그인을 원본 위치에서 그대로 실행한다. 이 사실을 반영한 것이 `5991efa`다.
  - Codex: `~/.codex/plugins/cache/qmd-shared-v21-local/…/rc42`
- 옛 세션 처리:
  - Codex 17217·98014는 소유자가 종료했다.
  - Claude 29479는 활성화 전에 이미 종료돼 있었다.
  - rc40으로 남은 세션은 activate를 실행한 세션(27808) 하나이고, activate 직후 종료한다.
  - 이 조건을 소유자가 알고 승인해서 `old_host_sessions_restarted=true`로 넘겼다.

**복구**
- 새 history에 기록이 생기기 전에만 `rollback()`으로 깨끗하게 되돌릴 수 있다. 포인터를 지우고, 기록된 원래 권한을 복원하고, fence를 제거한다.
- 기록이 생긴 뒤에는 감사된 역이전이 따로 필요하고, 코드에는 아직 구현돼 있지 않다.
- 백업: `~/Documents/Codex/2026-10-13/task/qmd-learning-backup-20261013-112502/`. 원본 디렉터리 전체, SQLite 백업 2개, 활성화 전 학습 루트가 들어 있다.
- 설치 전 설정 백업: `~/Documents/Codex/2026-10-13/task/qmd-install-rc41-backup/`, `…/qmd-install-rc42-backup/`

**남은 일**
1. 실행 권한 rebind: rc41부터 기존 실행 권한(gold, local_train, quality_evaluate)은 막힌 상태로 닫혀 있다. 이 권한은 rc40 adapter 경로에 묶여 있었다. 다시 쓰려면 소유자가 승인한 해시로 `runtime_authorization.rebind`를 실행해야 한다.
2. transport 잠금 수정(`6b860b4`)은 반영됐다. 원인과 재현 조건은 해당 커밋 메시지에 있다.
3. 2단계(MacBook 신규 설치)와 3단계(agy 리뷰)는 그대로 남아 있다.

## 2026-10-13 12:23 KST — 활성화 후 읽기 전용 확인

12:14 절이 다음 세션에 넘긴 확인 세 가지를 마쳤다. 모든 DB는 `mode=ro`로만 열었고 파일·권한·상태 파일은 바꾸지 않았다.

**결과**
- `selected_database`: shared·private 레거시 경로 둘 다 `migrations/g-a511b279646649d0bcea5c309aa4a571/history.sqlite3`로 해석된다. 포인터와 `history-state.json`(`phase: active`, `seal.modes` `[384, 420]` = `[0600, 0644]`)이 일치한다.
- history 행 수: 테이블 36개, 56,658행, `quick_check` ok. cases 203, samples 429, corpus_review_queue 49,054, gold_attempt_receipts 176, deferred_edit_captures 11.
- fence trigger 수: shared 63개(테이블 22개 중 `sqlite_sequence`를 뺀 21개 × INSERT·UPDATE·DELETE), private 30개(10 × 3). history DB에는 trigger가 없다.
- 봉인: 레거시 DB 두 개 모두 `0400`, `journal_mode=delete`.
- 쓰기 여부: `history.sqlite3`의 SHA-256 `7ae01bd7…`이 `prepared.json`의 `history_sha256`과 같고 mtime도 prepare 시점(11:25)이다. **활성화 뒤 새 history에 쓰기가 아직 없으므로 `rollback()`으로 깨끗하게 되돌릴 수 있다.** 첫 쓰기가 생기면 이 창은 닫힌다.

**하지 않은 것**
- 쓰기 거부 확인과 `recover()` 검증은 쓰기를 동반하므로 이번 확인 범위에서 뺐다.

**남은 일 (변화 없음)**
1. 실행 권한 rebind(gold, local_train, quality_evaluate): 읽기 전용으로 계획을 계산했다(파일 변경 없음 확인). 소유자 승인 후 실행한다.
   - 현재 체인 끝: `67fe4b2ae48150843a60739853f475e6ceef5e745321b3cd69eeb2fec3da73a8` (adapter rc40, 만료 2026-10-18 17:15 KST)
   - rc42 adapter `laya_adapter.py` SHA-256: `1509e6833ba8b2ca23d6e7811173d268c09b16005a2b3d9f9f2838c9ae23bf5e` (Claude 캐시·Codex 캐시·Claude 디렉터리 marketplace 세 곳 동일, nlink 1)
   - 새 proposal: `fdfda5c508b32931b6e01a1dc4f273d4b749c8a44da1e2262894fb829d80c68c`. `_same_execution_scope` 통과 — adapter 경로 외 범위(operations, goldModel `gpt-6.1-sol`, modelDir, cohort, 만료)는 그대로다.
   - rebind는 shared 루트의 `runtime-authorization-updates/`에 receipt 파일 하나만 추가하고 원래 grant는 그대로 둔다. history DB에는 쓰지 않으므로 **rebind 자체는 rollback 창을 닫지 않는다.** 창을 닫는 것은 그 뒤 실제 gold·학습 작업이 history에 쓰는 순간이다.
2. 2단계(MacBook 신규 설치)와 3단계(agy 리뷰)는 그대로 남아 있다.

## 2026-10-13 12:36 KST — 실행 권한 rebind 완료, Claude 디렉터리 로드 경로는 여전히 거부

- 소유자 승인으로 `runtime_authorization.rebind`를 실행했다. 결과 `rebound`, `67fe4b2a…` → `fdfda5c508b32931b6e01a1dc4f273d4b749c8a44da1e2262894fb829d80c68c`. receipt `runtime-authorization-updates/fdfda5c5….json`이 추가됐고 원래 grant 파일은 그대로다. `active()`가 새 해시를 돌려주고 두 adapter 행이 rc42를 가리킨다.
- history DB 해시는 rebind 전후 동일하다. rollback 창은 아직 열려 있다.
- 설치된 코드 위치별 `require_operation`(gold, local_train, quality_evaluate):
  - Codex 캐시 `~/.codex/plugins/cache/qmd-shared-v21-local/…/rc42`: 세 작업 모두 통과
  - Claude 캐시 `~/.claude/plugins/cache/qmd-shared-v44-local/…/rc42`: 세 작업 모두 통과
  - **Claude 디렉터리 marketplace `~/Documents/Codex/2026-10-09/task/qmd-v44-local-marketplace/plugins/qmd-auto-context-v52-rc42-claude-load-root`: 세 작업 모두 `runtime_authorization_adapter_invalid`**
- 원인: `runtime_authorization._approved_adapter_paths`와 `_adapter_rows`가 Claude adapter를 `~/.claude/plugins/cache/qmd-shared-v44-local/…`로 고정한다. 그런데 Claude는 디렉터리 marketplace 플러그인을 원본 위치에서 실행한다(`5991efa`가 history ack 쪽에서만 반영한 사실). 그래서 실제 Claude 세션에서 도는 코드는 자기 adapter가 승인 목록에 없어 실패로 닫힌다. 안전 방향이므로 데이터 위험은 없고, Claude 세션에서 gold·학습·평가가 돌지 않을 뿐이다. Codex 세션은 정상이다.
- 고치려면 `5991efa`와 같은 방식(Claude 자체 설치 기록으로 실제 로드 폴더를 찾아 승인)으로 runtime_authorization을 바꿔야 한다. 그러면 proposal 해시가 바뀌므로 새 rc 설치와 또 한 번의 rebind 승인이 필요하다. 아직 하지 않았다.

## 2026-10-13 KST — rc43: Claude 실행 폴더 권한 수정, 설치, rebind 완료

- 수정 `58640d5`: `runtime_authorization._installed_adapters`가 Claude 실행 폴더의 adapter도 받는다.
  - 인정 조건: `history_activation._claude_load_roots`가 Claude 자체 설치 기록으로 그 폴더를 지목해야 한다. 이 함수는 캐시 사본과의 바이트 일치도 함께 요구한다. 폴더의 `.claude-plugin/plugin.json` 버전이 `REVIEWED_PLUGIN_VERSION`과 같아야 한다.
  - grant에는 계속 캐시 경로만 기록되므로 proposal 해시와 기록된 범위는 바뀌지 않는다.
  - Claude 기록을 읽지 못하면 이 추가 경로만 닫힌다.
  - 테스트 `test/context-learning-runtime-authorization-claude-directory.test.mjs`. 세 가드를 각각 빼면 실패하는 것을 확인했다.
- 버전 `0905639` = rc43 `0.35.2-dev.20261013-claude-handoff-v53-rc43-claude-grant-root`.
  - 로컬 `npm test`: 1,482건 중 1,476건 통과. 실패 4건은 PyYAML 환경 산물이다.
  - CI `38343282593`·`38343279142` 통과.
- 설치: Claude·Codex marketplace에 각각 581개 파일을 넣었고 커밋 대비 차이는 0개다. 두 host가 rc43으로 업데이트됐다.
  - `_installed_hosts_compatible`이 통과했다. Claude는 `…/qmd-v44-local-marketplace/plugins/qmd-auto-context-v53-rc43-claude-grant-root`로 잡힌다.
  - 설치 전 설정 백업은 `~/Documents/Codex/2026-10-13/task/qmd-install-rc43-backup/`에 있다.
- rebind(소유자 승인): `fdfda5c5…` → `2a6403bee484cda985b9aadd7ced037e2b4985cc26100350d8e5049644d32b68`, 결과 `rebound`.
  - 바뀐 것은 adapter 경로(rc42 → rc43)뿐이다. adapter 해시 `1509e683…`, operations, goldModel, modelDir, cohort, 만료(10-18 17:15 KST)는 그대로다.
- `require_operation` 결과(gold, local_train, quality_evaluate):
  - Codex 캐시 rc43, Claude 캐시 rc43, Claude 실행 폴더 rc43: 모두 통과
  - Claude 실행 폴더 rc42: `runtime_authorization_adapter_invalid`(의도한 동작. rc42로 시작한 세션은 실행 권한이 닫힌다)
- history DB 해시는 rebind 전후로 같다. 아직 새 history에 쓰기가 없어 rollback 창은 열려 있다.
- 실제 Claude·Codex 새 세션에서 hook이 rc43을 로드하는지는 아직 확인하지 않았다. 확인 방법은 12:14 절의 headless 세션 방식이다.

**남은 일**
1. rc42로 시작한 세션(이 문서를 쓴 세션 포함)을 재시작한다.
2. 2단계(MacBook 신규 설치 검증)를 rc43으로 진행한다.
3. 3단계(agy 리뷰).

## 2026-10-13 14:10 KST — rc43 로딩 확인, Codex의 오래된 qmd 복사본 두 개 비활성화

- 새 headless 세션에서 확인했다. 실행 위치는 중립 폴더 `~/Documents/Codex/2026-10-13/task/qmd-ack-check`다. lifecycle request를 내지 않았으므로 ack는 확인 범위에 넣지 않았다.
  - Claude `claude -p`는 `…/qmd-v44-local-marketplace/plugins/qmd-auto-context-v53-rc43-claude-grant-root`(rc43)를 불러왔다. SessionStart·UserPromptSubmit·Stop 훅이 모두 exit 0이다.
  - Codex `codex exec`도 rc43으로 응답했다. 그런데 SessionStart 훅 7개 중 2개가 `Failed`였다.
- 원인: `~/.codex/config.toml`에 오래된 qmd 두 개가 `enabled = true`로 남아 있었다. Codex는 rc43과 함께 이 둘을 계속 불러왔다.
  - `qmd-auto-context@qmd-v2-frozen-synthetic-20261007`, 버전 0.35.2-dev.20261004
  - `qmd-auto-context@qmd-v2-stop-hook-20261007`, 버전 0.35.2-dev.20261007-stop-batch
  - 두 플러그인의 marketplace는 등록이 지워져 `codex plugin list`에 나오지 않는다. 하지만 config의 활성화 항목과 cache 폴더가 남아 있었다.
  - 이전 버전의 `update-queued`는 `[qmd] …` 알림을 JSON이 아닌 일반 텍스트로 출력한다. Codex는 이 출력을 잘못된 JSON으로 보고 그 훅을 `Failed`로 표시했다. 실패한 훅 2개가 정확히 이 둘이다.
  - 표시 문제로 끝나지 않았다. Codex에서 qmd 훅(update-queued·recall·topical-stop)이 매번 세 벌씩 실행됐다.
  - 원인 확인 방법 1: `QMD_SANDBOX=1`로 qmd 훅을 모두 무력화하면 7개가 전부 성공했다.
  - 원인 확인 방법 2: PATH 앞에 둔 python3 shim에 qmd 진입점이 3회씩 기록됐다. 두 이전 복사본의 훅을 직접 실행하면 JSON이 아닌 텍스트가 나왔다.
- 조치(소유자 승인): 두 항목을 `enabled = false`로 바꿨다. cache 폴더는 지우지 않았다.
  - 변경 전 설정은 `~/Documents/Codex/2026-10-13/task/qmd-codex-stale-plugins-backup/`에 백업했다.
  - 재확인: SessionStart 3/3 Completed. qmd 진입점은 이벤트마다 1회만 실행됐고, 이전 복사본은 한 번도 호출되지 않았다. Codex에서 활성 상태인 qmd는 `qmd-shared-v21-local`(rc43) 하나다.

**남은 일**
1. rc42로 시작한 세션을 재시작한다.
2. 2단계(MacBook 신규 설치 검증)를 rc43으로 진행한다. 새 머신에도 이전 qmd 활성화 항목이 남아 있지 않은지 함께 본다.
3. 3단계(agy 리뷰).


## 2026-10-15 — MacBook 전환 전 자동화·프로세스 재확인

- 활성 전환/handoff를 새로 실행하기 전, service-engineering의 Codex inbox 자동화를 공식 관리 경로에서 일시정지하고 정지 상태를 확인한다. 기존 세션만 종료해도 자동화가 새 `codex exec` 세션을 만들 수 있다. 사용자 관찰로는 16:16~18:12에 8회, 간격은 약 6~20분이었다. 이 세션의 v2 SessionStart가 재색인을 enqueue할 수 있으므로, 실행 중 프로세스가 잠깐 없다는 사실만으로 정지를 판정하지 않는다.
- 확인 근거에는 자동화 식별자, 정지 시각/상태와 진행 중 실행 유무를 남긴다. 자동화가 무엇인지 또는 정지 여부를 확인할 수 없으면 activate/handoff는 보류한다. 이미 시작한 세션·worker가 있으면 프로젝트·로드 버전·큐/쓰기 영향을 읽어 확인한 뒤 종료 필요성을 별도로 보고한다. 자동화 정지·세션 종료·운영 전환을 이 문서 수정만으로 수행하거나 승인된 것으로 간주하지 않는다.
- 이전 종료 후보 PID 41636·48375는 사용자가 이미 종료됐다고 알려왔다. 09:34 시작 Claude bg-spare PID 38080은 v1 프로세스로 남아 있다는 사용자 보고가 있다. 모두 당시 정보이며 실행 직전 정상 호스트에서 명령·시작 시각·프로젝트·로드 버전을 다시 확인한다. 숫자 PID만 보고 종료하지 않는다.
- 이 세션의 workspaceWrite seatbelt에서는 ps/kill(0) 및 Metal 접근이 거부될 수 있다. 프로세스 identity를 확인하지 못하면 `qmd_endpoint_identity_unavailable`로 중단 사유를 드러내고, 자동화 정지나 기존 프로세스 종료 완료로 판정하지 않는다. 라이브 검증은 승인된 정상 호스트 실행 경로에서 수행하며 가드·샌드박스를 끄지 않는다.
- 작업 후 자동화 재개도 영향과 시점을 따로 기록한다. 이번 로컬 수정/읽기 조사에서는 자동화·세션·DB·활성 포인터를 변경하지 않았다.

Laya 실제 상주/학습/증빙 현황은 [2026-10-15 읽기 조사](plans/2026-10-15-laya-residency-readonly-findings.md)에 정리했다. 일반 운영 경로는 아직 호출별 모델 로드이며, 기존 상주 worker는 experimental edit 경로의 프로젝트별 90초 유휴 종료 방식이다. 같은 OS 사용자 전체에서 한 개의 추론 서비스를 공유한다는 요구는 추가 구현이 필요하다.
