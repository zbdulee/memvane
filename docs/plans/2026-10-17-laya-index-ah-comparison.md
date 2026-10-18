# Laya Index A/H 합성 저장 구조 비교 (2026-10-17)

## 범위와 재현

이 결과는 기존 `published_view`의 CAS, `shadow_index`의 비활성 세대와
`request_view_pin`의 요청 pin 계약을 참고한 작은 **격리 원형**이다.
[비교 스크립트](../../test/fixtures/laya-index-ah-compare.py)는 Python 표준
라이브러리와 SQLite만 사용하고 실행마다 임시 폴더를 만든 뒤 지운다.
원고·운영 DB·QMD API·모델·설치본은 읽거나 실행하지 않는다.

- **A:** control/head/receipt/pin, 공유 content, 세대별 전체 membership을
  한 SQLite DB에 둔다. 런타임 claim queue는 별도 SQLite DB다.
- **H:** 작은 control DB, 공유 content DB, 세대별 전체 membership SQLite
  파일을 사용한다. claim queue는 A와 동일하게 별도다.

이 문서의 **A-int**(아래 표의 A)는 단일 DB **내부에 불변 세대 행을
누적**하는 구조다. [옛 QMD 게시 조사](2026-10-17-qmd-published-view-options.md)의
**A-copy**(매번 전체 QMD DB를 복제한 뒤 pointer를 교체)는 다른 원형이며
수치·원자성 주장을 서로 옮겨 쓰지 않는다.
- 입력은 2 scope, 최초 600 membership 행, 공유 content 480개다. 다음
  세대에서 alpha의 20개를 삭제하고 20개를 다른 해시로 바꾸며 beta는
  유지한다. 제3세대는 준비 상태로 남긴다. 각 구조에 같은 입력·검사를
  적용한다. 실제 FTS/벡터·긴 본문·여러 프로세스는 포함하지 않는다.
- 각 모드는 별도 프로세스에서 5회 순차 실행했다. 프로세스당 30초 wall,
  CPU 10초, 파일당 64 MiB 제한을 걸었다. 스크립트는 관측 RSS 64 MiB,
  종료 시 총 파일 8 MiB/24개 상한을 확인한다. 이 마지막 RSS 검사는
  실행 중 순간 상승을 막는 OS hard limit이 아니다.

각 모드는 `python3 test/fixtures/laya-index-ah-compare.py --mode A` 또는
`--mode H`로 실행한다. 스크립트 자체가 위 자원 제한을 설정하고 JSON
한 줄을 출력한다. 이 비교는 `assert`를 쓰는 개발 fixture이며 `python -O`
실행을 거부한다.

## 동일 판정과 측정

각 실행에서 scope 가시성, 삭제 뒤 이전 pin의 조회 가능성, 늦은 writer의
CAS 거절, 게시·pin·준비 후보의 GC 보호, pin 해제 뒤 옛 membership GC,
게시 뒤 ACK 유실을 가정한 **같은 claim 재실행**과 receipt 기반 조건부
ACK, DB 무결성·membership digest·content 참조 및 clean-process
재열기를 확인했다. H는 중단 뒤 남을 수 있는 orphan 파일의 inventory도
임시 파일로 확인했다. H의 body는 한 번의 `IN` batch로 읽고
세대 읽기 연결을 30회 조회 동안 재사용했다. A는 같은 결과를 DB 내부
join으로 읽었다. `query30`은 범위 내 path 순서의 10개 body를 30회 읽는
시간이며 검색 품질이나 FTS/vec 지연이 아니다.

| 지표, 5회 중간값 | A | H |
|---|---:|---:|
| 전체 elapsed | 12.568 ms | 15.461 ms |
| 후보 준비 | 1.528 ms | 2.816 ms |
| 30회 body 조회 | 0.375 ms | 0.789 ms |
| 멤버십 GC | 0.281 ms | 0.079 ms |
| 관측 최고 RSS 범위 | 16.12–16.45 MiB | 16.64–16.98 MiB |
| 종료 파일 수 | 6 | 11 |
| 종료 파일 총량 | 417,792 B | 397,312 B |

전체 elapsed 범위는 A 11.814–13.998 ms, H 14.889–16.882 ms였다.
파일 수에는 빈 WAL(0 B)과 SHM(각 32,768 B)이 포함된다. A의 GC가
행을 지운 뒤에도 DB의 재사용 가능 page는 남아 있다. H의 이전 세대
파일은 unlink돼 즉시 공간을 돌려준다. 이 작은 한 머신 비교만으로 H의
저장량 우위 또는 A의 조회 성능 우위를 제품 특성으로 확정하지 않는다.
H 세대 파일은 rollback journal이고 control/content는 WAL이다. H는
준비 파일의 fsync·rename·권한 설정도 수행한다. 이 물리 비용과 Python
호출 방식이 섞여 있으므로 수치 차이를 구조 고유의 비용으로 분리할 수 없다.

## 비교에서 드러난 구조 차이와 열린 반례

1. A의 membership·head·receipt는 같은 DB의 한 트랜잭션으로 묶을 수
   있다. **별도 런타임 queue의 ACK는 두 구조 모두 여기 묶이지 않는다.**
   재전달, fencing, 멱등 receipt와 조건부 ACK를 따로 증명해야 한다.
2. H는 임시 generation 파일을 fsync·rename하고 읽기 전용으로 연 뒤
   control pointer를 전환한다. 준비 뒤 pointer 기록 전 crash는 orphan
   파일을, GC의 control commit 뒤 unlink 전 crash는 더 이상 참조하지
   않는 파일을 남길 수 있다. 원형은 orphan을 **목록화만** 하며 정리하지
   않는다. 파일 바이트 seal, 삭제 직전 pin 재확인, 다중 DB 시점 결속과
   power-loss 복구를 구현·검증해야 한다. 권한 0400은 불변성 증명이
   아니다. 현재 원형은 전원 손실이나 동시 프로세스 경합을 주입하지 않는다.
3. 두 구조 모두 논리적으로 **전체 멤버십을 세대마다** 보관한다. A의
   DB 내부 행과 H의 세대 파일 사이의 파일 수/업데이트 비용 차이는
   더 큰 문서 수, worktree 수, 변경률, 긴 pin과 checkpoint에서 재측정한다.
4. 이 GC 검사는 **옛 membership 세대**만 제거한다. 공유 content/벡터
   파생물의 참조 GC, opt-out 철회가 과거 pin·rollback에서 재노출되는
   문제, 모델 지문 교체, 위키 의존 집합은 아직 해결하지 않았다.
5. A의 단일 DB도 큰 후보 stage 동안 writer lock을 오래 잡으면 동시
   `pin()`을 지연시킬 수 있다. H는 별도 파일에서 준비하므로 그 경합을
   줄일 여지가 있지만 이 원형은 동시 writer/reader를 측정하지 않았다.

복구 절차의 현재 범위는 다음과 같다. A는 단일 DB를 다시 열어 head와
세대 행, `quick_check`, membership digest, content 참조를 검사한다.
H는 control과 content DB를 검사하고, control에 등록된 각 세대 파일의
행 digest와 content 참조를 대조하며, 등록되지 않은 `g*.sqlite`와
미완료 임시 파일을 orphan 목록으로 보고한다. 둘 다 별도 queue에 ACK가
남았으면 receipt를 근거로 같은 claim을 재처리한다. **누락된 게시 파일,
손상된 DB, 전원 손실 뒤 불확실한 fsync 순서, 복구 중 새 writer 경합에서는
fail closed 정책과 수리 절차를 아직 구현하지 않았다.** orphan은 자동으로
삭제하지 않는다.

## Claude Opus 지정 독립 재검토

Claude Code 2.1.292를 `--model opus`로 지정한 무도구·무세션 요청이
성공 응답을 돌려줬다. 검토자는 **최종 구조 결정 보류, 현재 첫 원형은 A**를
권고했다. 초기 스크립트의 H 연결 재열기·본문 N+1 조회와 A의 중복 인덱스가
수치를 왜곡한다고 지적해, 위 측정 전에 H의 읽기 연결 재사용·배치 조회와
A의 중복 인덱스 제거를 적용했다. 리뷰 응답 JSON 자체에는 모델 ID 필드가
없었다. 직후 같은 `--model opus` 옵션의 최소 확인 호출은 성공했고,
`modelUsage`에 **`claude-opus-5-5`**가 기록됐다. 따라서 Opus 가용성과
별도 호출에서의 선택 모델은 확인됐지만, 리뷰 호출의 실제 내부 모델 ID는
응답 메타데이터만으로 독립 확인하지 못했다.

검토자가 찾은 `publish()` 예외 시 열린 트랜잭션, 같은 claim의 미실행
재시도, NULL head pin, content 참조 누락은 원형에서 고쳤다. H의
직접 최종 파일 쓰기도 임시 파일+fsync+rename으로 바꾸고 orphan 탐지를
추가했다. 다만 orphan의 자동 정리, 공유 content GC, 동시 pin/GC 경합,
전원 손실 내구성은 **미해결**로 둔다.

후속 수정 **완료 뒤의 비교 스크립트**도 `--model opus`로 독립 재리뷰했다.
응답 `modelUsage`에는 `claude-opus-5-5`가 기록됐고 **P1은 없었다.**
재리뷰는 A/H 스크립트에 stage·pin·GC 예외 rollback, A의 orphan 검사,
같은 generation stage 재시도, pin 수명 정책의 P2를 지적했다. 검색 전
가시성 검사와 실제 SELECT 사이의 TOCTOU도 파괴적 GC가 들어오면 위험하다.
아래 [A-int 수직 절편](2026-10-17-laya-index-a-vertical-slice.md)은
rollback·stage 멱등성·FK/digest 검사·pin된 한 읽기 snapshot을 별도로
구현·검증한다. pin 만료/보존 정책은 여전히 미정이며 GC 삭제는 없다.
이 A-int 코드와 fixture는 **그 A/H 원형 재리뷰 이후의 새 변경**이다.
이후 별도 Claude Opus 독립 리뷰와 다중 connection/process 합성 검증을
수행했으며 결과와 미해결 사항은 [A-int 절편](2026-10-17-laya-index-a-vertical-slice.md)에 기록한다. 재리뷰 응답은 스크립트
단독을 보고 표본을 1회라고 추정했지만, 위 수치는 각 모드 5회 반복이다.

현재 QMD `scoped-vector-exact.patch`에 대한 같은 검토는 상위 `limit`개
**해시**를 body join에 보내는 논증을 조건부로 인정했다. 동일 해시를
수많은 문서 경로가 공유하면 실제 join 행은 제한되지 않고, 동점 순서와
적격 SQL·최종 join predicate 일치, hybrid downstream의 후보 수 의존은
추가 검증이 필요하다. 2만 청크 전량 exact scan을 운영 기본값으로
채택하지 않는다. 이 원형의 패치를 추가 수정하거나 배포하지 않았다.

## 잠정 권고와 다음 판단

첫 수직 절편은 **A-int를 우선 원형**으로 삼는다. 이유는 head·receipt·멤버십의
원자적 상태 전이를 한 DB에서 증명할 수 있고, H가 추가하는 파일 seal·
orphan 복구·cross-DB 읽기 계약의 비용을 아직 정당화할 규모 증거가 없기
때문이다. H를 폐기하지 않는다. 동일한 FTS/벡터 파생물, 긴 본문과 다수의
서로 다른 문서, 2개 이상 worktree, 게시/조회 동시성 및 crash injection에서
A의 WAL/GC 지연이나 저장량이 실제 병목이면 H를 재평가한다.

결정 전에는 검색 지연 목표, 허용 저장량·파일 수, 보존 세대 수, 활성 pin의
최장 기간, 철회와 학습 재현성의 우선순위가 필요하다. 이 수치는 서비스
성능 또는 마이그레이션 준비 완료의 증거가 아니다.
