# 학습

학습 선택, 추론 서비스와 selector 유지보수를 담당한다. 초기 활성화 후 detached 작업이 heal 다음 warm을 수행한다.

base checkpoint 포인터는 `schema_version: 1`을 기록한다. 해당 키만 없는
기본 enrollment 포인터는 보정하고 receipt 재준비 후 warm을 수행한다.
다른 값이 달라진 포인터는 거절한다. selector 재시도는 최소 10분 간격이며
지속 실패 시 최대 1시간까지 지수 backoff를 적용한다. 자원 부족과
시작 경로·권한·IO 실패는 별도 고정 사유 코드로 기록한다.
유지보수 한 번의 전체 시간은 `MAINTENANCE_SECONDS`(1,800초)다. 넘으면 그 run은
`maintenance_timeout` 실패로 끝나 요청이 backoff하고 준비 잠금을 반납한다.
`learning` wake는 미완으로 끝난 pass가 연속 288회(약 24시간)면 `retry_cap`으로
버려지고(`wake.drop`), 다음 hook의 요청이나 selector 재제안이 새 행을 만든다. 이 상한은 사유를 보지 않고 미완 pass를 센다: 학습이 조각으로 진행 중인 pass와 자식이 도는 동안의 확인도 한 번이므로, `kind` `learning`의 `wake.drop` `retry_cap` 행만으로 "학습이 끝내 못 돌았다"고 읽지 않고 같은 시간대의 `learning.cycle` 행과 함께 본다. 상한이 없던 버전에서 오래 미뤄진 행은 갱신 뒤 첫 미완 pass에서 큰 `attempts`와 함께 바로 버려질 수 있다.
worker 자식이 재시도 결과(`deferred_resource`·`busy`·`apply_deferred` 등, 종류가 섞여도 이어 센다)로 연속 288회 끝나면 그 요청은 더 깨우지 않고
마지막 결과로 정산되며 `learning.cycle` `retry_cap` 행(사유 = 마지막 결과, `retries`)이 남는다(`worker_retry.py`. 횟수는 namespace의 `worker-retry.json`에 두며, 없거나 읽히지 않거나 저장된 상태가 없는 옛 형식이면 0부터 세는 캐시라 1.x 영속 계약에 들지 않는다). 횟수를 쓰지 못한 결과는 그 자리에서 정산하고 `retry_cap` 행에 `detail` `retry_count_unwritable`을 남긴다. 재시도가 아닌 결과, 그리고 진척이 있었던 pass가 이 수를 0으로 돌린다. 진척의 정의는 `worker_retry.moved` 하나다: 직전 재시도 결과 때 같은 파일에 적어 둔 durable 상태(`worker_retry.snapshot`)와 지금 사이에 job·cycle·평가·적용 journal·활성 포인터가 `worker_retry.FORWARD`의 쌍(상태 모델의 `PROGRESS` 이벤트에서 유도, 학습 완료·종결, 평가 지표 저장·종결, 포인터 기록, 활성화·종결)로 앞으로 갔거나 학습 cursor의 저장 그룹이 늘었으면 진척이다. 상태는 최근 64행, 열린 후보(`staged`·`staged_evaluated` cycle)의 행, 직전 상태에 있던 행을 읽으므로 오래된 열린 후보가 평가를 끝낸 것도 진척이다. 기다림·재시도·다시 형성되는 intent와 prepare·되돌아가는 전이는 진척이 아니다. 상태를 읽지 못한 pass(잠긴 DB 등)는 진척도 초기화도 아니다: 마지막으로 읽은 상태를 그대로 두고 한 번으로 센다.
다음 hook의 요청부터 다시 센다. trainer 실패로 막힌 job은 실패한 학습 시도의 누적(`attempts`: 실패하거나 중단된 claim만 세고, cursor가 전진한 조각과 자원·추론 미룸은 세지 않는다)이 30회에 닿으면 재무장하지 않고
`trainer_attempts_exhausted`로 닫히며, trainer 자식이 보고한 반복 불가 사유(`trainer_failed:` 접두)도
worker의 것과 같이 즉시 닫힌다.
유지보수가 끝나는 모든 방식과 요청·등록·갱신·teacher 전환·warm의 전이는
`docs/dev/learning-state.md`의 M9~M16 표다.

학습 namespace의 불변식(`context_learning/invariants.py`: I-1~I-9, G-1, G-7)은 읽기만 하는 검사다. 시험은 단계마다
위반 목록을 보고, 설치된 프로젝트에서는 `memvane:logs`의 학습 점검(`project_counts`)이 규칙별 위반 개수만 낸다.

실제 모델로 학습 경로를 도는 증명 run은 `memvane/learning/tests/model/`의 둘이다. 둘 다 한 디렉터리에 상태를
두고 여러 번의 `model-test` 실행으로 이어 돌리며, 호출마다 시작 줄(호출 번호, 커밋, 플러그인 버전, 프로필이 묶은
모델 정체성, 인자)과 끝 줄(끝난 이유), pass·trainer 호출마다 한 줄, 끝난 뒤의 요약(학습 조각과 시간, 미룸, 기본·후보의
네 지표, 적용 여부, 적용 뒤 recall 행의 `selectorCheckpoint`)을 `lp2-record.jsonl`에 남긴다. 종료 코드는 위반 없이
끝에 닿았거나 시간 예산만 다한 호출이 0, 자원 미룸이 이어져 멈춘 호출이 2, 그 밖이 1이다. teacher는 둘 다 결정적 가짜
CLI다.

- `resident_loop.py`: 체크아웃을 fixture HOME의 host 플러그인 캐시에 복사해 그 번들로 돈다. enrollment의 selector
  준비·warm, 코드 정체성, 제품이 policy로 만든 trainer와 그 자원 감시, 적용 transport, 실제 interpreter와 모델로
  도는 추론 서비스(`apply_prepare`·`apply_activate`), recall의 selector 예측이 실제다. 끝의 recall 행의
  `selectorCheckpoint`가 적용 artifact와 같아야 통과다. 대역은 host CLI(teacher, 플러그인 목록), 설치된 runtime
  조회, 서비스의 시작 방식(per-user resident가 띄우는 대신 learning 전용 시작)과 wake(구동기가 pass를 부른다)다.
- `lp2.py`: LP-1의 합성 enrollment 위에서 trainer의 학습·예측 프로토콜과 수락 쪽 자원 보고만 실제다. trainer는
  구동기가 만들어 제품의 trainer 감시는 지나지 않고, resident의 적용 transport는 대역이다. gold가 30건을 넘으면 `learning.score` 행에 순위 기준선
(`rankCases`·`rankExactRate`·`rankMicroF1`: hybrid 순서의 앞 세 장 대 gold, 추론 없음)이 실린다. trainer 자식이 남긴
`error_site`는 항목별로 걸러 `file:line function` 꼴인 것만 최대 8개 남긴다.

사용자 단위 준비 lock(`learning-prepare.lock`)을 다른 설치가 쥐고 있으면
(`learning_prepare_busy`) 실패로 세지 않는다. 요청 행에 `busyRetries`를 올리고
60초 뒤(`BUSY_RETRY_SECONDS`) `learning` wake로 다시 요청하며, 그동안 10분 하한 대신
60초 하한을 쓴다. 큐에 이미 `learning` wake가 있으면 그 due를 따른다. 연속 30회
(`BUSY_RETRY_LIMIT`)에 닿으면 그 뒤 경합은 일반 미룸(실패 1회, 10분 하한, backoff)이다.
완료와 경합이 아닌 결과는 `busyRetries`를 지운다. 횟수는 `learning.selector.request`와
`learning.selector.maintenance`의 `busyRetries`에 남고, 경합 미룸은 유지보수 진입에서
예외로 올라가지 않는다. `busyRetries`를 모르는 1.x 번들은 그 행을 10분 하한으로 읽는다.

학습·평가를 시작할 때의 coordinator 메모리 승인은 측정한 trainer peak로 판단한다. 학습이 끝나면 process group의 peak RSS를 namespace의
`trainer-footprint.json`(trainer 명령별 최댓값, 계약 밖 캐시)에 남기고, 그 뒤의 승인은 `max(ceil(1.5×peak), peak+512)`
(하한 1,024MiB, 상한 policy `maxRssMib`)를 peak 추정으로 쓴다(`trainer_footprint.py`). 측정이 없으면 `maxRssMib`(4,096MiB)
그대로다. 약 1.9GB peak에서 필요량은 reserve 1,024MiB를 더해 5,120MiB에서 3,942MiB가 된다. 실행 중인 trainer의 RSS 상한과
학습 요청의 `learning_control` policy(요청 정체성의 일부, 단계마다의 승인은 올린 모델만큼 credit을 받는다)는 설정값 그대로다. 추론 서비스는 요청 없이 `IDLE_RELEASE_SECONDS`(900초)가 지나면 올린 selector 모델을 내려놓고
(`idle_release.py`, 적용의 준비된 세대가 활성화를 기다리는 동안은 제외) 다음 요청이 첫 요청처럼 다시 올린다.

warm은 client 검증 30초, resident 준비 대기 60초, 준비 뒤 추론 요청
30초를 각각 사용한다. 추론 요청 상한은 wire의 30초와 같다. 첫 추론
요청이 시간 초과하면 같은 resident의 idle을 최대 60초 기다리고 30초
요청을 한 번 더 보낸다. 기동 시간 초과에는 이 추가 요청을 보내지 않는다.
성공한 추론만 warm 영수증을 기록한다.

`learning.selector.warm.phase`와 `learning.selector.maintenance`는 성공·실패·미룸의
기동 대기, 검증, 모델 로드, 추론 시간을 측정한 구간만 숫자로 기록한다.
닫힌 소켓 때문에 관측하지 못한 서버 구간은 생략한다. 시간 초과 후 같은
세대 모델을 재사용하면 `cached_model_load_seconds`로 첫 로드 시간을 전달한다.

추론 서비스는 lane을 적은 recall 요청의 초기 정체성 검증에서, 같은 프로세스가 같은 세대를
5초 안에 sweep했으면 runtime·모델 디렉터리 stat sweep과 모듈 범위 확인만 건너뛴다
(`inference_identity.reuse_window`). receipt·binding·policy·attestation·포인터·artifact·adapter·
code 대조는 요청마다 한다. warm 요청과 후보 apply, 모델 로드 뒤 재검증은 매번 sweep한다.

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
정체성·권한 거절은 후보 적용을 차단한다. `selector_heal.check_scope`는 갱신 intent나
전환 intent(`teacher_enable_pending`)가 있으면 거절한다.

구현: `context_learning_auto.py`, `context_learning_cli.py`, `prepare_laya_inference.py`, `project_learning_setup.py`, `project_learning_recovery.py`, `selector_maintenance.py`, `default_teacher_enable.py`, `candidate_close.py`, `case_rotation.py`.

공유 학습 queue는 `context_learning/shared.py`가 진입점이다. `shared.py`는 경로·policy·DB·동의 같은
공통 seam, owned worker 자식 본문과 CLI `main`을 두고, 역할 모듈의 이름을 모두 다시 내보낸다.
역할 모듈은 `shared_cases.py`(사례 수락·학습 출처·holdout), `shared_queue.py`(capture queue·상태·후보 채점),
`shared_train.py`(학습 주기·학습 한 번·되돌리기·trial 관찰), `shared_run.py`(계측 run·hook launch·학습 전용
pass), `shared_dispatch.py`(durable worker 요청과 dispatcher step)다. 호출부와 시험의 patch는
`shared.<이름>`을 가리킨다. 역할 모듈 본문은 `shared`와 공유하는 이름을 실행 시점에 `shared` 모듈에서
읽으므로 `shared`의 patch가 모든 본문에 닿는다.

공유 학습 `cases` 테이블은 상태와 무관하게 `CASE_LIMIT`(2,000행)에서 새 사례 수집을
멈춘다(`queue_full`). default-teacher namespace의 worker pass는 행 수가 1,800
(`case_rotation.ROTATE_LIMIT`)을 넘으면 teacher 호출 전에 이 프로젝트의
가장 오래된 종결 사례(소비된 gold, `pending_review`, `historical`)를 지워 1,800행으로
맞추고 `learning.rotation`(`count`, `caseRows`, `protected`, `limit`)을 남긴다. 계획은
트랜잭션 없이 읽어 hook의 capture 쓰기를 기다리게 하지 않고, 삭제만 짧은 쓰기 트랜잭션
하나로 한다. 지울 것이 없으면(`nothing_closed`) 계획이 의존하는 개수와 상태의 서명을
namespace의 `case-rotation-status.json`에 남기고, 서명이 같은 동안은 다시 계획하지 않는다.
`nothing_closed`와 계획 실패(`deferred`)도 `learning.rotation` 이벤트로 남는다. 기본 모델 파일이 활성 artifact인데 그 바이트가 바뀌면 scope 권한 검사가 grant를 거절하고, worker pass는 `incumbent_damaged` 결과(`held`, `apply_incumbent_artifact_changed`)를 `project-last-status.json`에 남기며 처음 그렇게 끝난 pass가 `learning.cycle` `held` 행을, default-teacher 프로필이면 `learning.score` 집계도 남긴다. 지우지
않는 것은 종결되지 않은 사례, 최근 gold 128건과 최근 소비 gold 128건, 어느 학습 job도
소비하지 않은 gold, 판정 전 job(미학습, 열린 후보, apply journal의 cycle, 가장 새 적용
후보)의 구성원과 평가 사례, 활성 checkpoint cycle과 가장 새 job의 구성원, 다른 프로젝트의
행이다. 삭제 행 수가 계획과 다르거나 상태 파일을 읽지 못하면 아무것도 지우지 않고 다음
pass에 다시 계획한다. private `samples`와 gold ledger는 그대로 둔다. 명시 프로필과
default-local namespace는 순환하지 않는다. `seam._store`가 `queued`·`duplicate`
밖의 결과를 받으면 `learning.capture.shared` 이벤트를 남긴다. worker pass는 자원 수락
미룸 시 `resource-defer.json`의 안쪽 사유와 관측 수치를 `learning.cycle`(`deferred_resource`)에
남기고, 학습이 끝나면 `learning.cycle`(`trained`)을 남긴다. worker 자신의 자원 점검이나 scope inventory가 teacher와 cycle 앞에서 pass를 미루면 `deferredBy: worker_admission`인 `deferred_resource` 행을, 막힌 job(`blocked`)을 만난 pass는 `learning.cycle`(`blocked`, 사유와 실패 횟수)을 남긴다. pass 밖의 무엇이 바뀔 때까지 매 pass 같은 답이 나오는 결과(`project_worker.HELD`)는 처음 그렇게 끝난 pass가 `learning.cycle`(`held`, 결과가 `reason`)을 남긴다. 결과 상태 전체와 분류는 `docs/dev/learning-state.md`에 있다. 학습 실패 3회로 막힌 job은 24시간 뒤 같은 id·구성원으로 다시 `queued`가 되고(`learning.cycle` `rearmed`, 실패 횟수 0, `attempts`는 그 job의 누적 학습 시도 수), 다시 해도 똑같이 실패할 사유(`learning_queue.UNREPEATABLE`)는 재무장 없이 그 사유로 즉시 닫으며, 구성원이 바뀌어 막힌 job(`source_revoked`·`already_consumed`)은 그대로 두고 다음 계획이 다른 구성원을 고정한다(구성원이 같으면 막힌 그 job을 돌려준다). 학습 cursor가 전진한 뒤의 `trainer_timeout`은 실패가 아니라 미룸(`trainer_progress`)이다. 완료 기록 없는 최종 artifact는 쓰지도 지우지도 않고 cycle 폴더의 `candidate.artifact.uncertain-<해시 앞자리>`로 옮긴 뒤 끝난 cursor에서 복원하거나 다시 학습한다. worker 격리 env에 provider가
더하는 키 목록은 `runtime_admission.PROVIDER_ENV_KEYS`에 고정한다. CLI에는
`--accept-security-policy` 플래그를 두지 않는다.

저장된 recall capture의 후보 출처는 별도 `learning.capture.sources` 진단
이벤트에 기록한다. `request_id`로 기존 표본과 연결하며 후보 id의 SHA-256과
`store`·`python`·`lexical` 값만 담는다. 표본 스키마·학습 입력은 그대로다.
capture가 꺼져 있으면 출처 이벤트도 쓰지 않는다.

학습 사례는 편집 recall에서만 수집한다(sample schema v5, `context_origin: tool_edit`).
질의는 편집이 추가한 텍스트이고 편집 경로는 해시로만 남는다. main checkout 세션이
같은 저장소의 연결 worktree 파일을 편집한 사례도 main 프로젝트의 사례다. 후보는 main
view의 pool이고, capture 자식은 worktree의 프로젝트 root를 route로 다시 증명한 뒤
(`edit_checkout_unproved`) 그 root에서 파일 해시를 읽는다. git 무시는 worktree와 main
양쪽에서 검사해 어느 한쪽이 무시하면 저장하지 않고(`edit_git_ignored`), 한쪽이라도 git이
답하지 못하면 저장하지 않는다(`edit_git_check_failed`). worktree settings가 capture를 끄면
`disabled_in_checkout`이다. `learning.capture` 행의 `editTarget`이 `main`·`linked`를 가른다.
연결 worktree를 cwd로 연 세션의 pool은 그 worktree의 view다. 카드 하나라도 main 게시
head와 다르면 private 표본도 남기지 않고 `edit_view_diverged`로 끝낸다.
default-teacher
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
그 호스트 사본은 수집·학습을 하지 않고 recall은 hybrid 순위로 답한다. 1.0.21 번들은
`default-teacher` 경로에서 `queued` 사례만 읽으므로 `first_done`·`answer_parked`·`lineage`·닫힌
평가 행을 건드리지 않는다. 정책 파일은 `goldModel`과 `maxGoldCalls`를 보존하나
기본 teacher는 이를 읽지 않고 호출별 해결 모델을 상태와 `memvane:logs`에 남긴다.

기존 default-local 설치는 selector maintenance가 `default_teacher_enable.py`로 한 번에
바꾼다. intent(`project-teacher-enable.json`)에 대상별 old/new 바이트를 적고 CAS로 쓴다.
intent가 있는 동안 grant·profile·선택 검사가 거절하고 `selector_heal`도 거절한다. 다음 실행이
전제가 유지되면 앞으로 끝내고, 깨졌으면 되돌린다. old도 new도 아닌 대상이 있으면 쓰기를
멈추고 fence를 유지한다. setup skill의 `restore-teacher-enable`이 old 바이트로 되돌린다.
같은 사유의 실패가 5번 이어지면 플러그인 버전이 바뀌거나 복구 명령이 돌 때까지 요청을 멈춘다;
intent 없이 요청만 멈춘 상태에서는 `ENABLE_DEFERRED_NOTICE`를 안내한다. 각 결과는
`learning.teacher` 행(`enabled`, `enable_deferred`, `enable_failed`, `enable_restored`)으로
남는다.

teacher는 학습 worker에서만 돈다(`general_teacher.process_pending_default`, pass당 호출
8건). 사례를 수집한 세션의 호스트 CLI만 쓰고, 번들 tier 표(`teacher_model_tiers.py`)의
모델만 요청하며, 호출마다 격리 사전 점검(`teacher_isolation.py`)을 지나야 보낸다. 호스트
호출·대기·차단기는 `teacher_hosts.py`와 namespace의 `teacher-host-state.json`이 맡는다.
실행 중 guard는 `prequential.admitted_one`으로 해당 사례 한 행만 읽는다. 본문 전송 뒤(`sent=True`)
감시는 자식 RSS·timeout·출력 상한·scope를 유지하고 시스템 전체 준비 상태로 중단하지 않는다.
전송 전 guard의 `ResourceDeferred`와 전송 전 시스템 압박(`gold_system_pressure`)은 본문 없이
`deferred`로 끝내며 `deferred`·`host` 대기는 시도 횟수를 올리지 않는다. 동의 파일을 끝까지
읽지 못한 I/O 오류(파일 없음·link·권한 오류 밖)는 철회가 아니라
`ResourceDeferred`(`teacher_consent_unreadable`)이므로 보관 답을 닫지 않는다. 그런 pass마다
`teacher-host-state.json`의 `consent.unreadable`이 1씩 늘고(`learning.teacher` `deferred`의
`consentUnreadable`) 파일을 읽은 pass가 0으로 되돌린다. 연속 5회부터 SessionStart가 7일에 한 번
라벨링이 멈췄다고 알린다. pass 이벤트는 `preparedHosts`(이 pass가 CLI 경로·격리·모델을 점검한 host 수)와
`preCallMs`(pass 시작부터 첫 전송 전 자원 판정까지)를 싣고, worker 종료로 끊긴 호출의 사례를 닫은
pass는 `pending_review` `interrupted_external_attempt`와 그 `count`를 남긴다.
응답 뒤 guard의 `ResourceDeferred`는 사례를 `first_done`, ledger를
`answer_parked`로 보관하고(`learning.teacher` `parked`), 다음 pass 맨 앞(빈 큐 조기 반환 전)에서
현재 동의·활성·불변·payload 해시·`admitted_one`·보관 답 해시가 맞으면 호출 없이 gold로
확정한다(`reason: parked_answer`). 차단기는 `NOT_HOST_FAILURES` 네 사유(`teacher_scope_changed`,
`gold_capture_changed`, `teacher_cohort_changed`, `gold_case_changed_before_commit`)와
`project_scope_`·`teacher_consent_`로 시작하는 사유를 제외한 `unknown`·`invalid`에서 오른다. 입력은 편집 텍스트와 후보 카드 본문 스냅숏뿐이다. 루트에 `.git`이
없고 상위 work tree 안에 있는 프로젝트는 teacher 동의의 `grantedAtUnixMs`보다 먼저 수집된 v5
편집 사례를 모두 teacher·학습 대상에서 빼고 `historical`(`pre_teacher_capture`)로
둔다(이 규칙은 지우지 않는다). git이 답하지 못하면 그 pass는 부르지 않는다. gold는 holdout 없이 모두 학습에 들어가고,
승격은 학습 cutoff 뒤 사례로 판정한다. 승격 판정에는 grant의 cohort 한도에 더해 비지 않은 gold
평가 사례 10건이 필요하다(`prequential.py`).

default-teacher는 gold가 `minQualityTrainCases`(100건) 전이면 job을 계획하지 않고
(`waiting_fresh_train`), replay 자리는 최근 소비 사례부터 채운다(`recent_replay`). plugin 갱신으로
승인 해시가 바뀌면 `candidate_close.supersede`가 유효한 grant 아래에서 계보(`binding`을 뺀 grant
해시)·`job_family`·base artifact·입력 view·후보 artifact·활성 stage·구성원 행·apply journal·`rejected_cycles`
제외 조건이 모두 맞는 미완 후보를 현재 승인으로 옮기고(`carriedFrom`), 나머지 구승인 staged cycle은
두 프로필 공통으로 `authorization_superseded`로 닫는다(평가가 `running`으로 남은 cycle 포함). 승계
도중의 `ResourceDeferred`는 아무것도 닫지 않고 그 pass를 `deferred_resource`로 끝낸다. 또한 후보 적용이 판정 사유(`candidate_apply.JUDGED`)로
거절되거나 apply journal이 `rejected`로 끝나면 `candidate_close.close_judged`가 `EvaluationQueue.reject`로
두 프로필 모두에서 해당 후보(및 완료된 채 열려 있는 선행 후보 `superseded_by_judged`)를 닫아 재적용을
막는다. 적용 기준은 세 지표(`necessary_recall`·`none_exact_rate`·`irrelevant_injection_rate`) 무회귀와
`exact_rate`가 incumbent보다 큰 것이다(같으면 `apply_quality_not_improved`). 정책 파일의 `targetExact`는
파일에 남고 이 판정에 읽지 않는다. 포인터가 아직 incumbent일 때 적용을 끝내는(`rejected`와 `reason`) 것은
판정 사유, resident가 prepare·activate 요청에 실패로 답한 것, 쓸 수 없게 된 journal, 그 밖의 실패 연속
5회뿐이다(답을 받지 못한 요청 — 닫히는 resident, 갱신 중의 다른 번들 resident — 은 그 밖의 실패다. resident가 후보를 검증·로드하다 실패한 `worker_error` 답도 후보에 대한 판단이 아니므로 그 밖의 실패 `apply_service_error`다). resident가 수락하지 않은 답의 상태 단어는 journal 행·pass 결과·`learning.promotion` `rejected` 행의 `serviceStatus`에 남는다(resident의 고정 단어 중 하나, 그 밖의 답은 `other`). `activation_pending` 행도 이 값을 싣고, 앞선 pass가 닫고 멈춘 후보를 뒤 pass가 보고할 때는 journal에서 읽는다. 답이 아닌 실패는 고정 단어(운영체제 오류 `apply_io_error`, 적용의 사유 목록 `candidate_apply.REASONS`에 없는 문구는 모양과 무관하게 `apply_error`)로만 남고 메시지 원문은 남지 않는다. 연속 실패는 후보를 가로질러 센다: 앞 후보의 journal이 아무것도 커밋하지 않은 채 다른 후보의 차례가 오면 횟수와 backoff가 이어지고, 끝난 journal(적용, 사유 있는 종결)만 그 연속을 끊는다. 포인터를 쓴 뒤의 적용은 자체 종점 없이 pass마다 다시 시도하며, 연속 5회부터 로그에 `activation_pending`이 보이고 소유자의 출구는 이전 모델 되돌리기다. 그 밖의 실패는 backoff 뒤 다시 시도하고(`apply_deferred`와 `failures`), `apply_quality_stale`은
아무것도 정하지 않고 cycle을 `staged`로 되돌려 재평가로 간다. 포인터가 incumbent인 동안은 커밋된 것이 없어
`intent`와 포인터를 쓰지 못한 `prepared`는 다음 시도가 새로 만든다. 포인터가 후보의 것이 된 뒤에는 판정을
다시 하지 않고 정체성(후보 artifact의 해시, journal의 stage·profile digest와 binding)만 대조해 activation을 끝낸다.
그 구간에서는 구성원·평가 사례의 활성, 구성원 행, 품질 준비, 승인 해시를 다시 보지 않으므로 원문 카드 수정이나 승인
갱신이 적용을 `pointer_committed`에 세워 두지 않는다. 그때의 실패는 journal에 `failures`·`reason`으로 남고
다섯 번째부터 답에 `activationPending`이 실리며, 다섯 번째에 `learning.promotion` 행(outcome `activation_pending`)이
한 번 남는다. journal의 `failures`는 `pointer_committed`에서만 0으로 돌아간다(`prepared`에서는 유지). resident가 자기
검증에서 평가가 낡았다고 본 prepare는 응답 코드 `apply_quality_stale`로 돌아오고 worker는 아무것도 정하지 않는다(재평가).
평가 행의 `failures`는 평가 자신의 실패만 센다(`completed`와 `refresh`에서 0). journal이 생기기 전의 적용 실패는
namespace의 `apply-failures.json`(cycle별 연속 횟수와 마지막 사유)에 세어 재평가를 넘겨 유지하고, 다섯 번째에 평가를 그
사유로 닫는다. 파일에 쓰지 못한 실패는 평가 행에 센다. 활성 artifact의 바이트가 포인터의 해시와 다르면(`apply_incumbent_artifact_changed`, 평가와의 대조보다 먼저 본다) 어느 pass도 그 바이트를 바꾸지 못하므로 그 후보의 평가를 첫 실패에서 그 사유로 닫고 다시 평가하지 않는다(`learning.promotion` `rejected` 행 하나). 나오는 길은 소유자의 되돌리기나 artifact 복구다. 이 파일은 캐시다: 없거나 읽히지 않으면 0부터 세고, 쓸 때 열린 후보의 항목만 남기며, 1.x 영속 계약에 들지 않는다.
포인터 뒤의 profile 대조는 플러그인 갱신이 다시 쓰는 파생 필드(`default_renewal_plan.PROFILE_DERIVED`)를 뺀 digest
(journal의 `profile_stable_sha256`. 이 값이 없는 옛 journal은 전체 digest)로 하고, 그 포인터에 다시 준비된 receipt는 journal의
것으로 받아 덮어쓰지 않는다. 그래서 `pointer_committed`에서 멈춘 적용이 갱신을 넘겨도 끝난다. 적용된 모델을 그 앞의 것으로 되돌리는 것은 소유자가 요청하는
유지보수 명령 하나뿐이다(`candidate_rollback.restore`, setup action `restore-previous-model`). 대상은 포인터의 적용 표식이
가리키는 journal이다(살아 있는 것, 또는 뒤 후보의 intent가 서면서 `candidate-applications/`로 옮겨진 것). 그 journal이
`activated`이거나 `pointer_committed`에서 멈췄고 포인터가 그 `new_pointer`일 때, 적용의 두 잠금 아래에서 먼저 journal을
`rejected`(`reason` `rolled_back`)로 닫고 receipt → binding → 포인터를 이전 값으로 쓴다. 포인터 직전에 namespace의 `rollback-announce.json`(계약 밖 파생물)에 그 적용을 적어 두고, `rolled_back` 행을 남긴 요청이나 worker pass가 지운다. 포인터를 쓴 직후 멈춘 되돌리기도 다음 요청이나 pass가 그 행을 한 번 남긴다. 뒤 후보의 적용이 archive에 복사해 둔 채 살아 있는 journal은 두 곳에 있으므로 두 사본을 모두 닫는다(archive 먼저). 닫힌 journal의 후보는 다시 적용되지
않고 worker pass도 그 적용을 재개하지 않으며, cycle 행은 `applied`로 남는다. 따로 기록을 두지 않는다: 중단된 되돌리기는
`rolled_back` journal인데 포인터가 아직 그 `new_pointer`인 상태로 알아보고, 같은 요청이나 다음 worker pass
(`candidate_rollback.resume`)가 이어서 끝낸다. worker가 끝낸 되돌리기도 `learning.promotion` `rolled_back` 행과 유지보수 요청을 내고, 끝내지 못한 pass는 `rollback_pending` 행에 사유(오류의 고정 코드, 없으면 예외 종류 이름)를 남긴 뒤 평가·학습·적용 없이 `rollback_pending` 결과(`held`)로 끝난다. 그 사이 selector 추론은 거절된다. 같은 포인터로 되돌린 적용이 여럿이면 `already_restored`는 적용 시각이 가장 늦은 것을 고른다. 요청 한 번은 적용 하나를 되돌린다: 복원된 모델이 앞 적용의 것이면 같은 요청을 다시 보낼 때 그 적용도 되돌린다. 뒤 후보의 미커밋 journal(`intent`·`prepared`)은
사라지는 포인터를 전제로 한 것이라 결정을 쓴 직후(포인터를 바꾸기 전에) 지우고, 그 후보는 `apply_incumbent_changed`로 재평가에 돌아가 복원된 모델과 다시
비교된다. resident는 다음 요청에서 포인터가 가리키는 세대를 올린다. `candidate_apply.readable`은 적용된 포인터에서 표식의 journal(살아 있는 것, 없거나 다른 후보의 것이면 archive의 것)이 `activated`이고
지금 묶인 receipt가 그 포인터를 가리키는지를 본다(플러그인 갱신 뒤 다시 준비된 receipt도 같은 포인터의 것이면 읽힌다). journal이 생기기 전의 실패는 그 후보의 `completed` 평가에
세어 다섯 번째에 사유와 함께 닫는다. 평가는 사유가 있을 때만 `blocked`가 된다. 학습 전에 구성원 행이 사라진
열린 job과 재무장을 기다리는 job은 `source_revoked`로 닫고 다음 계획이 남은 구성원을 고정한다.
`source_revoked`는 구성원 행이 사라진 경우와 학습 뒤 구성원의 활성 확인이 거짓인 경우 둘 다의 사유이고,
구성원이 그대로면 다음 계획도 그 job을 돌려준다. 완료 기록 없는 최종 artifact의 격리 사본은 가장 새것 하나만 남긴다.

hook은 teacher 전송·격리·tier·전환 모듈을 import하지 않는다. SessionStart 안내는
`teacher_state.py`(stdlib와 kernel만 import)로 `teacher-host-state.json`과 전환 intent의
존재만 본다. `UNSAFE_PATH`(`teacher_cli_unsafe_path`) 안내는 7일 간격, `REST_REASONS`는
`teacher_codex_home_redirected`를 포함한다. 끄기는 setup skill의 `teacher-off`·`teacher-on`(`teacher-switch.json`)
하나다. 환경 변수는 끄기 수단이 아니다. 계약 전체는 `docs/continual-learning.md`의 Default teacher 절에 있다.
