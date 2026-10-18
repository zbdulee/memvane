# Laya Index: 내장 SQLite 검색·색인 엔진 방향 (2026-10-17)

## 결정과 범위

이 프로젝트의 장기 검색 기반은 QMD 배포물의 최소 패치나 upstream PR 수락에
의존하지 않는 **프로젝트 내부 엔진 Laya Index**로 설계한다. `Laya Index`는
외부 레지스트리 이름 충돌·상표 검증 전의 내부 작업명이고, 임시 패키지
식별자는 `laya-index`다. 외부 게시나 전체 프로젝트 최종 리네임은 아직 하지
않는다. **Laya Index는 검색·색인 구성요소이며 Laya 학습 모델·trainer·checkpoint와
다르다.** QMD는 출발 코드와 비교 기준이다. 현재 `qmd-fork-source-f4633f9`의
scoped vector 수정은 후보 알고리즘을 검토하는 격리 원형이지 배포 결정이 아니다.

최종 전환 목표는 **이 프로젝트가 호출하는 QMD backend를 Laya Index로 교체**하는
것이다. 사용자가 별도로 설치한 global QMD 자체는 삭제·업그레이드·비활성화하지
않는다. 엔진 전환은 기존 색인·설정의 이관, 무결성 검증과 실패 시 rollback
경로가 준비된 뒤에만 검토한다. 플러그인이 독립 설치·관리한 QMD runtime은
전환 검증 후 [소유권이 증명된 정리 절차](2026-10-17-embedded-engine-data-migration.md)에
따라 후속 정리한다. 소유권이 불확실한 파일이나 공유 파일·모델은 삭제하지 않는다.

이번 단계는 설계와 격리 합성 절편이다. 설치된 QMD 2.5.3, 기존 플러그인,
사용자 설정·원고·위키·색인·학습 DB·모델·pointer·hook은 변경하지 않는다.
격리 개발 폴더에서 공급 출처·script를 확인한 고정 lockfile 의존성 설치만 후속 승인되었다. 제품 모델·검색 API 실행, 외부 fork·push·배포는 하지 않는다.
Claude CLI의 연결 진단과 뒤이은 독립 설계 검토는 아래에 구분해 기록한다.
이 설계는 **Claude와 범위별 독립 제안·반례·트레이드오프 검토를 마친 뒤에도
물리 저장 구조와 운영 게이트가 미확정인 제안**이다. 대규모 구현이나 운영
적용이 완료됐다는 뜻이 아니다.

## 식별자와 데이터 경계

| 항목 | 제안하는 내부 계약 | 현재 상태 |
|---|---|---|
| 소스 경계 | 플러그인 저장소의 `engine/` 아래 별도 패키지. 내부 작업명 `Laya Index`, 임시 패키지 ID `laya-index`; 최종 제품·공개 패키지 이름은 미정 | 미구현·외부 미게시. QMD의 `@tobilu/qmd`를 같은 이름으로 재배포하지 않음 |
| 코드 버전 | 독립적인 `0.1.0-dev`에서 시작하고 엔진 자체 호환·변경 기록을 둠 | QMD의 2.5.3/2.8.3 버전 번호를 승계하지 않음 |
| 저장소 형식 | `laya_index_schema_version=1`을 엔진 DB에 명시. 코드 버전, 파일 포맷 버전, 검색 세대 ID를 별개 필드로 관리 | QMD SQLite 스키마와 버전 번호 공유 안 함 |
| 새 사용자 데이터 경로 | 후보는 사용자 전용 `~/Library/Application Support/laya-index/v1/`. 경로는 설정·설치 포트에서 주입하며 임시 검사에서는 별도 root만 사용 | 기존 QMD 경로와 병행. 실제 생성·전환은 후속 마이그레이션 |
| identity | 안정적인 프로젝트 등록 ID + 별도 worktree incarnation ID + 승인된 collection/role. branch 이름이나 moving HEAD는 identity가 아님 | 승인 범위 밖 경로는 자동 등록하지 않음 |

이 식별자는 현재 계획에만 사용한다. 외부 이름 검증 전에 패키지를 게시하지
않고, 이후 제품 이름을 정해도 이미 발행한 스키마·데이터 경로를 조용히
바꾸지 않는다.

## 여섯 영역과 엔진 포트

| 기존 영역 | 소유하는 결정·상태 | 엔진과의 경계 |
|---|---|---|
| 프로젝트·파일 동기화 | 승인된 루트의 현재 파일 열거, 읽기 실패·삭제 판정, raw change set과 snapshot | `stage_changes(project, worktree, collection, content/scope revision, changes)`로 증거 전달. 검색 세대나 위키 카드 작성은 소유하지 않음 |
| 색인·검색 **내장 엔진** | 아래의 멤버십, 공유 콘텐츠·파생물, FTS/벡터 저장, 후보 준비·검증·게시, 요청 pin, 검색 점수·범위 | `prepare`, `publish`, `pin_query`, `release`, `inspect`의 좁은 포트. 현재 `qmd_route`·`qmd_project_query`는 전환 기간의 QMD adapter로 남음 |
| 위키 | 원문 revision·출처·동의·비용·카드 생성/검증/게시 | 게시된 카드와 전체 의존 집합을 엔진에 색인 입력으로 전달. 카드의 진실성이나 유료 extractor 여부는 엔진이 결정하지 않음 |
| 학습 | capture 동의, label/평가, 모델 checkpoint·승격, 사유 DB | 엔진이 게시한 corpus/view 지문과 검색 후보만 읽음. 엔진은 학습·모델 적용을 시작하지 않음 |
| 설치·마이그레이션 | 엔진 패키지·스키마 호환성, 후보 준비 증거, 경로 전환 journal·rollback 승인 | 엔진의 `inspect`/`prepare` 증거를 소비하고 실제 선택을 조율. 검색 랭킹이나 사용자 문서 내용은 소유하지 않음 |
| 런타임 조율 | wake queue, 작업 우선순위, 자원 lease, endpoint·모델 프로세스 수명 | 승인된 엔진 작업을 실행·취소하고 ACK. 특정 세대가 검색 가능하다는 판정이나 query snapshot은 엔진이 소유 |

호스트 훅(Claude/Codex/Hermes)은 이 여섯 영역의 진입 adapter다. 새 엔진은
호스트 세션·위키 extractor·학습 권한을 직접 알 필요가 없다. `recall.py`는
한 요청에서 엔진 포트 또는 이전 QMD 포트 하나만 택하고, 결과의 출처와
세대를 함께 받은 뒤 기존 주입 정책을 적용한다. QMD와 새 엔진의 결과를
한 요청에서 섞지 않는다.

## 엔진 내부 소유권

1. **worktree membership:** 승인된 `(project ID, worktree incarnation,
   collection, relative path)`가 어느 content revision을 볼지 엔진 registry가
   소유한다. 후보 세대에는 그 worktree의 **전체 멤버십**을 기록한다. 삭제·rename은
   새 view에서 빠지고 다른 worktree의 같은 콘텐츠는 유지된다. 스캔 오류는 삭제가
   아니다. 신규 worktree와 외부 경로는 설치·동의 절차 없이는 자동 등록되지 않는다.
2. **공유 content와 파생물:** 원문 바이트 해시로 불변 콘텐츠를 식별하되,
   파서·청킹 설정·임베딩 모델/차원·전처리 버전을 파생물 키에 포함한다. 본문과
   FTS/벡터 파생물을 승인된 worktree 사이에 재사용할 수 있다. path별 제목·출처는
   멤버십 쪽에 둔다. GC는 게시 view와 진행 중 pin의 참조가 모두 사라진 뒤
   실행한다. 다른 프로젝트가 참조 중인 해시를 삭제하지 않는다.
3. **검색 범위:** FTS5와 벡터 후보 모두 요청의 게시된 멤버십을 **점수 정렬·LIMIT
   전에** 적용해야 한다. 전역 상위 K를 뽑은 뒤 worktree/collection을 거르는
   방식은 금지한다. 정확 스캔, scope별 벡터 구조, 적격 집합 기반 ANN 중
   구현 방식은 성능·메모리·취소·정확성 실측 후 정한다. 현재 2만 청크 원형의
   약 430 ms는 무제한 정확 스캔을 제품 기본값으로 삼을 근거가 아니다.
4. **세대 게시:** writer는 비활성 후보에만 쓰고, 내용·scope·모델 지문과
   FTS/벡터 완전성을 검증한다. 게시 전 영속 receipt와 queue claim의
   동일 identity를 기록하되, 서로 다른 SQLite DB에 있는 현재 런타임 큐와
   게시 pointer/receipt의 **단일 트랜잭션 원자성을 주장하지 않는다.**
   재전달 가능한 claim, fencing과 멱등 receipt, 게시 후 조건부 ACK 및
   불확실 ACK의 재조회로 효과를 맞추는 계약을 검증해야 한다.
   예상 세대·등록 revision을 CAS로 비교한다. 한 요청은 하나의 불변 view와
   SQLite 읽기 snapshot을 pin해 FTS/벡터/본문 조회에 공통 사용한다. 늦은
   writer의 이전 세대 쓰기와 불확실 ACK는 거부·보류한다. 게시 후 이전 view는
   진행 중 요청이 끝날 때까지 유지한다. pointer 변경만으로 완료라 하지 않는다.
5. **실패 경계:** 모델 준비는 런타임의 별도 승인·자원 게이트를 통과해야 하며,
   엔진은 승인된 벡터 공급자와 모델 지문만 받는다. query deadline·취소·RSS
   상한, SQLite WAL/checkpoint와 crash 복구는 엔진 구현의 검증 항목이다.
   범위 판정이나 snapshot을 증명할 수 없으면 다른 범위의 결과로 대체하지 않는다.

기존 `worktree_query_slice.py`와 `worktree_wiki_membership.py`는 멤버십/CAS
합성 계약의 출발점이다. `shadow_index`·`shadow_ack_policy`·
`request_view_pin`은 실패·ACK·pin 회귀를 제공하지만 현재 QMD DB 복제와
endpoint에 결합된 부분을 그대로 제품 엔진 스키마로 들여오지는 않는다.

## QMD 원형의 채택 범위와 비목표

- **검토·재사용 후보:** SQLite 저장과 FTS5, 문서/청크·벡터 생성의 일부 순수
  알고리즘, 검색 평가 fixture, MIT 허용 범위의 출처 표시된 코드. 실제 채택
  코드는 파일별 provenance와 수정 내용을 기록하고 upstream `LICENSE` 및
  저작권 고지를 보존한다. 의존성 라이선스도 별도 점검한다.
- **교체 대상:** QMD 전역 선별 후 scope 필터, 활성 DB 직접 update/embed,
  단일 endpoint와 QMD collection identity에 묶인 게시, QMD 내부 DB를 사용자
  데이터의 유일한 계약으로 취급하는 방식. `qmd-fork-source-f4633f9`의 전체
  scoped exact scan은 정확성 원형으로만 유지한다.
- **비목표:** `qmd` CLI·SDK·MCP 응답 형식, `@tobilu/qmd` 패키지명/버전,
  QMD 내부 SQLite 스키마와의 영구적인 호환. 필요한 upstream 버그·보안 수정은
  버전·영향·테스트를 보고 선택적으로 검토한다. upstream PR의 수락 여부를
  예단하지 않으며 수락에 제품 일정이나 정확성 계약을 의존하지 않는다.
  별도 global QMD의 제거·변경도 이 프로젝트의 전환 목표가 아니다.
- **지켜야 할 호환:** 사용자의 원문·위키 카드와 provenance, 승인된 collection
  경계, 검색/주입의 의미, 학습 이력·평가·checkpoint 참조, 실패 시 이전 상태로
  돌아갈 수 있는 경로. QMD index의 물리 바이트 동일성은 요구하지 않지만
  변환·재색인에서 누락/중복/교차 범위가 없음을 증명해야 한다.

## 별도 마이그레이션과 구현 순서

1. **계약 고정:** 엔진 포트·내부 ID·schema version을 문서화하고 합성
   프로젝트/두 worktree에서 생성·수정·삭제·rename·동일 해시 공유·스캔 오류,
   scoped FTS/vec 반례를 테스트한다. QMD 운영 데이터는 읽거나 쓰지 않는다.
2. **격리 엔진 수직 절편:** 명시적 임시 root의 SQLite schema, 불변 content,
   전체 membership, FTS5와 사전계산 벡터, 준비·CAS 게시·요청 pin·참조 GC를
   구현한다. 2만 청크 전후와 많은 **서로 다른 문서** 규모의 정확성·지연·RSS,
   취소·crash·WAL 회귀를 자원 가드 아래 측정한다. 모델 호출은 별도 단계다.
3. **도메인 adapter:** 파일 변경 값과 위키의 검증된 카드 입력을 엔진의
   prepare 포트에 연결한다. 검색 adapter는 기존 주입·학습 후보 포맷으로
   변환하되 세대/출처를 잃지 않는다. 런타임 writer·queue ACK와 설치 증거
   포트를 연결하기 전까지 운영 경로는 QMD에 남긴다.
4. **사용자 데이터 이전:** [별도 마이그레이션 계획](2026-10-17-embedded-engine-data-migration.md)에
   따라 기존 설정·원문·위키·출처·학습 참조와 QMD index의 의미를 대조한다.
   원본 DB·설정은 제자리에서 변환하지 않는다. 새 root에 staged 결과를
   만들고 검증 뒤 별도 승인으로 선택한다. 학습 DB와 모델 포인터 이전은
   독립된 계약으로 다룬다.
5. **릴리스 판단:** 독립 빌드·전체 합성 회귀, 사용자 데이터와 같은 형식의
   비운영 복제 E2E, 새 Claude/Codex 세션과 장기 규모 측정이 끝난 뒤에만
   설치 후보를 검토한다. 제품 이름과 외부 배포/상류 기여 결정은 마지막에 둔다.

QMD 2.5.3의 A/B 게시 검토는 [당시의 adapter 조사](2026-10-17-qmd-published-view-options.md),
현재 파일 최신성 정책은 [worktree 동기화 문서](../git-worktree-search-sync-policy.md),
여섯 영역의 현 상태는 [아키텍처 경계](2026-10-17-architecture-boundaries.md)에
남긴다. 이 문서의 목표는 아직 구현·운영 검증 결과가 아니다.

## Claude 공동 설계 검토와 조정 (2026-10-17)

로그인된 Claude Code 2.1.292의 Sonnet 모델을 일반 호스트에서
`--print --safe-mode --tools "" --no-session-persistence`로 호출해
**허용된 다섯 자료만** 범위별로 검토했다.
첫 다섯 자료 일괄 요청은 180초 무응답 timeout으로 결과가 없었다. 이후
독립 설계 요약, 실제 `scoped-vector-exact.patch`, 이 계획 원문,
[이관 계획](2026-10-17-embedded-engine-data-migration.md)과 `docs/release.md`를
나누어 전달한 네 차례 응답을 받았다. 원고·운영 DB·인증 정보는 전달하지
않았다. 이는 읽기 전용 설계 검토이며 코드·모델·운영 경로 검증이 아니다.

| 쟁점 | 검토 결과와 채택 범위 | 남은 증명 |
|---|---|---|
| 물리 저장 | Claude는 작은 control DB + 공유 불변 content/derivation 저장소 + 세대별 불변 전체 membership 파일(H)을 **비교 원형**으로 제안했다. 단일 DB(A)는 전체 membership을 매 세대에 쓰면 쓰기 비용이 없어지지 않는다. H도 파일 수·cross-DB 조회·시점 결속 비용이 있다. 어느 구조도 최종 채택하지 않았다. | A/H 동일 합성 workload의 작성량·파일 수·조회 p95/RSS/WAL·복구 비용. 논리적 전체 멤버십은 필수이며 물리적 delta/구조 공유는 정확성 증명 뒤에만 검토. |
| 범위·벡터 | 실제 QMD 패치의 `SELECT DISTINCT`는 중복 적격 키를 제거한다. p95도 이미 기록돼 있다. 적격 키 배열과 `bestByHash`는 scope 크기만큼 커지고 동점 순서가 명시되지 않았다. 리뷰 당시에는 최종 `slice(limit)` 전에 모든 상위 해시의 문서 body를 연결하는 비용이 있었고, 후속 패치가 body join을 상위 `limit`개 해시로 줄였다. 다만 **한 해시가 많은 문서 경로로 확장되는 비용**은 남는다. 동기식 20k 스캔은 약 430 ms다. | 범위 필터를 FTS/vec 점수·LIMIT 전에 적용, 안정적인 전체 동점 순서, 적격 키·조인 메모리/취소 상한과 다수의 서로 다른 문서 규모를 검사. NaN/NULL·차원 실패는 가능성으로 두고 합성 fixture로 확인. |
| 모델 파생물 | 고정 QMD 소스의 `content_vectors`는 model/fingerprint 열이 있으나 PK는 `(hash,seq)`이고 이번 scoped 조회는 model을 필터하지 않는다. Laya Index에는 모델·청킹·전처리 지문이 공유 파생물의 identity와 검색 세대에 함께 있어야 한다. | 서로 다른 동일 차원 모델과 다른 차원 모델, 세대 교체 중 섞임/재사용 거절. 기존 source의 실제 모델 전환 동작은 별도 검증. |
| 게시·큐 | 런타임 wake queue가 별도 SQLite DB인 현재 구조에서 receipt+CAS+ACK를 한 트랜잭션으로 묶을 수 없다. H/A 모두 claim identity·fencing·멱등 receipt → 게시 CAS → 조건부 ACK의 crash 재전달을 설계한다. | receipt 저장 직후, CAS 직후, ACK 유실, 늦은 writer, 두 worktree 경합에서 한 번의 효과와 중복 ACK 복구. 큐를 같은 DB로 이전할지는 별도 결정. |
| pin·취소·GC | pointer 읽기와 pin 등록은 경합 없이 묶여야 한다. 게시/rollback 보존 세대·활성 pin·준비 후보·학습이 참조하는 view를 GC root로 고려한다. `sqlite3_interrupt`/작업 청크 경계와 deadline을 평가한다. | 장기 query와 lease 만료가 겹쳐도 살아 있는 reader의 데이터를 지우지 않는 규칙, 취소와 게시의 단일 승자, WAL checkpoint 지연과 GC 재검증. TTL만으로 reader 생존을 추정하지 않음. |
| 위키·학습 | 위키는 provenance를 가진 카드를 **생산**하고 검증 후 엔진에 색인 입력을 보내며, 검색 결과도 소비한다. 학습은 게시된 view의 소비자다. 카드의 자기 인용·원문 변경·동의 철회가 이전 세대나 rollback에서 재등장하지 않아야 한다. | 카드 전용 type/role scope, 원문 의존 집합·순환 방지, 동의 철회와 학습 재현성/보존의 충돌 정책. |
| 이관·라이선스·QA | 플러그인 소유 runtime만 ownership manifest로 정리하고 global QMD는 보존한다. QMD 유래 코드의 파일별 출처와 MIT 고지를 유지한다. 릴리스는 현재 `docs/release.md`의 필수 Claude/Codex 실제 host gate를 따른다. | [이관 레드팀 항목](2026-10-17-embedded-engine-data-migration.md), 의존성/모델 라이선스 목록, 기존 사용자 upgrade·rollback·정리 시나리오의 필수 QA 편입 여부. Hermes/AGY를 이 문서만으로 필수 게이트에 추가하지 않음. |

Claude가 제안한 p50 150 ms 목표, 즉시 int8/SIMD 채택, `ATTACH`만으로
cross-DB snapshot 일관성을 보장한다는 주장, 적격 SQL의 중복과 p95 부재
지적은 채택하지 않았다. 성능 목표는 사용자 요청 지연·대상 규모·정확성
기준을 정한 뒤 측정해 결정한다. H의 불변 content와 GC 보호도 실제
multi-file seal·pin·crash 시험 전에는 보증이 아니다.

후속 [A/H 합성 비교](2026-10-17-laya-index-ah-comparison.md)는 같은 작은
membership workload에서 두 구조의 가시성·삭제·CAS·pin·ACK 재시도와
기본 비용을 측정했다. **첫 구현 원형은 A-int를 우선**하되 H는 큰 규모의
WAL·GC·저장량 병목을 증명할 때 다시 평가한다. 이 잠정 권고는 실제
FTS/벡터 서비스 지연이나 H의 multi-file crash 안전성을 판정한 것이 아니다.
A-int는 단일 DB **내부**에 불변 세대 membership을 누적한다. 과거 QMD
전체 DB 사본을 세대마다 만들던 A-copy와 다른 구조다. 첫
[A-int 수직 절편](2026-10-17-laya-index-a-vertical-slice.md)은 모델 없는
FTS·게시·pin·receipt와 삭제 없는 GC 계획까지만 합성 구현했다.

## 작업 TODO와 증거 상태

| 상태 | 작업·판정 | 근거·다음 조건 |
|---|---|---|
| 완료: 원형 | QMD 2.5.3 설치본과 후속 고정 source `f4633f9`를 구분했다. 실제 scoped vector patch, 임시 DB 정확성 검사와 100/19,999/20,001 청크 합성 benchmark를 기록했다. | [QMD 원형 기록](2026-10-17-qmd-fork-prototype.md), 형제 폴더 `qmd-fork-source-f4633f9/LOCAL-PROTOTYPE.md`. 약 2만 청크 vec warm p50 429–434 ms는 제품 성능 통과가 아님. |
| 완료: 설계 | Laya Index의 임시 ID·버전·스키마·경로, 여섯 영역과 엔진 포트, 전체 worktree 멤버십·공유 content/embedding·검색 전 scope·세대 게시를 문서화했다. | 이 문서와 [별도 데이터 이전 계획](2026-10-17-embedded-engine-data-migration.md). 구현·운영 적용은 아님. |
| 완료: 독립 설계 검토 | 사용자 승인 뒤 실제 패치, 엔진·이관 계획과 릴리스 게이트를 Claude와 범위별로 검토했다. 제안·반례·대안 조정은 위에 기록했다. | 검토는 코드 수정/실행이나 저장 구조 최종 선택이 아니다. 첫 일괄 요청은 timeout, 이후 작은 요청 4건은 응답 완료. |
| 완료: 작은 A/H 비교 | 두 구조에 같은 600행·3세대 합성 workload를 적용하고 visibility/삭제/stale writer/pin/멤버십 GC/ACK 유실 재시도/재열기를 확인했다. | [비교 기록](2026-10-17-laya-index-ah-comparison.md). 각 5회, 임시 SQLite만 사용. Opus 지정 최종 원형 재리뷰는 P1 없음, P2와 한계는 문서에 기록. A-int 우선은 잠정 권고. |
| 완료: A-int 합성 검증 | 한 DB의 불변 전체 membership·공유 content, 세대별 FTS, 제외 시 옛 pin 제공 거부, schema v3, 봉인된 source/claim, 별도 합성 queue의 receipt 대조·조건부 ACK, 삭제 없는 GC 계획을 구현했다. | [수직 절편](2026-10-17-laya-index-a-vertical-slice.md), [제품 계약](2026-10-17-laya-index-a-product-contract-gates.md). Python 28건·Node 1건 집중 검사 통과. 다중 connection/process CAS·pin·busy·child exit 검증 포함. 제품/전원 손실 검증은 아님. 첫 후속 전체 회귀는 1,584 PASS·9 SKIP였지만 뒤의 Opus 수정 전 실행이다. 두 번째 전체 회귀도 1,584 PASS·9 SKIP·0 FAIL(가드 `8efe82cbefad45c0a5e330082957d848`), `inspect()` 보강까지 포함한 최종 전체 회귀도 1,584 PASS·9 SKIP·0 FAIL(가드 `db74d95944fd45f6bdbdb14a6022c5e4`, `passed/settled:true`). |
| 완료: 승인된 합성 계약 | worktree 결과·후보·순위 격리, 최신성 실패 시 명시적 `index_stale`, 제외 시 옛 pin 제공 차단, 중복 안전·중단 복구 queue를 임시 DB에 구현하고 Opus 범위별 리뷰·회귀 테스트를 반영했다. | [제품 연결 전 계약 게이트](2026-10-17-laya-index-a-product-contract-gates.md). 동일 DB의 지연 측면 채널 차단은 요구하지 않는다. 제품 schema/FTS 규모·모델 지문·성능, 실제 host와 이관, 물리 삭제 정책은 열려 있고 파괴적 GC는 비활성이다. |
| 완료: 격리 fork 빌드 | 고정 QMD fork `f4633f9`를 별도 `/tmp` 사본에 두고 공식 Bun 플랫폼 패키지 출처·SHA512 확인, 고정 `bun.lock` 의존성 208개를 script 없이 설치한 뒤 `npm run build`가 통과했다. | 가드 `passed/settled:true`: 의존성 run `b08b33474b144fa3a301b654b484b282`, build run `196fee54e6c149909164b6ce87430e42`. 원본·설치본 불변. Laya Index 제품 패키지 빌드 또는 upstream 회귀는 아님. |
| 진행: A-int v4 제품 경계 | 단일 SQLite DB의 불변 전체 membership·공유 content에 벡터 profile/세대 seal, 명시적 source/scope의 claim→게시→ACK, full-plugin 임시 bundle·설치 journal, read-only QMD 형식 후보와 Claude/Codex/Hermes 차단 경계를 합성 구현했다. | [수직 절편 최신 기록](2026-10-17-laya-index-a-vertical-slice.md), [recall 동등성 계획](2026-10-18-laya-index-v4-recall-parity.md). 첫 v4 전체 회귀의 package/backup fixture 실패를 수정한 뒤 최종 모델 제외 전체 회귀는 **1,610건 중 1,601 PASS·9 SKIP·0 FAIL**(가드 `8c4c845fede54f37996632128eb5a9ef`, `passed/settled:true`)이었다. 로컬 GGUF bridge는 QMD 청킹·native closure·22초 hook 지연을 아직 증명하지 못하며 제품 선택은 차단한다. |
| 미실행: 제품 엔진 연결 | 독립 패키지/설치 schema, 다수 worktree·서로 다른 문서 규모의 FTS/vec, 자원·취소·WAL·전원 손실 검증, 실제 writer/ACK/요청 pin 연결. | 우선순위 2: 완료한 작은 다중 프로세스 합성 검증 이후 대규모 fixture와 지연/정확성 기준을 정하고 adapter를 연결한다. 이전 전체 회귀 수치는 현재 A-int 검증 증거로 사용하지 않는다. |
| 미실행: 운영 전환 | 기존 색인·설정 이관·무결성 대조, rollback backup, ownership manifest, 플러그인 소유 QMD runtime의 복구 가능한 정리. | 우선순위 3: [마이그레이션 계획](2026-10-17-embedded-engine-data-migration.md)의 별도 검증·승인. 사용자 별도 global QMD·공유 파일·모델은 삭제하지 않음. 영구 삭제는 별도 확인. |
| 마지막 | Laya Index 외부 이름 검증, 프로젝트 전체 최종 리네임·게시 여부. | 현재 `laya-index`는 내부 작업 식별자이고 외부 게시하지 않음. |

AGY 리뷰는 이 TODO의 필수 게이트가 아니다. 제품 전환을 완료로 표시하려면
실제 설치 후보의 독립 빌드, 데이터 이관과 rollback, 새 Claude/Codex 세션,
장기 규모 검색 검증을 별도로 마쳐야 한다.

Claude 공동 설계의 첫 제안·반례·트레이드오프 조정은 위에 기록했다.
남은 물리 저장·보존·성능 판단을 합성 측정과 실제 품질 요구로 좁히기
전까지 위 구조를 확정하지 않는다.
