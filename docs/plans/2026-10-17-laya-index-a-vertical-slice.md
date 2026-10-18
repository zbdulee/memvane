# Laya Index A-int 첫 합성 수직 절편 (2026-10-17)

**2026-10-18 후속 변경과 TODO:** Claude Opus(`modelUsage: claude-opus-5-5`)의 청킹 구현 초안을 검토해 regex/tokenizer 기반 후보 청킹과 별도 문서 chunk recipe를 추가했다. off-hook 전체 batch 준비는 동일 hash의 파일명별 title 충돌을 모델 호출 전에 차단하고 source를 재스캔한다. 모델 없는 집중 가드 `d2fedc8d5b1448f18808e15713dd2007`은 Node 9/9를 통과했다. QMD 형식의 임시 A-int 후보를 명시적으로 게시·재전달·재개방한 추가 테스트는 Python 26+Node 1(`f1009aa5ea4c45bb9722a3fe22f1086e`) 통과했다. QMD title-only 변경이 원본 digest에 빠졌던 결함을 고치고, 같은 body hash의 title 충돌을 후보 준비에서 거부했다. tagged QMD-origin 전환 리허설을 포함한 집중 가드 `6dc4cd9fcbe9440cb18b0513ce37dc37`은 Node 10/10 통과했지만 제품 route 선택이나 rollback은 연결하지 않는다. 별도 승인된 embedding 기능 smoke는 합성 문서·질의 각 1회, 768차원에서 통과(`af90592d823d482ba8318de9016a010b`)했으나 실제 peak RSS와 제품 성능은 미측정이다. 최종 모델 제외 전체 회귀는 **1,617건 중 1,608 PASS·9 SKIP·0 FAIL**(`608c3a4808a94c1294cb407de09e5c5d`, `passed/settled:true`)였다. 아래 1,601 PASS는 이전 기준선이다. 제품 route와 정식 L1은 차단/`NOT RUN`이다. 남은 릴리스 TODO는 벡터 identity와 title 결속, 모델·native closure의 제품 승인과 청킹 parity, hook 밖 소유 worker 및 durable queue, QMD 제품 route의 첫 쓰기 전후 복구 계약과 독립 queue ID, 정식 marketplace L1이다.

## 구조와 계약

**A-int**는 한 SQLite DB 안에 불변 세대의 **전체 membership 행**, 공유
content/FTS, scope별 게시 head, receipt와 pin을 둔다. 이것은 기존 QMD의
전체 DB 사본을 세대별로 만드는 **A-copy**와 다르다. 비교와 선택 이유는
[A/H 기록](2026-10-17-laya-index-ah-comparison.md)에 있다.

[소스](../../core/laya_index_a.py)는 호출자가 명시한 DB 경로만 사용한다.
현재 hook·설치·QMD daemon·모델·사용자 데이터 경로와 연결되지 않았다.
입력 `scope`는 이미 승인된 프로젝트/worktree/collection의 불변 식별자라는
가정이며 등록·동의 검증 구현은 아직 없다. 각 generation은 한 scope의
전체 membership을 갖고, content는 UTF-8 본문 SHA-256으로 공유한다.
모델 벡터나 실제 파서·청킹은 이 절편에 없다.

- `stage`는 content·FTS·전체 membership·prepared 상태를 한 트랜잭션에
  기록한다. 같은 generation+scope+digest의 재전달만 멱등 수락하고
  다른 입력은 거부한다. membership과 content UPDATE/DELETE는 trigger로
  거부한다. 이 digest는 합성 행 계약이지 외부 원문 출처 증거가 아니다.
- `publish`는 예상 head와 준비 세대를 확인하고 **head+receipt+상태**를
  같은 DB에서 커밋한다. 늦은 writer는 stale로 끝나고 같은 claim의
  재전달은 새 게시 없이 `replayed`를 반환한다. 별도 런타임 queue ACK는
  같은 트랜잭션이 아니므로 receipt 확인 뒤 조건부 처리한다.
- `pin`은 `BEGIN IMMEDIATE`에서 head를 읽고 token을 기록한다. 검색은
  pin을 먼저 확인한 뒤 **한 읽기 트랜잭션**에서 FTS, membership, content를
  읽는다. pin 없는 요청은 거부한다. 새 게시가 기존 pin의 세대를 바꾸지
  않는다.
- **검색 결과 단위는 content hash가 아니라 문서 경로다.** FTS 후보를
  generation·scope·eligible membership과 조인한 **뒤에** score와 path의
  결정적 순서로 `LIMIT`을 건다. 하나의 hash를 다수의 문서가 공유해도
  상위 K는 K개 문서를 반환할 수 있다. 적격성 검사와 body 취득이 같은
  membership 행을 사용하므로 별도의 후보/최종 predicate 불일치가 없다.
  현재 동점 순서는 BM25 점수 다음 `path COLLATE BINARY`다. 이 범위는
  합성 FTS 계약이며 vector/hybrid 랭킹 정책은 미정이다.
- `search_scored`는 외부에서 **사전 계산해 공급한** 해시별 거리만 받는다.
  scope·eligible 문서로 먼저 확장한 후 거리 오름차순과 binary path
  동점 순서로 top-K를 선택한다. 이는 모델을 부르지 않고 vector 후보
  순위의 범위/중복 의미를 확인하기 위한 계약이다. 거리 계산, ANN 후보
  완전성, 모델 지문과 차원은 제공하거나 검증하지 않는다.
- `gc_plan`은 head·pin·prepared를 root로 계산해 이 root 밖의 세대를
  `unrooted_candidates`로 보여주고, 그중 receipt FK가 참조하는 세대를
  `receipt_referenced`로 따로 표시한다. 둘 다 **계획 정보만** 반환한다.
  삭제 함수가 없고 반환값도 실행 권한이 아니다.
  pin 만료, 롤백 보존, 철회, 학습 참조 정책이 정해지기 전에는 세대·content·
  FTS 행을 파괴적으로 수거하지 않는다.

## 합성 증거

[Python fixture](../../test/fixtures/laya-index-a-checks.py)를
[Node test](../../test/laya-index-a.test.mjs)에서 임시 DB로 실행했다.
Python 11건과 Node 래퍼 1건이 집중 검사에서 통과했다. Node 래퍼는
실행된 Python 검사 수와 skip 부재도 확인한다.

1. `target`을 가진 같은 hash를 alpha의 적격 문서 8개, 부적격 문서 1개와
   다른 scope의 200개가 공유해도 alpha의 top-5가 정확히 `a-000`부터
   `a-004`까지 반환된다. 결과 5개가 모두 같은 hash이므로 hash 단위
   top-K의 정답 누락을 직접 드러낸다. 부적격·다른 scope는 섞이지 않는다.
2. g0 pin 중 g1 게시·삭제 후에도 g0 요청은 이전 5개를, 새 pin은 삭제를
   반영한 `a-001`~`a-005`를 본다. g2 prepared와 g0 pin을 GC root로
   보호하고, pin 해제 후 g0는 **삭제 후보**로만 나타나며 행은 남는다.
3. 사전 계산 점수에서 다른 scope의 220개 해시가 모두 더 가까워도
   alpha의 5개 문서가 빠지지 않는다. alpha의 같은 hash 8개와 다른
   hash의 동점 문서가 `a-000`, `a-001`, `a-002`, `a-002a`, `a-003`
   순서로 나온다. 이는 전역 top-K 뒤 scope 필터의 정답 누락과 hash
   단위 top-K의 문서 수 부족을 함께 검출한다.
4. 잘못된 expected의 stale writer, publish의 receipt 직후·head 직후
   fault, 별도 queue의 ACK 유실과 같은 claim 재실행을 확인했다. fault
   뒤 head·receipt는 함께 rollback되고 재실행은 한 번의 게시 효과다.
5. stage 도중 fault는 generation·membership·content·FTS를 모두
   rollback한다. 같은 stage 재전달은 성공, 다른 내용은 충돌이다. pin·
   GC 계획 fault 뒤에도 다음 호출이 가능하며, 검색 오류 뒤 다시 조회할
   수 있다. clean close/reopen 뒤 head·digest·pin 조회도 확인했다.
6. 별도 connection과 소유 자식 프로세스로 오래된 pin·읽기 snapshot 중
   새 게시, 두 writer의 단일 CAS 승자/늦은 writer, 별도 queue ACK 유실 뒤
   receipt 재전달, 짧은 busy timeout과 해제 후 재시도를 확인했다.
7. 자식 프로세스를 미커밋 stage에서는 `os._exit`, 미커밋 publish에서는
   `SIGKILL`로 종료한 뒤 새 connection에서 `integrity_check`, head/receipt,
   내용 rollback과 재게시를 확인했다. 이는 살아 있는 OS에서의 프로세스
   종료 시험이며 OS crash·전원 손실 내구성 시험이 아니다.
8. `KeyboardInterrupt`를 commit 지점에 합성 주입해 트랜잭션/쓰기 락 정리와
   다음 stage를 확인했다. 구현은 `BaseException` 정리, `inspect`의 단일
   읽기 snapshot, 읽기 전용 GC 계획의 deferred 트랜잭션, hash 조인 인덱스,
   WAL mode 확인을 추가했다.

## 이번 회귀와 재리뷰 기록

모델 제외 `npm test`는 저장소 전역 개발 가드에서 파일 병렬도 1로 실행했다.
첫 실행은 시스템 Python의 테스트 전용 PyYAML 부재로 실패했고 가드는
`failed/settled:true`였다. 이미 승인·설치된 테스트 venv의 PyYAML
site-packages만 `PYTHONPATH`로 주입한 뒤 실패 파일 47/47이 통과했고,
전체 재실행은 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드
`passed/settled:true`(run `238180703a9b4338a74319a028fd7515`)였다.
최종 GC plan 명명·fixture 증거 보정 후 다시 수행한 전체 회귀도
**1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드 `passed/settled:true`
(run `53b7eb07cfe94ac5b3458838d2385e57`)였다. 9 SKIP에는 explicit
opt-in이 필요한 실제 모델 검사가 포함된다. 새 의존성 설치·모델 실행은
하지 않았다.

초기 A-int 독립 리뷰와 패치 후 최종 재리뷰 모두 실제 `modelUsage`가
`claude-opus-5-5`였다. 최종 재리뷰는 새 핵심 수정의 정합성 P1을 찾지
못했고, GC 목록의 `unreferenced` 오명칭과 reopen 뒤 head 단언 누락이라는
증거/명명 P2 두 건을 지적했다. 둘 다 코드·fixture·문서에 반영했다.
리뷰는 읽기 전용이며 최종 보정 뒤 재리뷰나 운영 승인으로 해석하지 않는다.

## 다음 작업과 미결정

초기 절편의 검증은 **기능 계약에 대한 합성 검사**였다. 파괴적 GC, 장기 pin의
owner/lease/복구 정책, prepared 후보의 폐기, 학습이 참조하는 view의 보존 범위는
미정이다. 이후 사용자 승인 계약에서 opt-out·색인 제외는 옛 pin까지 즉시 제공
차단하고 물리 삭제는 보류하기로 확정했다. FTS 입력 정규화·안전한
쿼리 parser, SQLite FTS/vector 지문과 model 차원, 여러 worktree 동시
게시, deadline·취소·WAL checkpoint·macOS 전원 손실 내구성, 설치·이전·
실제 host 검증도 수행하지 않았다.

다음 개발 단계는 공개 가능한 다수 문서·worktree fixture에서 scoped
FTS와 사전계산 vector의 **동일 predicate·순위·취소 상한**을 비교하고,
큰 workload의 writer/pin 경합과 복구를 검증하는 것이다. 그 다음에만 위키 카드
provenance 입력과 런타임 queue adapter를 연결한다. 승인된 제공 차단·물리 삭제
보류 정책의 제품 경로 구현, 삭제 범위·기간, 실제 이전/설치는 별도 작업으로 남긴다.

제품 연결 전의 안전 기본값과 결정 항목은 [후속 계약 게이트](2026-10-17-laya-index-a-product-contract-gates.md)에 정리했다.

이 새 A-int 절편은 최종 A/H 비교 원형에 대한 리뷰와 별도로 Claude Opus
(`claude-opus-5-5`)가 독립 리뷰했다. 리뷰의 수정 범위 일부는 반영했지만
아래 미해결 계약은 제품 경로를 열기 전에 결정해야 한다.

- FTS5 `bm25(content_fts)`는 공유 content 전체의 통계를 사용하므로 pin이
  고정하는 것은 membership과 본문 세대다. 다른 scope·준비 세대의 content
  추가가 동일 pin의 점수/순위에 영향을 줄 수 있다. scope별 순위·점수 계약을
  정하고 별도 재현/회귀를 추가해야 한다.
- generation/head/receipt/pin/FTS의 봉인과 scope·state 정합성은 현재
  trigger만으로 완결되지 않는다. `inspect`는 동시성 일관성을 높였지만
  schema 우회·FTS 역색인·본문 해시를 모두 검증하지는 않는다.
- 큰 stage 동안 pin은 SQLite writer lock을 기다리며 현재 timeout은 2초다.
  합성 busy/재시도는 확인했으나 대규모 지연·취소 상한은 미검증이다.
- 외부 queue와 index DB의 ACK는 하나의 트랜잭션이 아니다. 살아 있는 OS의
  재전달 시험은 통과했지만 전원 손실 때 두 저장소의 내구 순서와 복구
  reconciliation은 별도 설계·검증이 필요하다. CAS는 신선도 watermark를
  확인하지 않으므로, 오래된 source로 만든 세대를 새 expected로 다시
  게시하는 경로도 막지 않는다.

## 후속 A-int 계약 구현 (2026-10-17)

위 11건 및 1,584 PASS 회귀 기록은 **이 후속 변경 전** 기록이다. 현재 절편은 단일 DB·전체 membership·공유 content의 A-int를 유지하면서 `core/laya_index_a.py` schema v3, 세대별 FTS 모집단, 기존 pin의 제외 제공 차단, source/base/claim 증거 봉인, read-only GC 계획을 구현했다. 새 `core/laya_index_source_port.py`는 제한된 전체 Markdown 스캔·게시 직전/요청 시 재검사를, `core/laya_index_claim_queue.py`와 `core/laya_index_flow.py`는 별도 합성 DB에서 유지되는 claim·lease·receipt 판정·조건부 ACK를 맡는다. 운영 hook이나 설치된 플러그인은 이 모듈을 호출하지 않는다.

현재 저장소 가드 집중 검사에서 Python **28건**, Node 래퍼 **1건**이 통과했다(run `ebeee1537d8b4c95b7dd60d3a3544888`, `passed/settled:true`). reader pin과 새 게시/GC 계획, 두 writer의 stale CAS, receipt 재전달, 짧은 busy timeout, 테스트 소유 프로세스의 비정상 종료 뒤 reopen/SQLite integrity를 임시 DB로 검증한다. GC는 삭제하지 않는다. 실제 전원 손실, 다른 DB 사이의 원자성, 운영 자료의 정책은 검증하지 않았다. 후속 코드의 첫 전체 회귀는 1,584 PASS·9 SKIP·0 FAIL(가드 `4fcffadc46e44d73a12985329261878e`)였고, 그 뒤 Opus 지적을 수정했으므로 두 번째 전체 회귀도 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드 run `8efe82cbefad45c0a5e330082957d848`, `passed/settled:true`였다. 그 뒤 `inspect()` 수정이 들어간 마지막 전체 재실행도 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드 run `db74d95944fd45f6bdbdb14a6022c5e4`, `passed/settled:true`였다. 모델은 실행하지 않았다.

고정 QMD fork의 격리 사본 `/tmp/laya-fork-build`에서 공식 Bun 1.4.3 플랫폼 패키지 SHA512 확인 후 `/tmp/laya-build-tools/package/bin/bun install --frozen-lockfile --ignore-scripts --no-save`(208개 의존성, exit 0), 이어 `npm run build`(Node v24.21.0, npm 11.19.0, exit 0)가 통과했다. `bun.lock`은 설치 전후 동일했고 원본 source와 설치본은 변경하지 않았다. 결과물은 `/tmp/laya-fork-build/dist/cli/qmd.js`와 `dist/cli/build-info.json`이다. 이는 QMD fork의 TypeScript 빌드 증거이며 Laya Index 제품 패키지/운영 경로의 빌드 증거가 아니다. 의존성 설치와 빌드의 저장소 가드 run은 각각 `b08b33474b144fa3a301b654b484b282`, `196fee54e6c149909164b6ce87430e42`다.

남은 작업은 독립 QA 지적사항 처리, 요청 중 철회 경합과 새 파일 탐지 창의 계약 결정, queue 소유자 사망 증거와 별도 DB의 전원 손실 정책, 크기·지연·FTS 비용 검증, 실제 engine 패키지 및 host 포트, 승인된 제공 차단의 제품 경로 구현과 물리 삭제 범위·기간 결정이다. 최종 이름은 마지막에 정한다.

### 2026-10-18 통합 진행 기록

이 절의 기존 28건 및 1,584 PASS 기록은 새 제품 경계 코드 이전의 기록이다. 최신 A-int/claim queue 합성 집중 검사는 Python **48건**과 Node 래퍼 **1건** 통과(가드 `1243775993aa4947949ca9aa9d88adae`, `passed/settled:true`)했다. 독립 QA의 동일 세대·다른 claim 반복, 열린 reader의 schema fence, 생성 중 header snapshot, FTS 행 누락, source ancestor symlink, dangling sidecar 지적을 수정·검사했다. 후속 Opus 리뷰에서 찾은 metadata 교체, 세대 ordinal, receipt 증거, 직접 API 경로, 제외 뒤 stage 재전달, RO/RW 사이 DB 교체, GC 참조 세대 분류도 보강했다. 별도 queue v2의 불변 DB incarnation 결속을 추가하고, 명시적 runtime은 무바인딩 queue를 거부한다. 이 결과는 임시 SQLite와 소유 child process에서의 동시성·중단 검증이지 실제 전원 손실 검증이 아니다.

새 `laya_index_runtime.py`는 명시적 source/scope/DB/queue 결속을 받는 프로그램 API이며, 집중 검사 Python **6건**과 Node 래퍼 **1건** 통과(가드 `51a1a6b57ab4414796af6682b151f681`)했다. host route 표식이 있으면 기존 QMD로 조용히 되돌아가지 않도록 하는 선택 경계는 최종 호환 수정 후 Claude/Codex/Hermes·통합 **45 PASS·1 SKIP**(가드 `4b6deb76b75f40df8ae9089ecdc1288a`), topical **21 PASS·1 SKIP**(가드 `f865e7181e2148dc9b78bdb09eed0498`)였다. 실제 제품 활성화는 차단 상태다. 합성 migration rehearsal은 Python **28건**과 Node 래퍼 **1건** 통과(가드 `100f8d466a87491ead3f16b3a962ca96`)했으나 실제 QMD 형식의 migrator가 아니다. source-only package/backup은 Python **10건**과 Node 래퍼 **1건** 통과(가드 `1c9253fe860745859b94108594f48e74`)했다. 이 최종 수정 이후 모델 제외 전체 회귀는 **1,600건 중 1,591 PASS·9 SKIP·0 FAIL**(가드 `20c1707d1878449ba7c265f0e9ee0a66`, `passed/settled:true`)이다. 설치 후보 L1 QA는 `NOT RUN`이다.

릴리스 필수 TODO: 독립 설치 소유 journal과 exact-commit 제품 bundle 및 host 응답 adapter를 연결한다. 실제 QMD 형식 입력에 대해 소유권 inventory, 비파괴 백업·복원 대조, 재색인·후보 검증, 원자 선택·복구와 운영 작업 저장소의 claim/ACK 연결을 구현·검증한다. 이어 설치 후보 L1 QA와 변경 후 전체 회귀를 통과한 뒤에만 전환 또는 main 반영을 검토한다. 후속 TODO: 진행 중 철회 경합, source 발견 창과 규모·지연, 보존·물리 삭제 정책 및 파괴적 GC. GC 실행은 지금 비활성이고 제품명 확정은 마지막에 수행한다.

### 2026-10-18 제품 연결 및 벡터 작업 중

위 1,591 PASS와 Python 48건은 **벡터 스키마 v4 이전 기준선**이다. 이후 실제 QMD 2.5.3/고정 fork 형식의 읽기 전용 이관·백업·복원·lexical 후보 준비(Python 19+Node 1, 가드 `9f39defa5c254450b8508080c00a6005`), 전체 플러그인 임시 bundle·설치 journal(Python 6+Node 1, `c06465ff65204efc8497389df4c5161d`), owner-bound sync(Python 16+runtime 6, Node 2, `6e4db41992094cbe8787f6585d53db6c`), 호스트 경계(34/34, `a9e4cd361d5d4f68af25785e046c4a68`), 임시 후보 설치 리허설(Python 2+Node 1, `4f55f941c524446a8ebf0d49a427dedf`)이 각각 가드에서 통과했다. 모든 결과는 합성·임시 입력이다. 임시 후보 리허설의 선택된 hook 시간 231–263ms는 작은 fixture의 관측치다. 정식 marketplace 설치 후보 L1은 `NOT RUN`이다.

실제 형식 이관은 벡터 provenance가 부족해 현재 `cutover_ready=False`이며 게시·선택을 하지 않는다. 단일 DB의 벡터 저장, 세대·scope 한정 후보 검색, 모델 ID·가중치/처리 fingerprint·차원 결속을 v4 새 DB 스키마와 별도 vector 포트에 합성 구현했다. 미리 계산한 합성 벡터로 검증하며, 승인된 운영 embedding provider가 없으면 선택된 route는 차단한다. 릴리스 필수 TODO는 실제 모델·native 의존성 결속, QMD 청킹·검색 동등성, 운영 지연 측정, 실제 이관의 재색인/게시·복구, 정식 L1이다. 파괴적 GC와 물리 삭제는 비활성 후속 작업으로 유지한다.

v4의 첫 집중 검사는 사전 계산한 합성 벡터 Python **8건**·Node **1건**(가드 `e9812e8956414f9c9d64cc940c616171`, `passed/settled:true`)과 A-int Python **51건**·sync **20건**·runtime **6건**·Node **3건**(가드 `a2d703174e514dbfb5c35e8a9aef1346`, `passed/settled:true`)을 통과했다. 첫 벡터 실행 `006a03ea0efc4317a661174a7fe113fb`은 fixture의 pin 반환값·임시 경로 오류로 실패했고 수정 후 재검사했다. 이 기록은 실제 임베딩 모델 실행, 설치 후보 또는 전체 회귀 통과가 아니다.

기존 로컬 provider의 **소스 기본값**은 고정 QMD fork에서 `node-llama-cpp` 3.20.0과 `hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf`다. QMD 2.5.3 재사용 runtime은 `node-llama-cpp` 3.18.1이므로 같은 실행 closure로 간주하지 않는다. 문서는 900토큰/135토큰 overlap의 tokenizer 기반 청킹 뒤 `title: … | text: …`, 질의는 `task: search result | query: …` 형식을 사용한다. QMD의 6자리 처리 지문은 모델 가중치 hash가 아니다. 로컬 GGUF adapter의 코드 구현은 진행 중이나, 실제 파일 hash·dependency closure·차원·레시피와 추론 자원은 아직 증명하지 않았다. 모델 실행·다운로드는 수행하지 않는다.

**선택 상태의 읽기 전용 메타데이터 확인(2026-10-18):** 플러그인 shared QMD의 선택된 `index.yml`에서 embed 식별자는 위 `embeddinggemma-300M-Q8_0.gguf`와 일치했고, 선택된 플러그인 cache에 같은 이름의 일반 파일 **328,576,992바이트**가 있었다. 현 세션의 `QMD_EMBED_MODEL` 환경 변수는 설정되지 않았다. 선택된 managed QMD는 2.5.3, 설치된 `node-llama-cpp`는 3.18.1이며 패키지의 타입 선언에 `build:"never"`·`skipDownload` 옵션이 있고 macOS arm64 Metal 패키지와 `llama-addon.node` 파일이 있었다. `otool -L`의 읽기 전용 관찰에서는 addon이 Metal llama/ggml dylib 세 벌을 참조하고, 이 파일들은 `libggml-base.dylib`와 시스템 라이브러리를 참조했다. 별도 일반 QMD 설정도 같은 embed 식별자이나 그 기본 cache에는 모델 디렉터리가 없었다. 이는 파일 목록·크기·설정 scalar·native link 메타데이터만 읽은 결과로, 모델 내용·운영 DB·원고·인증 비밀은 읽지 않았다. 모델 SHA와 전체 native closure의 hash·소유권 증명, 차원, 실제 로드/추론, 지연은 여전히 미검증이다.

코드 전용 GGUF bridge의 최종 모델 없는 합성 검사는 벡터 Python **9건**·provider **8건**·Node **2건**(가드 `aba569d224274137adaac5d52734034a`, `passed/settled:true`)을 통과했다. 명시 로컬 GGUF·Node·모듈 경로/해시를 받으며 HF resolver·cache 탐색은 하지 않는다. Opus 수정 검토에서 실행 코드·의존성 해시의 profile 결속과 엄격한 UTF-8 처리, native loader의 `build:'never'`·`skipDownload:true`를 보강했다. 실제 `modelUsage`는 `claude-opus-5-5`였다. 그러나 manifest의 dependency closure 승인은 호출자 선언일 뿐 검증된 제품 attestation이 아니다. 실제 GGUF hash, QMD 2.5.3의 `node-llama-cpp` 3.18.1 옵션/전처리 호환성, QMD 900/135 tokenizer 청킹의 동등성, 상시 적재 provider와 22초 hook 예산의 cold/warm 지연은 증명하지 않았다. 현재 bridge는 전체 문서 하나만 임베딩하므로 QMD 문서 청킹 기능과 동등하지 않다. 별도 `model-test` opt-in과 검토된 GGUF/runtime·자원 envelope 없이는 실제 추론을 실행하지 않으며 제품 route 승인을 하지 않는다.

이 최종 v4 코드의 모델 제외 전체 회귀는 **1,610건 중 1,601 PASS·9 SKIP·0 FAIL**(가드 `8c4c845fede54f37996632128eb5a9ef`, `passed/settled:true`)이다. 첫 v4 전체 실행의 package/backup fixture 실패는 schema v4 상수와 필수 route 증거로 고친 뒤 집중 재검사를 거쳐 재실행했다. 9 SKIP에는 명시적 opt-in 모델 검사가 포함된다. 정식 marketplace 설치 후보 L1은 `NOT RUN`이며, 이 회귀는 제품 활성화나 실제 전원 손실의 증거가 아니다.

### 후속 Opus 리뷰와 남은 제한

실제 `modelUsage`가 `claude-opus-5-5`인 세 범위별 읽기 전용 리뷰가 schema/stage, source/queue, publish/search/GC를 검토했다. 첫 37KB 단일 호출은 240초 시간 초과되어 결과로 취급하지 않았다. 지적받은 동적 FTS 객체 이름 우회, meta/head/receipt 봉인, stage 멱등 시 FTS 확인, 별도 queue의 deferred lease 정체, `inspect`의 head/receipt/pin 상태 검증, scored 조회 변수 상한과 비정규 철회 입력을 수정하고 새 합성 회귀를 넣었다. 최종 수정 부분의 추가 Opus 재리뷰에서 `inspect()`의 열린 연결 schema 재검사와 receipt proof 대조 누락 두 건을 찾아 보강했다. 직접 SQL로 수정한 FTS 내부 역색인의 완전한 읽기 전용 검증, 별도 DB 전원 손실, 철회와 진행 중 출력 사이 경합, 원문에 새 파일이 생기는 미탐지 창, queue 저장소의 독립 schema/object 검증은 남는다. 물리 삭제·GC 실행은 계속 비활성이다.
