# 학습

학습 선택, 추론 서비스와 selector 유지보수를 담당한다. 초기 활성화 후 detached 작업이 heal 다음 warm을 수행한다.

base checkpoint 포인터는 `schema_version: 1`을 기록한다. 해당 키만 없는
기본 enrollment 포인터는 보정하고 receipt 재준비 후 warm을 수행한다.
다른 값이 달라진 포인터는 거절한다. selector 재시도는 최소 10분 간격이며
지속 실패 시 최대 1시간까지 지수 backoff를 적용한다. 자원 부족과
시작 경로·권한·IO 실패는 별도 고정 사유 코드로 기록한다.

warm은 client 검증 30초, resident 준비 대기 60초, 준비 뒤 추론 요청
30초를 각각 사용한다. 추론 요청 상한은 wire의 30초와 같다. 첫 추론
요청이 시간 초과하면 같은 resident의 idle을 최대 60초 기다리고 30초
요청을 한 번 더 보낸다. 기동 시간 초과에는 이 추가 요청을 보내지 않는다.
성공한 추론만 warm 영수증을 기록한다.

`learning.selector.warm.phase`와 `learning.selector.maintenance`는 성공·실패·미룸의
기동 대기, 검증, 모델 로드, 추론 시간을 측정한 구간만 숫자로 기록한다.
닫힌 소켓 때문에 관측하지 못한 서버 구간은 생략한다. 시간 초과 후 같은
세대 모델을 재사용하면 `cached_model_load_seconds`로 첫 로드 시간을 전달한다.

학습 승인과 갱신이 쓰는 host plugin 목록(`claude|codex plugin list --json`을 프로젝트에서
실행한 것, Codex의 `--marketplace` 목록)은 host·종류·프로젝트마다 기록해 두고
(`host_inventory.py`, learning state의 `host-inventory/`, 0600), CLI 실행 파일과 host 설치
기록 파일(Codex `config.toml`과 plugin cache 디렉터리, Claude settings·plugin 기록)의 stat
stamp가 같으면 CLI를 부르지 않는다. 목록이 가리키는 설치는 매번 설치 기록과 대조한다.
느린 CLI를 기다리는 곳은 selector maintenance뿐이다. maintenance는 짧은 준비 예산 단계
전에 기록이 없거나 stamp가 바뀐 목록을 120초 안에서 다시 받고, 실패하면 7일 안의 기록을
쓰거나(`stale`) 그 목록이 필요한 단계를 `host_inventory_unavailable`로 미룬다. 결과는
`learning.selector.inventory`(`refreshed`·`stale`·`unavailable`)로 남는다. 그 밖의 호출부
(index 뒤 wake, project worker, capture worker, recall의 adapter 확인, setup)는 기록을 쓰고,
stamp가 바뀌었으면 7일 안의 기록을 쓰며, 기록이 아예 없을 때만 원래의 짧은 제한(프로젝트
목록 10초, Codex marketplace 목록 2초 또는 남은 예산)으로 CLI를 부른다. 그 실패는 원래
오류 그대로다. setup 명령은 목록 시간 초과를 `deferred`·`host_inventory_unavailable`로 끝낸다.

SessionStart의 owner 경합·요청 게이트·예외는 `learning.selector.request`에
고정 사유 코드로 기록한다. 실패·미룸은 기존 `learning` wake를 재시도
하한에 예약하고, 게시를 끝낸 sync는 같은 owner 아래 학습 요청을 다시
확인한다. 요청 하한과 backoff는 이 경로에도 적용한다.
sync가 하한보다 먼저 끝나면 `learning`에 남은 지연을 예약한다. 학습 예약은
`laya-sync`의 편집·vector 재offer due를 바꾸지 않는다. 더 이른 일반 학습
wake와 합쳐지면 due probe와 기존 큐 backoff로 유지하며 일반 학습 step도
함께 실행한다. 결과 기록이 owner 경합에 지면 파일을 바꾸지 않고 요청
하한에 `learning` wake를 남긴다. 요청 파일의 실패 횟수가 아직 0이어도
미게시 버전이나 profile 활성화 전 기본 namespace의 미완료 warm을
재시도 대상으로 확인한다. 학습 runtime이나
모델이 없는 설치는 자동 polling을 예약하지 않고 자연스러운 다음 요청을
기다린다. 큐 용량 때문에 예약이 버려지면 `retry_offer_dropped`를 기록한다.

기본 namespace의 adapter 경로만 달라진 정책은 heal의 내용·승인 범위
검증으로 재결속한다. 미활성 기본 namespace는 profile을 만들기 전에
현재 번들의 adapter 경로로 원자적으로 고정한다. interpreter·model·다른
정책 키가 달라지면 거절하며, 정책 다이제스트가 달라지면 추론 증명을
재준비한다. 이미 승인된 profile은 이 경로로 재기록하지 않는다.
선택 설치의 기본 자동 학습 profile은 별도 renewal 경계에서 공식 번들
업데이트의 코드·설치·adapter 경로와 파생 digest만 갱신한다. 정책의 다른
필드, interpreter·model, project·namespace, operations·cohort 전체, 기존
만료와 profile 선택은 유지한다. default-local과 default-teacher 모두 권한 필드를
바이트 그대로 두며, default-teacher는 동의 파일과 공유 동의 파일을 시작할 때와
intent를 지우기 직전에 plan과 대조하고 쓰지 않는다. 수동·custom·명시 teacher·
opt-out·만료된 승인은 자동 갱신하지 않는다. settings와 최초 setup 승인 기록은 쓰지 않는다.
기본 grant는 해당 host가 프로젝트에서 선택한 MemVane marketplace와
출처·설치 바이트에 결속하며 다른 marketplace 항목은 결속하지 않는다.
다른 host의 사본이 갱신하면 marketplace id·scope·projectPath가 같고 두
host의 출처가 모두 공식 MemVane repo여야 한다(Claude는 `{github, repo}`,
Codex는 `{git, url}`로 기록한다).
renewal intent는 다섯 승인 파일의 원래/새 바이트 쌍을 기록하고 소비를
차단한다. 같은 대상은 CAS로 이어서 게시하고, 더 새 대상은 원래 승인으로
CAS 복원을 끝낸 뒤 다시 계획한다. 설명되지 않는 파일·settings 변경과
낮은 host는 fence를 유지하며 거절한다.
설치 manifest는 0644를 허용하고 소유자·경로·link·비신뢰 쓰기 권한과
읽기 전후 fd/path 정체성을 검사한다. intent와 승인 상태 파일은 별도의
소유자 전용 읽기 규칙을 유지한다.

legacy 전체 registry SHA의 이행은 고정된 공식 1.0.15 배포의 기존
code/executable/manifest/adapter 지문과 기본 승인 체인, 현재 공식 remote
출처를 모두 확인한다. 구 캐시가 있으면 저장 지문으로 다시 검증하며
없으면 저장된 배포 지문을 사용한다. 당시 registry의 source는 전체 SHA로
복원할 수 없으므로 legacy directory/custom 출처와 다른 배포 지문은
자동 이행하지 않는다. 새 self binding은 자기 source 변경도 거절한다.
중단된 기본 등록은 기존 설치 binding을 검증한 뒤 adapter 경로 고정보다
먼저 복구한다. 복구는 backup과 source settings의 결속 및 모든 복원 대상의
경로·권한·내용을 확인한 뒤 단계를 기록한다. settings가 backup과 같으면
파일을 쓰지 않는다. 다른 사용자 편집은 거절하며, 0644 settings는 허용하지만
그룹 쓰기가 가능한 0664와 symlink·hardlink는 거절한다. 복구 중단 뒤 재진입은
검증된 backup 내용으로 이미 복원된 namespace 파일도 허용한다.
준비할 selector가 없으면 warm 없이 버전을 게시한다.
후보 apply의 resident 시작 실패는 재시도를 예약하며, 실행 중인 resident의
정체성·권한 거절은 후보 적용을 차단한다.

구현: `context_learning_auto.py`, `context_learning_cli.py`, `prepare_laya_inference.py`, `project_learning_setup.py`, `project_learning_recovery.py`, `selector_maintenance.py`, `default_teacher_enable.py`.

저장된 recall capture의 후보 출처는 별도 `learning.capture.sources` 진단
이벤트에 기록한다. `request_id`로 기존 표본과 연결하며 후보 id의 SHA-256과
`store`·`python`·`lexical` 값만 담는다. 표본 스키마·학습 입력은 그대로다.
capture가 꺼져 있으면 출처 이벤트도 쓰지 않는다.

학습 사례는 편집 recall에서만 수집한다(sample schema v5, `context_origin: tool_edit`).
질의는 편집이 추가한 텍스트이고 편집 경로는 해시로만 남는다. default-teacher
namespace에서 v5 사례는 기본 teacher의 입력이다. 명시 프로필의 cohort 배정은
v5 사례를 건너뛴다. 기본 namespace의
첫 worker pass는 schema 4 이하 prompt 사례(shared `cases`와 같은 request id의
private `samples`)를 한 번 지우고 개수를 `prompt-cases-retired.json`과
`learning.migration` 이벤트에 남긴다(`context_learning/prompt_retirement.py`).

편집 사례는 편집 경로를 git이 무시하면 저장하지 않는다. 프로젝트 루트가 상위
디렉터리의 git work tree 안에 있어도 같은 검사를 적용하고, git이 답하지 못하면
사례를 저장하지 않는다. capture는 프로젝트의 첫 편집 사례를 shared queue에
넣기 전에 이 검사의 적용 시각 파일(`edit-git-check-fixed.<project key>.json`)을
한 번 만들고 갱신하지 않는다. worker pass는 루트에 `.git`이 없고 상위 work tree
안에 있는 프로젝트에서 적용 시각과 자신의 시작 시각 중 이른 시각보다 먼저
저장된 v5 편집 사례(shared `cases`와 같은 request id의 schema 5 private
`samples`)를 한 번 지운다. 삭제 전에 예정 개수를 pending 파일에 기록하고,
완료 개수는 `edit-git-migration.<project key>.json`과 `learning.migration`
이벤트에 남긴다. 해당하지 않는 프로젝트는 개수 0으로 표시만 하고, git이 답하지
못하면 표시 없이 다음 pass에 다시 확인한다(`context_learning/edit_git_migration.py`).
v5 사례는 teacher가 저장된 snapshot만으로 판정하므로 후보 카드의 원문이 바뀌어도
`historical`로 바뀌지 않는다. 동의·설정·tombstone·private 표본 대조는 그대로
적용한다.

## 기본 teacher

기본 namespace의 프로필 종류는 `default-local`, `default-teacher`, `synthetic`,
`explicit` 넷이다(`default_enrollment.profile_kind`). 새 온보딩은 `default-teacher`로
등록하고, teacher 끄기 표시가 있으면 `default-local`로 등록한다. teacher 권한은 setup
journal의 plan mode, grant의 `gold_default` operation, 동의 v2 파일
(`project-teacher-consent.json`, `teacher_consent.py`의 고정 template) 셋이 담는다.
공유 동의 파일은 `externalTeacher: true`다. 1.0.21보다 낮은 번들은 이 모양을 거절하므로
그 호스트 사본은 수집·학습을 하지 않고 recall은 hybrid 순위로 답한다.

기존 default-local 설치는 selector maintenance가 `default_teacher_enable.py`로 한 번에
바꾼다. intent(`project-teacher-enable.json`)에 대상별 old/new 바이트를 적고 CAS로 쓴다.
intent가 있는 동안 grant·profile·선택 검사가 거절한다. 다음 실행이 전제가 유지되면 앞으로
끝내고, 깨졌으면 되돌린다. old도 new도 아닌 대상이 있으면 쓰기를 멈추고 fence를 유지한다.
setup skill의 `restore-teacher-enable`이 old 바이트로 되돌린다. 같은 사유의 실패가 5번
이어지면 플러그인 버전이 바뀌거나 복구 명령이 돌 때까지 요청을 멈춘다.

teacher는 학습 worker에서만 돈다(`general_teacher.process_pending_default`, pass당 호출
8건). 사례를 수집한 세션의 호스트 CLI만 쓰고, 번들 tier 표(`teacher_model_tiers.py`)의
모델만 요청하며, 호출마다 격리 사전 점검(`teacher_isolation.py`)을 지나야 보낸다. 호스트
호출·대기·차단기는 `teacher_hosts.py`와 namespace의 `teacher-host-state.json`이 맡는다.
입력은 편집 텍스트와 후보 카드 본문 스냅숏뿐이다. 루트에 `.git`이 없고 상위 work tree 안에
있는 프로젝트는 teacher 동의의 `grantedAtUnixMs`보다 먼저 수집된 v5 편집 사례를 모두
teacher·학습 대상에서 빼고 지우지 않은 채 `historical`(`pre_teacher_capture`)로 둔다. git이
답하지 못하면 그 pass는 부르지 않는다. gold는 holdout 없이 모두 학습에 들어가고, 승격은 학습
cutoff 뒤 사례로 판정한다. 승격 판정에는 grant의 cohort 한도에 더해 비지 않은 gold 평가 사례
10건이 필요하다(`prequential.py`).

hook은 teacher 전송·격리·tier·전환 모듈을 import하지 않는다. SessionStart 안내는
`teacher_state.py`(stdlib와 kernel만 import)로 `teacher-host-state.json`과 전환 intent의
존재만 본다. 끄기는 setup skill의 `teacher-off`·`teacher-on`(`teacher-switch.json`) 하나다.
환경 변수는 끄기 수단이 아니다. 계약 전체는 `docs/continual-learning.md`의
Default teacher 절에 있다.
