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
전환 검증 후 소유권이 증명된 owner 정리 절차(`skills/setup/SKILL.md`의
`plan-legacy-cleanup`·`apply-legacy-cleanup`)에 따라 후속 정리한다. 소유권이 불확실한 파일이나 공유 파일·모델은 삭제하지 않는다.

Laya Index의 현재 구현은 `core/laya_*.py` 모듈이며 선택된 Laya route에서만
쓰인다. hook은 `core/laya_index_host_route.py classify-hook`으로 요청을
`legacy|candidate|preview|blocked`로 분류하고, legacy QMD route의 코드와 경로는
그대로 남아 있다. 이 문서는 방향을 기록하며, 아래에서 현재 구현으로 명시하지
않은 항목은 목표다.

## 식별자와 데이터 경계

| 항목 | 내부 계약 | 비고 |
|---|---|---|
| 소스 경계 | 플러그인 저장소의 `core/laya_*.py`. 색인 DB는 `core/laya_index_a.py`의 `LayaIndexA`(A-int)가 소유한다. 내부 작업명 `Laya Index`; 최종 제품·공개 패키지 이름은 미정 | 외부 미게시. QMD의 `@tobilu/qmd`를 같은 이름으로 재배포하지 않음 |
| 저장소 형식 | `LayaIndexA.SCHEMA_VERSION = 6`을 SQLite `PRAGMA user_version`으로 확인한다. 검색 세대 ID는 별개 필드다 | QMD SQLite 스키마와 버전 번호 공유 안 함 |
| 사용자 데이터 경로 | installer가 설치별 `~/.local/state/laya/<install id>`(32자리 hex, 0700)를 만든다 | 기존 QMD 경로와 병행 |
| identity | 안정적인 프로젝트 등록 ID + 별도 worktree incarnation ID + 승인된 collection/role. branch 이름이나 moving HEAD는 identity가 아님 | 승인 범위 밖 경로는 자동 등록하지 않음 |

외부 이름 검증 전에는 패키지를 게시하지 않는다. 이후 제품 이름을 정해도 이미
발행한 스키마·데이터 경로를 조용히 바꾸지 않는다.

## 여섯 영역과 엔진 포트

| 기존 영역 | 소유하는 결정·상태 | 엔진과의 경계 |
|---|---|---|
| 프로젝트·파일 동기화 | 승인된 루트의 현재 파일 열거, 읽기 실패·삭제 판정, raw change set과 snapshot | source catalog(`laya_source_catalog.py`)의 완료 snapshot과 generation binding으로 증거 전달. 검색 세대나 위키 카드 작성은 소유하지 않음 |
| 색인·검색 **내장 엔진** | 아래의 멤버십, 공유 콘텐츠·파생물, FTS/벡터 저장, 후보 준비·검증·게시, 요청 pin, 검색 점수·범위 | `LayaIndexA`의 `stage`/`stage_stream`, `publish`, `pin`, `search_many`, `release`, `inspect` 포트. legacy QMD route는 `qmd_route`·`qmd_project_query`를 사용함 |
| 위키 | 원문 revision·출처·동의·비용·카드 생성/검증/게시 | 게시된 카드와 전체 의존 집합을 엔진에 색인 입력으로 전달. 카드의 진실성이나 유료 extractor 여부는 엔진이 결정하지 않음 |
| 학습 | capture 동의, label/평가, 모델 checkpoint·승격, 사유 DB | 엔진이 게시한 corpus/view 지문과 검색 후보만 읽음. 엔진은 학습·모델 적용을 시작하지 않음 |
| 설치·마이그레이션 | 엔진 패키지·스키마 호환성, 후보 준비 증거, 경로 전환 journal·rollback 승인 | 엔진의 `inspect` 결과와 게시 receipt를 소비하고 실제 선택을 조율. 검색 랭킹이나 사용자 문서 내용은 소유하지 않음 |
| 런타임 조율 | wake queue, 작업 우선순위, 자원 lease, endpoint·모델 프로세스 수명 | 승인된 엔진 작업을 실행·취소하고 ACK. 특정 세대가 검색 가능하다는 판정이나 query snapshot은 엔진이 소유 |

호스트 훅(Claude/Codex/Hermes)은 이 여섯 영역의 진입 adapter다. 새 엔진은
호스트 세션·위키 extractor·학습 권한을 직접 알 필요가 없다. hook은 요청마다
legacy QMD route(`recall.py`)와 Laya route(`laya_host_response.py`) 중 하나만
택하며, blocked로 분류된 요청은 QMD로 대체하지 않고 실패한다. QMD와 새 엔진의
결과를 한 요청에서 섞지 않는다.

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
   방식은 금지한다. 현재 FTS(`search_many`)와 벡터(`search_exact`) 후보는
   모두 pin한 세대의 멤버십·경로 범위를 SQL 조건으로 적용한 뒤 순위를 매긴다.
   벡터는 범위 안의 모든 chunk를 정확 스캔한다. scope별 벡터 구조나 적격 집합
   기반 ANN은 구현돼 있지 않다.
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

현재 A-int(`LayaIndexA`)는 단일 SQLite DB 안에 세대별 불변 전체 membership과
해시로 공유하는 content를 누적하고, 세대마다 별도 FTS 테이블을 둔다.

`worktree_query_slice.py`와 `worktree_wiki_membership.py`는 멤버십/CAS
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

## 관련 문서와 남은 목표

플러그인 소유 QMD runtime 정리는 [setup 스킬](../../skills/setup/SKILL.md)의
owner 정리 절차에, 원문
최신성과 worktree 정책은 [worktree 동기화 문서](../git-worktree-search-sync-policy.md)에
둔다.

다음은 구현되지 않은 목표다.

- 플러그인이 독립 설치·관리한 QMD runtime을 ownership manifest로 소유권을
  증명해 복구 가능하게 격리·정리하는 절차. 사용자가 별도로 설치한 global
  QMD·공유 파일·모델은 삭제하지 않으며, 영구 삭제는 별도 확인을 받는다.
- A-int 세대의 증분 stage와, 질의마다 세대 전체 FTS 행을 다시 대조하지 않는
  무결성 검사. 현재 A-int stage는 세대 전체를 한 트랜잭션으로 쓰고,
  `search_many`는 질의마다 `_generation_fts_rows_match`로 세대 FTS를 대조한다.
  문서 단위 증분 공개는 A-int 세대와 별개인 `laya_incremental_docs.py`가 맡는다.
- scope별 벡터 구조 또는 적격 집합 기반 ANN.
- Laya Index 외부 이름 검증과 프로젝트 전체 최종 리네임·게시 여부.
