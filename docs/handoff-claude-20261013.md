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
