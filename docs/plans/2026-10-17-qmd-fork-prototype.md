# QMD 고정 소스 원형: 범위와 검증 기록 (2026-10-17)

**현재 제품 방향:** 이 문서는 QMD 고정 소스 실험의 증거다. 제품으로 QMD
fork를 설치하거나 upstream PR 채택을 기다리는 계획이 아니다. 최신 개발
순서와 TODO는 [Laya Index 계획](2026-10-17-embedded-sqlite-search-engine.md),
사용자 데이터·플러그인 소유 QMD runtime의 이관/정리는
[마이그레이션 계획](2026-10-17-embedded-engine-data-migration.md)을 따른다.
별도 global QMD는 보존한다. 아래 단계와 당시 잠정 추천은 실험 당시의
판단으로 읽는다.

## 결정 전제

이 문서는 **개발 checkout의 source-only 원형** 계획이다. 외부 GitHub fork,
push, 패키지 게시·설치, 모델 실행, 운영 DB 이행을 승인하거나 수행한 기록이
아니다. 제품 이름 변경은 마지막 단계로 미룬다. 후보 C의 합성 멤버십과
요청 pin은 `core/worktree_wiki_membership.py`와
`core/worktree_query_slice.py`에 있으며 운영 QMD와 연결돼 있지 않다.

### 출처·라이선스·현재 기능

- 관리형 runtime `prepared.json`은 `@tobilu/qmd` 2.5.3 설치본을 가리킨다.
  설치본 `package.json`은 공식 `https://github.com/tobi/qmd`를 저장소로
  명시하고, `locks/qmd-2.5.3/package-lock.json`은 2.5.3 tarball URL과
  SHA-512 integrity를 고정한다. 이 증거는 설치 tarball의 고정이지,
  npm 배포물과 Git tag의 byte-for-byte 동일성 증명은 아니다.
- 이 **현재 설치 2.5.3**은 후속 source commit `f4633f92…`와 다르다.
  설치본 lex는 아직 전역 `limit×10` 후보 뒤에 collection을 적용한다.
  고정 소스의 `package.json`이 `2.8.3`이라도 그 커밋의 나중 lex 수정이
  현재 설치본에 반영된 것으로 해석하지 않는다.
- 설치본의 `LICENSE`는 MIT이며 복제·수정·재배포 시 copyright와 license
  고지를 요구한다. lock의 289 dependency entry에는 MIT 외 ISC,
  Apache-2.0, BSD, BlueOak 및 복수 선택 라이선스가 있어 내장 배포 전
  각 의존성의 고지·재배포 조건을 별도로 확인해야 한다.
- 현재 설치 2.5.3의 `dist/store.js:2601-2643` FTS는 전역 상위
  `limit×10` 뒤에 collection을 걸고, `:2669-2713` vec는 전역 상위
  `limit×3` 뒤에 collection을 건다. QMD가 보장할 검색 범위와 후보 C
  합성 adapter의 범위가 달라서 그대로 연결할 수 없다.
- 공식 [v2.8.3 릴리스](https://github.com/tobi/qmd/releases/tag/v2.8.3)
  (tag `facd35e`)는 collection별 vec 검색과 local config 신뢰·symlink
  containment·HTTP origin/host 검증 등의 보안 수정을 포함한다. 공식
  [BM25 범위 수정 `f4633f92a99ffdb2d0e885ed391fd8169133d70e`](https://github.com/tobi/qmd/commit/f4633f92a99ffdb2d0e885ed391fd8169133d70e)
  는 v2.8.3 **이후**의 source commit이며, v2.8.3 패키지에 들어 있다고
  간주하지 않는다. [문제 #922](https://github.com/tobi/qmd/issues/922)는
  2.8.3의 lex 누락과 그 수정 경위를 기록한다. 버전 변경에는 QMD CLI,
  SDK, MCP, native dependency, DB schema와 플러그인 adapter 호환성
  검증이 필요하다.
- 추가로 공식 [partitioned vector index #1024](https://github.com/tobi/qmd/pull/1024)
  와 [partition cleanup #1025](https://github.com/tobi/qmd/pull/1025)가
  2026-09-30 main에 병합됐다. 앞선 [exact small-collection #847](https://github.com/tobi/qmd/pull/847)는
  2만 청크를 넘는 scope에서 전역 후보 fallback이 남고 전체 벡터를 반복
  읽는 비용이 있었다. [#983 설계 기록](https://github.com/tobi/qmd/pull/983)은
  partition 방식과 그 migration·공유 hash 복사 문제를 상세히 다룬다.
  이 upstream 변경들을 새 fork의 독자 발명으로 기록하거나 이미
  설치된 2.5.3에 들어 있다고 간주하지 않는다. **고정 소스
  `f4633f92…`의 `src/store.ts`에는 partitioned vec 테이블이 없다.**
  이 commit에 #1024/#1025의 내용이 들어 있다는 앞선 추정은 소스
  확인으로 철회한다. 병합 기록과 이 파일의 실제 상태가 다른 이유는
  아직 확인하지 못했다.

### 배포·유지보수 비용

- 설치 2.5.3의 `package.json`은 Node `>=22`, `better-sqlite3` 12.10.0,
  `node-llama-cpp` 3.18.1, `sqlite-vec` 0.1.9와 OS/CPU별 optional native
  패키지를 선언한다. 현재 Mac ARM 설치본은 약 187 MiB(이 환경의 `du`),
  `docs/qmd-installer-scope-20261006.md`의 기존 검증은 dedicated npm
  generation 약 228 MB와 package 전송 크기의 제한을 기록한다. fork의
  크기·전송량·실행 속도는 **미측정**이다.
- `core/qmd_runtime.py`는 이미 플러그인 전용 generation, Node/QMD/package
  hash 검증, `active.json` 선택과 이전 generation rollback을 제공한다.
  fork는 사용자 전역 `qmd` 실행 파일을 덮어쓰지 않고, 향후 별도의 고정
  source commit·lock·native ABI 증명을 갖춘 비활성 generation으로
  준비되어야 한다. 현재 `VERSION='2.5.3'`과 installer lock 검증은
  공식 패키지 전용이므로 fork를 끼워 넣어 우회할 수 없다.
- upstream release·security advisory·관련 issue를 주기적으로 대조하고,
  수정 commit을 backport하거나 base를 다시 고정할 때마다 license 고지,
  lock integrity, 세 host adapter, Node/OS/native matrix, DB rollback을
  다시 검증해야 한다. upstream의 새 버전이 같은 요구를 만족하면 fork
  patch를 줄이거나 없애는 것이 유지보수에 유리하다.

## 실행 순서와 통과 기준

1. **baseline 고정:** 공식 서명 tag와 후속 수정 commit을 정확한 hash로
   기록하고, 설치본 2.5.3과 필요한 동작 차이를 source level에서 열거한다.
   MIT와 transitive license 고지를 누락 없이 기록한다. 아직 새로운
   패키지를 설치하지 않는다. 첫 기준 `f4633f92…`는 10월 BM25 수정은
   포함하지만 partitioned vec는 포함하지 않는다. partition 소스의
   정확한 commit·schema·migration은 별도 확인이 필요하다.
2. **검색 원형:** 개발 checkout의 격리 파일에서 lex의 전역 유한 top-K
   후 필터와 수정된 전체 MATCH scope 방식을 비교한다. vec는 upstream의
   partitioned-index 계약과, 공유 hash의 임베딩 계산 재사용·partition
   row 복사 구분을 모델 없는 합성 fixture로 표현한다. 검색 후보 생성이
   scope 이전에 잘리지 않는다는
   합성 반례(다른 collection의 200개 이상 lex, 60개 이상 vec), 요청
   generation pin, 삭제·rename·같은 경로 충돌을 통과해야 한다.
   단순히 overfetch 배수를 늘리는 방식은 통과로 인정하지 않는다.
3. **adapter 원형:** 선택된 worktree·불변 wiki 세대가 검색 전 scope에
   도달하고, 전체 멤버십 게시 CAS 뒤에만 ACK하며, 늦은 writer·옛
   세대 재선택을 거절해야 한다. 개발용 명시 경로와 임시 SQLite만
   사용한다. 기존 공유 DB writer, daemon, 활성 pointer는 건드리지 않는다.
4. **합성 benchmark:** 크기·동일 hash 비율·worktree 수·변경/삭제율을
   고정한 공개 가능 fixture로 2.5.3식 bounded overfetch, 정확 범위
   원형, worktree별 격리 대안을 비교한다. 정답의 scope/recall@K,
   p50/p95 lex 지연, 갱신·게시 시간, CPU/RSS, DB/WAL 크기를 같은
   하드웨어와 요청 집합에서 잰다. 모델을 쓰지 않는 FTS/멤버십 수치와
   **미측정**인 vec ANN/embedding 성능을 분리해 기록한다. 실제 우위는
   추정하지 않는다.
5. **향후 승인 경계:** package build/install, real QMD model·native
   benchmark, OS/Node matrix, 외부 fork 생성·push/게시, 운영 데이터
   migration과 daemon cutover는 각각 별도 검토 대상으로 남긴다.

**현재 판단으로 대체된 잠정 추천:** 이 실험 당시에는 QMD fork를 줄이거나
upstream 기능으로 대체할 가능성을 검토했다. 이후 제품 방향은 독립된
`laya-index` SQLite 엔진으로 정했다. QMD upstream의 필요한 버그·보안
수정은 출처·라이선스를 남겨 선택적으로 검토하되, PR 채택 여부에 엔진
구현·전환을 의존하지 않는다. 제품 전체 최종 이름은 나중에 결정한다.

## source-only 원형 결과

- `core/qmd_fork_scope_prototype.py`는 후보 C의 임시 SQLite 위에 공유
  hash별 FTS5 행을 만들고, 설치 2.5.3 형태의 전역 상위 후보 제한과
  범위 적용 전 전체 MATCH를 물질화하는 질의를 나란히 구현한다.
  QMD 원본 코드를 복사하거나 설치본·운영 DB를 변경하지 않았다.
- `core/qmd_fork_source_adapter.py`는 기존 요청 pin의 worktree·세대를
  하나의 불변 collection ID로 묶어 fake lex/vec backend에 전달한다.
  backend가 후보 제한 **이전** 범위 적용과 불변 세대를 제공한다고
  명시하지 않으면 검색 호출 전에 거절한다. 결과의 세대·멤버십도
  검증한다. 단, 이 capability 표시는 실제 QMD 엔진의 증명이 아니다.
- 합성 FTS5 fixture(대상 5개, 다른 worktree 220개, 한 단어 `target`,
  limit 5, warm query 30회)의 정확도는 기존 전역 후보 방식 recall@5
  `0.0`, 범위 내 전체 MATCH 방식 `1.0`이다. 최초 한 실행의 지연은
  각각 p50 `0.1403`/`0.1683` ms, p95 `0.1655`/`0.1844` ms였다.
  이 미세 수치는 실 QMD·ANN·embedding 속도를 나타내지 않는다.
  fixture는 `test/fixtures/qmd-fork-scope-bench.py`, 가드 실행은
  `test/qmd-fork-scope-bench.test.mjs`이다. 다른 규모·해시 공유 비율·
  갱신/삭제율의 비교는 아직 하지 않았다.
- 새 프로토타입 집중 검사 3개는 통과했고 가드가 `settled:true`로
  끝났다. 이 추가 이전의 안전한 전체 회귀는 1,590개 중 1,581개 통과,
  9개 skip, 실패 0개였다. 실제 모델을 실행하는 기존 fixture는
  `QMD_REAL_MODEL_TESTS=1`을 명시한 별도 흐름에서만 열리도록 격리했다.

## 고정 QMD 소스 패치

- 공식 commit `f4633f92a99ffdb2d0e885ed391fd8169133d70e`의 GitHub
  archive를 개발 공간의 형제 디렉터리 `qmd-fork-source-f4633f9/`에
  추출했다. archive SHA-256과 MIT 고지, source diff, 검증 한계는 그
  사본의 `LOCAL-PROTOTYPE.md`에 기록했다. 전역 설치본은 바꾸지 않았다.
- `src/store.ts`의 **scoped vec** 경로는 기존 20,000 eligible chunks
  초과 때 전역 ANN으로 돌아가던 분기를 제거하고 전량 exact distance
  scan으로 바꿨다. 파일당 여러 chunk가 `limit×3`을 독점할 수 있던
  사전 절단도 제거했다. 독립 리뷰에서 확인된 전량 본문 join 비용은
  콘텐츠 hash당 가장 가까운 chunk만 남기고 상위 `limit`개 hash만
  본문에 연결하도록 줄였다. lex는 upstream commit에서 이미 전체 FTS
  MATCH를 물질화하므로 중복 패치하지 않았다.
- 수정된 실제 TypeScript 함수와 설치된 sqlite-vec 0.1.9를 연결한
  임시 메모리 DB 검사는 통과했다. 4,100개 다른 범위 벡터가 전역
  `k=4096`을 채울 때 대상 범위 벡터가 빠지는 반례에서 exact 함수가
  대상만 반환했다. Node의 임시 TypeScript 변환으로 실행한 **실제
  수정 `store.ts`**의 FTS/vec 통합 스모크도 통과했다. 두 검사 모두
  사전 계산한 숫자 벡터만 사용했고 모델은 호출하지 않았다.
- 이는 전체 upstream 빌드·typecheck·네이티브 호환 행렬 검증이 아니다.
  고정 source는 `better-sqlite3` 13.0.3을 선언하지만 격리 검사는
  설치본의 12.10.0을 읽기 전용으로 빌려 썼다. 전량 exact scan은
  매우 큰 범위에서 거리 계산과 적격 hash 목록의 시간·메모리 비용이
  커질 수 있어 운영 채택 전
  분할 인덱스 대안과 실제 규모 benchmark가 필요하다.
- 독립 읽기 전용 재리뷰는 최선 청크/상위 hash 선택에서 새 정확성 결함을
  발견하지 못했다. 동일 문서 11청크와 다음 문서를 함께 반환하는
  실제 `store.ts` 통합 스모크를 포함해 고정 소스 집중 2개가 통과했고
  가드 `settled:true`였다. 재리뷰도 대규모 적격 청크의 전수 계산과
  메모리 비용을 미해결 위험으로 남겼다. 초기 Claude 구현 리뷰는 DNS
  `ENOTFOUND`로 완료되지 않았고, 사용자 승인 전 전체 payload 요청은
  자동 승인 심사가 실행 전에 거부했다. **이후 사용자 승인 아래** 로그인된
  Claude Code 2.1.292의 Sonnet 모델로 무도구·무세션 범위별 요청 네 건이
  응답 완료됐다. 첫 일괄 요청은 180초 timeout으로 결과가 없어 완료로
  세지 않는다. Claude는 당시 실제 scoped 패치에서 적격 키 배열·
  `bestByHash`의 범위 비례 메모리, 동점 순서 부재, body join 비용과
  2만 청크 약 430 ms를 지적했다. `SELECT DISTINCT`와 p95는 이미 있었고,
  결과 `slice(limit)`도 이미 적용됐으므로 해당 지적은 정정했다. 이후
  body join을 상위 `limit`개 hash로 줄인 현재 패치 자체는 Claude에 다시
  보내지 않았다. 한 hash의 여러 document path 확장, 모델 지문 혼합,
  취소·대규모 메모리 상한은 아직 증명되지 않았다. 조정 결과와 다음 검증은
  [Laya Index 계획](2026-10-17-embedded-sqlite-search-engine.md)에 기록했다.
- 중단한 첫 전체 검사에서 오프라인 캐시 모델 fixture가 실행됐으나
  가드 checkpoint는 `reason:interrupted`, `settled:true`다. 가드의
  `cleanup()`은 소유 PID/시작 시각을 재확인하고 남은 소유 프로세스가
  없을 때에만 settled를 반환한다. 다음 전체 검사의 9 skip 중 새로
  제외한 7개는 실모델 fixture의 명시적 opt-in 부재에 따른 것이며,
  이 7개 모델 검사가 통과했다고 주장하지 않는다.
- 앞서 기록한 **전체 1,590개 회귀 결과는 실제 QMD 소스 패치 이전**의
  플러그인 checkout 결과다. QMD 소스 사본의 후속 집중 검사는 별도의
  임시 DB·기존 sqlite-vec에서 통과했고, 전체 upstream 회귀는 아직
  실행하지 않았다.
- 모델 없는 대형 범위 측정은 `qmd-fork-source-f4633f9/LOCAL-PROTOTYPE.md`에
  상세히 기록했다. 실제 수정 `store.ts`의 vec warm p50은 100 scoped
  chunks/4,100 distractors에서 `1.470 ms`, 19,999/4,100에서
  `429.423 ms`, 20,001/4,100에서 `433.866 ms`였다. 모두 올바른
  scope 결과를 냈다. 최고 관측 RSS는 `190,896 KiB`였고 자원 가드는
  `settled:true`로 끝났다. 새 연결에서 lex를 먼저 호출한 뒤 첫 vec
  호출과 warm 질의를 따로 쟀다. OS cache와 앞선 lex의 SQLite cache는
  비우지 않았으므로 cold benchmark는 아니다.
- 공식 `node scripts/build.mjs`를 임시 복사본에서 기존 설치 의존성만
  링크해 시도한 결과 TS exit 2였다. 현재 설치본 의존성에는
  `@types/node`가 없고, source lock의 `@types/better-sqlite3` 7.6.13
  (→ `@types/node` 24.10.1)과 `better-sqlite3` 13.0.3 등 선언된
  버전이 갖춰지지 않았다. 새 패키지 설치는 수행하지 않았다.
