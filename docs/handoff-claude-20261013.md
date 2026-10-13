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
