# Laya 학습 runtime 부하·재개 제품 게이트 (2026-10-18)

이 문서는 실제 학습 실행 승인이나 운영 자료 접근 지시가 아니다. 검색,
embedding, 학습과 Laya Index 이관은 모두 설치된 프로젝트 안의 기능 목표다.
학습의 자원 대기·중지·재개는 우선 게이트다. 검색·embedding 작업에 별도 부하
대기열을 확대할지는 v2를 완전히 연 뒤 실제 사용 양상을 보고 결정한다. 두
기능 자체의 정확성 게이트는 유지한다.

## 기존 구현과 확인할 경계

- `core/context_learning/laya_adapter.py`는 기존 로컬 Laya 체크포인트를
  읽기 전용으로 로드하고 encoder를 고정한 뒤 선택 head만 SGD
  (learning rate 0.01, momentum 없음)로 갱신한다. v2 승인 모델의
  encoder는 `jhu-clsp/mmBERT-base`, 1024/256 설정이다. optimizer의
  유효 그룹은 2쌍이고 자원 admission은 microbatch를 2에서 1로 줄일 수
  있다. 학습 후보의 생성은 활성 모델 포인터 변경과 별개다.
- `learning_queue.py`는 신선한 증거별 중복 소비를 막고 queued/running/
  deferred/blocked/staged 상태, 다음 재시도 시각, 소유 claim을 보존한다.
  `runtime_work.py`의 내구 wake와 `resource_coordination.py`의 UID
  lease/OS 관측이 실제 dispatch를 제한한다. 학습 중 stop pipe와
  `training_cursor.py`의 두 head slot·그룹 계획·RNG receipt는 완료한
  optimizer 그룹부터 재개하도록 설계돼 있다. 이 관계의 설치 후보
  end-to-end 증거는 아직 없다.
- 정상 v2 승인 cohort는 실행 시작에 신선한 train 5건 이상을 요구한다.
  품질 판정은 train 100건/5 family, validation 50건/5 family와
  빈 결과 validation 10건을 요구한다. 합성 1쌍 모델 시험만으로 자동
  학습·품질·적용을 통과했다고 말할 수 없다. 기존 수집/동의·소스·
  설치 adapter 버전·모델 identity가 모두 맞아야 한다.

## 학습 dispatch와 runtime 우선순위

`shared.launch`는 학습 worker를 hook에서 직접 시작하지 않는다. 선택된
profile은 학습·평가 요청만, 일반 프로젝트와 legacy source worker는 하나로
합쳐지는 full-worker 요청 파일을 남긴 뒤 `runtime_work`에 `learning` wake를
요청한다. dispatcher의 `runtime_handlers.learning`은 `worker_lease` 아래에서
`learning_worker_step`으로 소유 자식 프로세스만 시작하고 즉시 돌아오며, 그
요청이 끝날 때까지 wake를 유지한다. wake 우선순위는 `index`(0), `update`(1),
`cleanup`·`laya-sync`·`laya-compile`(2), `learning`·`auto`(3) 순서이고, 공유
compute admission은 `learning`·`auto` wake만 기다린다
(`runtime_wake_policy.KINDS`, `needs_compute_admission`). 이미 실행 중인 학습
자식과 index/update 작업이 실제 OS 자원을 두고 겹치는 경우의 부하 특성은
측정되지 않았다.

`shared.status`는 학습·평가 큐의 상태별 개수, 완료·staged가 아닌 행의 사유
집계와 다음 재시도 시각을 본문 없이 보고한다.

## 실제 학습을 제안하기 전 필요한 증거

첫 한정 학습 시험을 별도로 제안할 때는
설치 후보의 정확한 adapter/체크포인트/모델 파일 해시와 승인된 합성
입력, 동의·v2 grant 및 만료, base artifact와 후보 출력의 소유 경로,
모델 input view, OS별 device·메모리 프로필의 출처를 먼저 고정한다.
정상 pressure·fresh observation, 설정의 절대 메모리 reserve와
`maxRssMib`, load limit, 최소 microbatch 1의 추정 peak 적합성을
확인한다. 35% 가용 메모리는 확대 목표이지 최저 하드웨어 요구치가
아니다. 추정치는 실제 peak 측정으로 대체하지 않는다.

그 뒤에만 명시적 opt-in으로 `--kind model-test` 단독 개발 가드와
제품 runtime admission을 **둘 다** 적용한 짧은 합성 한 그룹 trial을
별도 범위로 심사한다. 전원/OS crash를 시험했다고 말하지 않는다.
프로세스 전체의 CPU/RSS peak, MPS 사용량, swap/pressure/load,
소유 child 정리, stop 지연, 재시작 cursor 및 최종 receipt를 관측
계획에 포함한다. 가드 기본 5초 샘플 사이의 peak가 빠질 수 있으므로
자식 내 계측 또는 더 좁은 관측이 준비되지 않으면 자원 상한 검증으로
승격하지 않는다. 운영 원고·학습 DB·활성 모델 포인터는 사용하지 않는다.
실제 학습의 실행 승인·입력·자원 envelope는 이 계획과 별도로 필요하다.

정식 릴리스에서는 `docs/release-agent-qa.md`의 RQA-RES-02를 설치
후보에서 통과해야 한다. 모델 없는 합성 테스트, embedding
기능 smoke, 또는 다른 버전의 전체 회귀는 그 gate를 대신하지 않는다.
