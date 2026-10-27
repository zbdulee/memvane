# Laya Index 사용자 데이터 이전 계획 (설계 전용)

QMD-origin 전환은 installer만 수행한다(`core/laya_qmd_cutover.py`). QMD DB는 읽기 전용으로 열고 원본 DB와 backup은 지우지 않는다. 원장 v1(단일 collection)과 v2(raw+wiki)는 full-scan producer가 게시한 후보를, v3는 비활성 initial build가 capacity path에서 seal한 후보를 결속한다. v3 증명은 저장된 generation binding, 그것이 가리키는 catalog snapshot과 vector seal로 하며 원문 트리를 스캔하지 않는다. route 선택은 설치 소유 journal의 `cas_activate`로, QMD 선택 복원은 `cas_restore_qmd`로 기록한다. project marker는 중단된 선택 route가 legacy QMD hook 호출로 조용히 바뀌지 않게 한다. QMD 형식 읽기 adapter(`core/laya_qmd_migration.py`)는 제목을 inventory/digest에 포함하고, 결과에 `candidate_ready`와 별개로 `cutover_ready=False`를 기록한다.

학습은 이관 범위에서 빠진 기능이 아니다. 설치된 프로젝트 안에서 학습 작업의 내구 대기·OS 자원 admission·소유 프로세스 중지·정확한 cursor 재개를 제품 게이트로 유지한다. 검색·embedding **기능**과 이관은 이 계획의 목표이고, 검색·embedding 작업의 별도 부하 대기열 확장은 v2를 완전히 연 뒤 사용량을 보고 정한다. 학습 실행과 운영 학습 DB 이관은 이 계획의 실행 범위가 아니다.
학습의 모델 없는 검사와 실제 한정 시험의 선행조건은
[학습 runtime 게이트](2026-10-18-laya-training-runtime-gate.md)에 기록한다.

이 문서는 [내장 SQLite 검색 엔진](2026-10-17-embedded-sqlite-search-engine.md)의
후속 운영 전환 경계를 정한다. **현재 설치·사용자 설정·QMD 색인 DB·원고·위키·
학습 DB·모델 포인터를 읽거나 변경하라는 실행 지시가 아니다.** 선택된 제품
경로는 기존대로 유지한다. 엔진 후보의 schema와 품질·복구 계약이 독립적으로
검증되기 전에는 전환하지 않는다.

전환 대상은 **이 프로젝트의 QMD backend 연결**이다. 사용자가 별도로 설치한
global QMD 프로그램과 다른 용도의 색인·설정은 이관 대상이 아니며 삭제하거나
비활성화하지 않는다. 기존 이 프로젝트의 QMD 색인·설정은 정확한 소유 경계를
확인한 뒤 새 `laya-index` 후보에 매핑한다. Laya 학습 모델은 검색 엔진과
별도 구성요소로 유지한다.

## 보존해야 할 의미와 분리할 저장소

| 데이터 | 이전 계약 |
|---|---|
| 원문·위키 | 원본 경로와 바이트, 카드의 출처·원문 revision·승인/동의 경계를 보존한다. 원본을 제자리 변환하지 않는다. Laya는 위키 카드만 색인하고, 원문은 path·hash cursor로 freshness 판단과 위키 생성에만 쓴다. |
| 프로젝트 설정·collection | 기존 이름·role·허용 root와 opt-out을 명시적으로 매핑한다. 새 worktree는 자동 승인하지 않는다. 지원 불가 설정은 경고 후 해당 프로젝트를 이전하지 않는다. |
| QMD index | 물리 DB 스키마와 벡터 바이트 동일성은 요구하지 않는다. 문서 멤버십, 검색 범위, 모델/청킹 provenance와 결과의 근거를 대조한다. 벡터를 안전하게 재사용할 수 없으면 별도 승인된 재생성 계획이 필요하다. |
| 학습·평가 | 기존 이력·gold·평가·checkpoint 참조는 독립 저장소의 책임으로 유지한다. 새 엔진 corpus/view ID와의 대응 표를 검증하기 전 자동 학습·모델 적용을 켜지 않는다. |
| 런타임·hook | 한 세션의 요청은 QMD 또는 내장 엔진 한 경로만 사용한다. 기존 세션을 임의 종료하거나 설정을 조용히 바꾸지 않는다. |

## QMD runtime 소유권과 정리 경계

전환 후 **플러그인이 독립 설치·관리한 QMD runtime**은 정리 대상이다.
사용자가 별도로 설치한 global QMD와 다른 프로그램이 공유하는 실행 파일·
라이브러리·모델 캐시는 정리 대상이 아니다. 경로가 QMD처럼 보인다는 이유만으로
소유권을 추론하지 않는다. Laya product bundle은 자기 엔진 파일의
`ownership-manifest.json`을 기록하지만, 아래의 manifest 기반 QMD runtime 정리는
구현되지 않은 목표다.

- 설치·업데이트 journal과 별도의 **ownership manifest**로 install ID,
  플러그인 소유 root, 세대, 설치 출처/receipt, 각 패키지·설정·DB·캐시의
  정규 경로·해시·역할, 공유 여부와 현재 참조를 기록·대조한다. 기존 기록에
  이 필드가 없으면 확인 가능한 증거를 모아 manifest 후보를 만들되,
  증명되지 않은 항목을 플러그인 소유로 승격하지 않는다.
- manifest에는 파일별 소유 증거를 `확인됨`/`불확실`/`타 소유`로 구분한다.
  설치 경로 이름이나 해시 일치만으로 전용 소유권을 추론하지 않는다.
- 정리 직전 선택된 엔진 세대와 사용 중인 endpoint·host 세션·writer 참조를
  확인한다. 대상은 manifest에 명시된 **전용 runtime 파일**로 제한하고
  경로 탈출 symlink, 공유 inode/하드링크, 다른 설치의 참조, 소유권 충돌·누락,
  활성 작업이 있으면 그 항목의 정리를 중단한다. 원고·위키·학습 DB·사용자
  설정·모델 파일은 runtime 정리에 섞지 않는다.
- 실제 격리 이동 직전에 정규 경로·device/inode·링크 수·해시와 manifest
  receipt를 다시 비교한다. inventory 이후 바뀐 파일, symlink/하드링크,
  다른 volume 등 원자 이동을 증명할 수 없는 대상은 보존한다.
- 기존 색인·설정의 이관과 무결성·범위·검색 검증, Laya Index 전환 및
  rollback 가능 상태를 먼저 증명한다. 옛 DB와 설정은 rollback backup으로
  보존하고, runtime 정리는 처음에는 원자 이동 가능한 격리 보관과 manifest·
  복원 receipt를 우선 설계한다. 실패하면 이전 세대 재선택과 전용 runtime
  복구가 가능해야 한다.
- 보존 기간 뒤의 **영구 삭제는 별도 확인**을 받은 대상에만 수행한다.
  소유권이 불확실하거나 rollback backup의 필요 기간이 끝나지 않았으면
  삭제하지 않는다. 별도 global QMD 설치본은 이 확인의 대상에도 넣지 않는다.

## 단계와 실패 시 처리

### 현재 구현과 남은 목표

`core/laya_index_migration_rehearsal.py`는 운영 이관 도구가 아니라 예행 도구다. 이 모듈의 합성 application ID를 가진 DB만 입력으로 받고, 모든 경로는 호출자가 만든 표시된 예행 디렉터리 안에 있어야 한다. QMD parser, hook, 설치, runtime 선택은 포함하지 않는다.

QMD 2.5.3과 고정 fork `f4633f9` 형식의 읽기 전용 adapter는 `core/laya_qmd_migration.py`이고, 설치 소유 전환은 위의 `core/laya_qmd_cutover.py`가 맡는다. QMD-origin initial build는 content hash·chunk 수·위치가 같은 문서에 한해 QMD 벡터를 재사용하며, 표본 재임베딩 대조가 어긋나면 그 실행의 재사용을 끄고 임베딩한다(`core/laya_qmd_vector_reuse.py`).

남은 목표: 사용자 설정·worktree·opt-out과 삭제 증거의 일관된 snapshot, 복원 가능한 백업과 후보 간 범위·검색 대조의 운영 절차, 아래 6단계의 ownership manifest 기반 플러그인 소유 QMD runtime 정리. 별도 global QMD는 제외하고, 플러그인 소유 runtime도 증명된 대상만 복구 가능한 격리 후보로 삼는다. 필수 설치 후보 QA가 `NOT RUN`인 동안 제품 전환 완료로 기록하지 않는다.

1. **명시적 inventory 계획:** 허용된 프로젝트·worktree, 기존 설정과 선택된
   색인/학습 세대, collection별 경로·문서 수·해시·모델/청킹 지문, 위키
   provenance를 열거할 읽기 전용 절차를 별도로 검토한다. 개인정보 본문이나
   비밀값은 보고서에 넣지 않는다. 실제 inventory 실행은 별도 범위다.
2. **격리 예행:** 합성 데이터와 사용자 자료와 같은 형식의 비운영 사본에서
   엔진 schema 변환·재색인, 범위별 FTS/벡터 검색, 삭제·rename·공유 해시,
   누락·중복·교차 프로젝트 누출과 중단 복구를 검증한다. 모델이 필요하면
   별도 승인·자원 게이트를 따른다. 기존 QMD DB를 쓰기 열지 않는다.
3. **비활성 후보:** 설치별 `~/.local/state/laya/<install id>` root에만 후보를 준비한다. 원본
   revision, 승인 scope, schema version, 모델 지문, 데이터·검색 검증 receipt를
   묶는다. 후보 오류·불완전한 위키 의존성·불확실한 queue ACK가 있으면 기존
   선택을 유지한다.
4. **전환 심사:** 새 설치와 데이터 경로, 전체 writer 차단/전환, host hook의
   실제 새 세션, 요청 generation pin, 자원·지연, 실패 코드·복구 창을 한
   체크리스트로 검토한다. 사용자 승인이 필요한 실제 설치/전환은 이 단계의
   구체적 결과를 제시한 뒤 수행한다.
5. **원자 선택과 관측:** 후보 endpoint가 준비된 뒤 설치 소유 journal에서
   예상 이전 세대와 후보 receipt를 CAS한다. 새 요청만 새 엔진에 라우팅하고
   옛 요청은 기존 경로에서 끝낸다. 검색 결과와 DB 세대가 섞이면 실패로
   처리한다. 롤백은 새 엔진 쪽 사용자 쓰기·학습 이력이 생겼는지 먼저
   판정한다. 첫 새 쓰기 이후에는 단순 pointer 복귀가 데이터 손실을 만들
   수 있으므로, 감사 가능한 역이전 또는 새 엔진의 전진 복구 계약을 먼저
   검증한다. 둘 다 없으면 자동 롤백을 허용하지 않는다.
6. **소유 runtime 정리:** 전환 안정화와 rollback backup 보존을 확인한 뒤
   ownership manifest에 확실히 귀속된 플러그인 전용 QMD runtime만 복구 가능한
   격리 보관 후보로 삼는다. 같은 volume의 원자 rename, 대상별 receipt,
   보관·복원 회귀와 실제 대상 목록의 별도 검토를
   마치기 전에는 실행하지 않는다. 영구 삭제는 다시 별도로 확인한다.

## 이관 전 확인 게이트

| 게이트 | 확인할 증거·결정 |
|---|---|
| 백업 일관성 | 변경 중인 SQLite를 파일 복사만으로 백업했다고 주장하지 않는다. 선택한 세대·설정·원문 revision·위키 provenance·학습 참조를 한 시점의 inventory와 묶고, 비운영 사본에서 restore rehearsal 및 행·해시·scope 대조를 한다. |
| 전환 뒤 쓰기 | 첫 새 쓰기와 학습 소비가 발생한 뒤의 rollback 가능 범위를 따로 정의한다. 역이전과 전진 복구 중 무엇을 지원할지 결정하기 전에는 이를 단순 되돌림으로 표기하지 않는다. 기존 세션을 임의 종료하는 것을 전제하지 않는다. |
| 철회·보존 | opt-out·색인 제외는 현재 세대뿐 아니라 보존된 rollback 세대와 장기 pin의 제공에서도 즉시 차단하고 물리 삭제는 보류한다. 학습 평가의 재현성과 개인정보 보존 요구가 충돌할 때의 물리 삭제 범위·기간과 대상별 계약은 별도로 정한다. |
| 정리 | manifest의 소유 증거가 불확실하면 보존한다. 격리 이동 직전 파일 identity를 다시 확인하고, 복원 경로와 보존 기간을 시험한다. 영구 삭제는 별도 승인 대상이다. |
| 릴리스 QA | `docs/release.md`의 새 Claude·Codex 실제 host 검증에 설치/upgrade, 이관 중단·재개, 첫 쓰기 전후 rollback/복구, 운영 QMD와 별도 global QMD 공존, runtime 격리·복원 시나리오를 필수 행으로 편입할지 결정한다. 필수 행의 `FAIL`/`BLOCKED`/`NOT RUN`을 통과로 간주하지 않는다. AGY를 자동으로 필수 gate로 추가하지 않는다. |

이 항목은 실행 승인이 아니다. 사용자 자료를 쓰는 inventory·전환,
모델/학습 실행, 플러그인 전용 runtime 격리 이동과 영구 삭제는 각각 실제
대상과 복구 증거를 준비한 뒤 별도 판단을 받는다.

제품 이름 결정과 QMD 호환 CLI 제공은 이 이전의 전제 조건이 아니다.
사용자 데이터의 의미·출처·권한과 복구 가능성은 전제 조건이다. 플러그인이
독립 관리한 QMD runtime의 정리는 이 전제와 소유권 증명을 충족한 뒤의
후속 단계다. 별도 global QMD는 이 정리 범위에 포함하지 않는다.
