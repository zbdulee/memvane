# MemVane 0.43.39 PR5 reader bridge 검증 상태

현재 작업은 v5 journal의 reader bridge와 기존 v6 게시 세대의 read-only 검색을 다룬다. schema3 후보는 내부 read-only capability로만 검증하며 운영 선택은 허용하지 않는다. PR6 migration과 PR7 view 선택은 이 작업의 완료 증거에 포함하지 않는다.

- v4에서 v5로 넘어갈 때 등록된 Claude·Codex host의 bridge preflight를 요구한다. 이미 실행 중인 구버전 recall 전체를 배제한다고 주장하지 않는다.
- 선택된 v6 reader는 base·catalog·binding·sidecar의 고정 snapshot과 증명된 revision을 사용한다. 전환 전 legacy sidecar의 completed 상태는 endpoint에 봉인하며 첫 writer는 그 baseline과 일치하는 상태에서 receipt를 발급한다.
- 첫 sidecar 문서 게시 전 base에 불변 의존성 표시를 기록한다. 게시된 sidecar가 없으면 검색과 빈 sidecar 재생성을 거절한다.
- host cache의 bridge preflight는 설치 commit과 marketplace source를 대조하고 기존 exact-commit adoption으로 cache 봉인을 확인·복구한 뒤 등록 hook을 관측한다. 예상하지 못한 metadata와 다른 commit은 거절한다.
- 기본 Python 3.9에서는 승인된 기존 DB의 `mode=rw` 연결 직후 `query_only=ON`을 사용한다. SQLite 자체 WAL/SHM 초기화는 허용하고 optional DB 본체의 새 생성과 논리 데이터·세대·journal 변경은 허용하지 않는다.
- upgrade fence와 이동 복구 중에는 증명된 endpoint의 read-only 검색만 허용한다. T3 물리 연결·writer·rollback drain, T6 복구, T7 host 분기 검사는 합성 설치와 실제 SQLite/프로세스 경계의 범위에서 기록한다.
- fence 중 새 릴리스 companion은 owner와 전체 번들·실행 코드 일치가 증명된 경우 검색한다. 분류 직후 fence가 해제된 요청도 journal과 endpoint를 다시 증명해 검색한다.
- upgrade 진단은 작업별 불투명 ID·시도·고정 단계와 사유를 기록한다. 새 프롬프트 원문과 extractor stderr 원문은 기록하지 않는다.
- Hermes recall classifier는 upgrade fence의 대기와 subprocess 시작 시간을 포함한 3초의 유한 예산을 사용한다. 손상된 route와 임의의 classifier 실패는 계속 거절한다.

## 검증 상태

최종 HEAD, 동일 커밋의 전체 3개 shard, 독립 리뷰, 실제 Claude/Codex host preflight·postflight와 설치·운영 증거, 마지막 CI는 아직 확정되지 않았다. 같은 커밋 트리의 3개 shard 순차 전체 통과를 canonical `npm test` 완료 근거로 인정한다. `.38` 운영 전환의 Codex preflight는 새 cache 미봉인으로 실패해 기존 journal v4를 유지했다. 개별 RED/GREEN과 guard ID는 `PROGRESS-CODEX.md` 및 해당 로그를 기준으로 확인한다.

합성 10-view×1,800 member 검사의 최신 warm pin/search p95는 약 3,086ms/2,489ms다. PR7/10의 목표 50ms/150ms는 미달이며 실제 host 지연과 동일시하지 않는다. 초기 합성 capacity 실행에서는 24회 wake 내 게시하지 못했지만, 입장 예산 변경 후 실제 SQLite 기반 합성 worker는 게시됐다. publish-cut 관측은 단일 실행이어서 p95·head CAS 상한의 증거가 아니다.

canonical `npm test`는 1,800초 상한으로 중단됐다. 중단 전 발견한 catalog timeout fixture를 수정한 뒤 관련 회귀와 shard 2는 통과했다. `0.43.38` shard 1에서는 Hermes fence 사례가 실패했고, classifier 시작 지연과 2초 한도의 충돌을 별도 재현해 수정한 뒤 관련 focused 검사가 통과했다. 최초 shard 실패의 예외는 확인되지 않았으며 이 재현과 동일 원인이었다고 단정하지 않는다. 각 결과는 다른 코드 시점의 증거로서 최신 코드 전체 통과로 합산하지 않는다.

최신 shard 3은 singleton version handshake timeout과 비활성 view 계보 검사의 기본 검색 deadline 초과로 실패했다. singleton 재실행은 통과했지만 최초 timeout 원인과 이번 브랜치의 시간 영향은 미확정이다. 계보 의미 검사는 제품 기본 2초를 바꾸지 않고 테스트 호출의 예산을 늘려 재검증했다. 두 결과 모두 원래 실패를 지우거나 전체 통과를 증명하지 않는다.

전환 당시 부재였던 sidecar의 최초 게시 후 유실과 빈 DB 재생성 경로를 합성 설치에서 재현하고, reader 거절 및 재생성 거절을 검사했다. 최종 독립 리뷰는 아직 완료되지 않았다. `review`·`hash`는 모델 자원 제한을 쓰지 않고 전역 guard의 잠금·프로세스 감시·시간 제한을 유지한다.

fork 자식의 -11 종료는 별도 미해결이다. 기존 백업·QMD origin rollback DB의 자동 정리는 비활성이고 실제 삭제는 별도 사용자 요청이 필요하다.
