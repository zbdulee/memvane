# Laya Index A-int 제품 연결 전 계약 게이트

**2026-10-18 현재 변경:** Claude Opus(`modelUsage: claude-opus-5-5`)가 작성한 청킹 초안을 검토해 고정 QMD fork의 regex breakpoint·code fence·tokenizer 재귀 분할을 `core/laya_qmd_chunking.mjs`에 이식했다. AST mode와 내용이 달라지는 detokenize fallback은 명시적으로 거부한다. `core/laya_local_embed_worker.mjs`/provider에는 별도 `document_chunks` recipe를, `core/laya_index_sync.py`에는 명시적 off-hook 전체 batch 준비·source 재스캔을 추가했다. 동일 본문 hash가 파일명 때문에 서로 다른 QMD title을 가지면 현재 `(content_hash,profile,seq)` 벡터 키에 두 임베딩을 저장할 수 없어 **모델 호출 전 차단**한다. 모델 없는 집중 가드 `d2fedc8d5b1448f18808e15713dd2007`은 Node 9/9(내부 sync Python 23, provider Python 9 등) 통과했다. 임시 QMD 후보를 명시적 A-int publish/replay/reopen으로 검증한 가드 `f1009aa5ea4c45bb9722a3fe22f1086e`는 Python 26+Node 1 통과했다. 후자는 설치 선택·rollback 검증이 아니다. 정식 L1은 `NOT RUN`, 제품 provider 등록은 비어 있다. 아래 1,601 PASS 전체 회귀는 **이번 변경 이전** 기록이다.

**한정된 실제 embedding 기능 smoke:** 승인된 선택 cache의 embeddinggemma-300M Q8_0 파일과 설치된 QMD 2.5.3/`node-llama-cpp` 3.18.1 closure를 가드에서 해시 확인한 뒤, `--kind model-test` 단독 lock·120초 제한으로 임시 문서 1회와 질의 1회만 호출했다. 첫 `gpu:false` 시도(`f9b3745e16ab459bb0e89cecc3cff1de`)는 CPU prebuilt 부재로 모델 호출 0회에 실패했다. 설치 QMD와 같은 native 선택 방식인 `gpu:'auto'`/`gpuLayers:0`, `maxThreads:1`, `threads:1`, `build:'never'`, `skipDownload:true`로 한 번만 재시도한 가드 `af90592d823d482ba8318de9016a010b`는 `passed/settled:true`였다. 실제 backend는 Metal로 보고됐으나 모델 GPU layer는 0이다. 문서·질의 벡터는 각각 유한한 768차원이고 토큰 수는 20/10, cold load 504ms, 문서 20ms, 질의 9ms, 총 679ms였다. 가드는 5초 간격 관측 사이에 약 1초 만에 끝난 모델 프로세스의 **실제 peak RSS를 측정하지 못했다**. 이 기록은 기능 smoke일 뿐 제품 provider 승인, QMD 검색 품질 동등성, 부하 중 latency 또는 정식 L1 증거가 아니다. 추가 모델/학습 호출은 하지 않는다.

**이번 최종 전체 회귀:** 첫 모델 제외 `npm test` 가드 `c4b175c3ed524e899e9cbf0aeb0c016f`는 시스템 Python에 PyYAML이 없어 실패했고, 테스트 venv를 PATH 앞에 둔 두 번째 가드 `644c6069ce364f3095656336351e5ab7`는 private user-site fixture가 금지되어 실패했다. 둘 다 `settled:true`/exit 1이다. 시스템 Python의 user-site를 유지하면서 기존 venv의 PyYAML만 `PYTHONPATH`로 제공한 4개 문제 영역 집중 가드 `40181e86f3dd4261b26f4af503bad93f`는 Node 50/50 통과했다. 이 환경에서 최종 코드의 모델 제외 전체 회귀는 **1,617건 중 1,608 PASS·9 SKIP·0 FAIL**, 가드 `608c3a4808a94c1294cb407de09e5c5d` `passed/settled:true`였다. `git diff --check`도 통과했다. 9 SKIP에는 명시적 opt-in 모델 검사가 포함되며 실제 학습은 실행하지 않았다.

**QMD 제목·전환 증거 보강:** QMD 원본 inventory의 digest가 title을 읽지 않아 같은 본문에서 제목만 바뀌어도 source/backup stale을 놓쳤다. 제목을 읽고 digest에 결속했으며, 동일 body hash의 활성 문서가 서로 다른 QMD title을 가지면 단일 hash 벡터를 사용하는 후보를 사전 거부한다. 격리된 tagged QMD-origin 원장은 실제 제품 route를 바꾸지 않고 source/backup, A-int incarnation·generation·vector seal, queue 경로/inode, route-byte SHA 및 CAS epoch를 확인한다. 첫 새 쓰기 전 합성 복구와 그 뒤의 forward-only 차단을 검사했다. 집중 가드 `6dc4cd9fcbe9440cb18b0513ce37dc37`은 Node 10/10(QMD Python 28, 전환 Python 8 포함) `passed/settled:true`였다. 독립 queue instance ID와 제품 QMD route 선택/복구 계약은 아직 없다.

이 문서는 [A-int 합성 절편](2026-10-17-laya-index-a-vertical-slice.md)의 제품 연결 계약과 남은 게이트를 기록한다. A-int는 **단일 SQLite DB 내부**의 불변 세대별 전체 membership과 공유 content이며 전체 DB 복사 구조가 아니다. 내부 이름 `Laya Index`/`laya-index`와 최종 제품명은 마지막에 결정한다. 파괴적 GC는 비활성이다.

### 이번 구현·검증 상태

**2026-10-18 갱신:** 아래의 Python 28건 및 전체 1,584 PASS는 당시 snapshot의 증거다. 최신 A-int/queue는 Python 48건+Node 1건(`1243775993aa4947949ca9aa9d88adae`), 명시적 runtime은 Python 6건+Node 1건(`51a1a6b57ab4414796af6682b151f681`), Claude/Codex/Hermes의 선택 경계는 최종 호환 수정 후 45 PASS·1 SKIP(`4b6deb76b75f40df8ae9089ecdc1288a`), topical은 21 PASS·1 SKIP(`f865e7181e2148dc9b78bdb09eed0498`), 합성 이관은 Python 28건+Node 1건(`100f8d466a87491ead3f16b3a962ca96`), source-only package/backup은 Python 10건+Node 1건(`1c9253fe860745859b94108594f48e74`)을 각각 가드에서 통과했다. 최신 수정 이후 기존 테스트 venv를 사용한 모델 제외 전체 회귀도 **1,600건 중 1,591 PASS·9 SKIP·0 FAIL**(`20c1707d1878449ba7c265f0e9ee0a66`, `passed/settled:true`)이다. 모델 opt-in 검사는 실행하지 않았다. 설치 소유 journal과 실제 제품 route는 구현되지 않았으며 L1 QA는 `NOT RUN`이다.

**후속 v4 진행:** 앞 문단의 전체 회귀 수치는 v4 이전 기록이다. 현재 설치 소유 journal과 full-plugin 임시 bundle, 선택 경계, 단일 DB 벡터 seal과 owner-bound sync를 합성 구현했다. v4 벡터 Python 8+Node 1(`e9812e8956414f9c9d64cc940c616171`), A-int Python 51+sync 20+runtime 6·Node 3(`a2d703174e514dbfb5c35e8a9aef1346`), `inspect` 보강 A-int Python 52+Node 1(`06a17db45a494dd085446db1a8116130`), 제품 hook 차단과 개발 전용 벡터 edit→sync→query 리허설 Python 2+Node 1(`1e2bdde46fe94d1eb62449a3ed998bc9`)이 각각 가드에서 `passed/settled:true`였다. QMD 실제 형식 adapter의 read-only 이관 후보는 별도 검증 중이며, 정식 marketplace L1은 여전히 `NOT RUN`이다. 생산 provider의 모델 파일·native dependency closure 승인과 동일 기능 recall 검증이 없어 제품 route는 차단된다. [v4 recall 비교](2026-10-18-laya-index-v4-recall-parity.md)를 필수 gate로 둔다.

**v4 최종 코드 검증:** QMD 형식의 읽기 전용 후보는 Python 25+Node 1(`636c6030a8d64f8489bc89b029e101c1`), provider의 Opus 지적 수정 뒤 벡터/bridge는 Python 17+Node 2(`aba569d224274137adaac5d52734034a`)로 각각 가드 통과했다. 첫 전체 회귀의 v4 package/backup fixture 실패를 수정한 뒤 모델 제외 **1,610건 중 1,601 PASS·9 SKIP·0 FAIL**(가드 `8c4c845fede54f37996632128eb5a9ef`, `passed/settled:true`)이었다. 실제 모델 추론·다운로드, 운영 데이터 이관, 정식 marketplace L1은 실행하지 않았다. 선택된 제품 경로는 계속 차단한다.

**실제 provider 메타데이터 확인 뒤의 연결 순서:** 선택된 플러그인 QMD 설정은 embeddinggemma-300M Q8_0을 가리키고 같은 모델 파일(328,576,992바이트)이 전용 cache에 존재한다. QMD 2.5.3의 `node-llama-cpp` 3.18.1에는 `build:"never"`·`skipDownload` 선언과 macOS arm64 Metal native 파일이 있다. 이것만으로 모델/의존성 hash나 동작을 승인하지 않는다. 먼저 exact artifact·native closure·900/135 tokenizer 청킹과 질의 형식의 동등성을 검증하고 별도 model-test 자원 게이트에서 cold/warm 시간을 측정한다. 이어 모델을 **hook 밖의 소유 worker**가 준비하고 완료된 전체 벡터 배치만 claim/source manifest와 봉인해 게시해야 한다. hook은 bounded enqueue와 현재 세대 stale 차단만 수행하며 실제 모델 로드를 기다리지 않는다. 그 뒤 격리 이관의 publish/rollback과 정식 L1을 검증한다. 현재 `APPROVED_PRODUCTION_PROVIDERS`는 비어 있고 제품 route는 보류 상태다.

**5초와 22초의 의미:** `core/config.py`의 기본 `queryTimeout=5`와 `core/recall.py`의 per-query clamp는 QMD 질의 한 번의 상한이다. 기존 v2 작업의 `core/hook_budget.py`는 prompt/posttool 전체를 기본 18초·최대 20초의 단일 monotonic deadline으로 묶었고, `hooks/hooks.json`·`hooks/hooks-codex.json`은 host timeout 22초로 종료 여유 2초를 둔다([당시 근거](../hook-budget-and-recovery-20261006.md)). SessionStart update/index 등은 별도로 5초 또는 3초다. 이번 A-int 작업에서 host timeout을 늘린 것이 아니다. QMD 5초 질의 예산을 새 모델의 로드 시간으로 전용하거나 host 22초를 늘려 성능 문제를 가리지 않는다.

**별도 실제 모델 smoke 범위(기능 시험 완료):** 위 한정 시험은 모델 load 1회와 문서·질의 embedding 각 1회로 끝났다. 본문 데이터는 합성이고 운영 DB/원고를 읽지 않았다. 실제 peak RSS는 미측정이며, guard의 표본 RSS를 peak로 해석하지 않는다. 문서 청킹 전체 parity, 품질, 소유 worker의 제품 lifecycle, 부하 중 자원 제한은 별도 검증이다.

**Opus 제품 경계 검토:** 도구 없는 범위별 호출이 성공했고 실제 `modelUsage`는 `claude-opus-5-5`였다. 레시피/부분 seal/stale commit, hook 밖의 소유 worker와 native closure, SIGKILL 지점별 리허설을 후속 구현 항목으로 채택한다. Opus가 제안한 selected route의 lexical-only 조용한 폴백은 현재 hybrid 필수 계약과 충돌하므로 채택하지 않는다. 첫 새 쓰기 뒤 단순 pointer rollback도 기존 journal 계약상 금지한다. 구현 순서는 (1) native artifact·recipe·tokenizer 증거, (2) 합성 청킹·비동기 job/전체 배치 seal과 stale CAS, (3) 별도 실제 모델 시험, (4) 격리 이관 publish/복구, (5) 정식 marketplace L1과 전체 회귀다. 실패·미완료 단계에서는 선택된 제품 route를 차단한다.

독립 Claude Opus QA 네 호출의 `modelUsage`는 모두 `claude-opus-5-5`였다. 동일 세대의 다른 claim이 반복 defer되는 결함, 열린 reader의 schema fence, 초기 header snapshot, FTS 누락, source ancestor symlink, dangling sidecar를 지적했고 관련 코드와 합성 반례를 보강했다. 이 리뷰는 수정 전 snapshot의 읽기 전용 근거이며 수정 후 제품 준비 승인으로 취급하지 않는다. 별도 queue v2의 DB incarnation 결속, source-only 후보 bundle, detached backup preflight도 추가했지만 exact-commit 설치·활성화와 운영 입력 이관의 증거는 아니다.

추가 A-int 독립 Opus 리뷰 세 호출도 실제 `modelUsage`가 `claude-opus-5-5`였다. metadata REPLACE 우회, 세대 ordinal, receipt proof, 직접 API 경로, 제외 뒤 stage 재전달, RO/RW DB 교체, GC 참조 세대 분류를 지적했고 이를 수정해 집중 회귀를 통과했다. 공유 FTS 내부 역색인의 완전한 읽기 전용 검사와 실제 전원 손실은 여전히 미검증이다.

- `core/laya_index_a.py`의 합성 스키마 v3은 명시적 no-clobber 생성, 읽기 전용 header/정의 지문 분류, 세대별 FTS 모집단, 불변 세대·receipt·pin 봉인, `INSERT OR REPLACE` 우회 방지, append-only 제외와 옛 pin의 명시적 `index_stale`, 고정 base/source manifest/claim 증거를 구현한다. 동적 세대 FTS 정의와 데이터 대응은 read-only `inspect`가 검사한다. 같은 DB의 공유 content는 유지하므로 동일 바이트 존재에 대한 저장량·처리시간 측면 채널은 남는다. 사용자 확정 계약은 결과·후보·순위 분리이며 이 측면 채널 차단을 요구하지 않는다.
- `core/laya_index_source_port.py`는 명시적 root의 bounded Markdown 전체 스캔, symlink 거부, 게시 직전 재검사, 검색 시 재검사를 제공한다. 오류는 빈/부분 검색으로 숨기지 않는다. `core/laya_index_claim_queue.py`와 `core/laya_index_flow.py`는 별도 합성 queue의 retained payload, lease, exact receipt 대조, 조건부 ACK·replan·defer를 연결한다. 이 모듈은 운영 hook/설치본에 연결되지 않았다.
- 격리 SQLite의 Python **28건 + Node 래퍼 1건** 집중 검사가 저장소 가드에서 통과했다(run `ebeee1537d8b4c95b7dd60d3a3544888`, `passed/settled:true`). 두 writer CAS, reader pin 중 게시/읽기 전용 GC 계획, busy timeout, receipt 재전달, 소유 child 비정상 종료 뒤 reopen/integrity가 포함된다. 실제 전원 손실이나 저장소 간 원자성을 입증하지 않는다.
- 고정 QMD fork `f4633f9`를 `/tmp/laya-fork-build`에 격리 복사했다. 공식 `@oven/bun-darwin-aarch64@1.4.3`의 registry SHA512를 확인해 script 없이 `/tmp/laya-build-tools/package/bin/bun`으로 추출했다. 그 폴더에서 `/tmp/laya-build-tools/package/bin/bun install --frozen-lockfile --ignore-scripts --no-save`(208개 의존성, 가드 run `b08b33474b144fa3a301b654b484b282`, exit 0), 이어 `npm run build`(Node v24.21.0, npm 11.19.0, 가드 run `196fee54e6c149909164b6ce87430e42`, exit 0)를 수행했다. `bun.lock`은 설치 전후 동일했고 원본 source·설치본은 바꾸지 않았다. 결과는 `/tmp/laya-fork-build/dist/cli/qmd.js`와 `dist/cli/build-info.json`이다. 모델·운영 데이터를 실행하지 않았다. 이는 Laya Index 독립 제품 빌드가 아니라 고정 QMD fork의 공식 빌드 검증이다.
- 후속 코드의 첫 전체 회귀는 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**(가드 run `4fcffadc46e44d73a12985329261878e`, `passed/settled:true`)였다. 두 번째 전체 회귀도 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드 run `8efe82cbefad45c0a5e330082957d848`, `passed/settled:true`였다. 그 뒤 `inspect()` 누락을 추가 수정한 마지막 코드의 전체 재실행도 **1,593건 중 1,584 PASS·9 SKIP·0 FAIL**, 가드 run `db74d95944fd45f6bdbdb14a6022c5e4`, `passed/settled:true`였다. 9 SKIP에는 명시적 opt-in이 필요한 모델 검사가 포함되며 모델은 실행하지 않았다.
- Claude Opus 독립 리뷰는 schema/stage, source/queue, publish/search/GC로 나뉘어 실제 `modelUsage`가 모두 `claude-opus-5-5`였다. 처음 37KB 단일 호출은 240초 시간 초과로 결과가 없었다. 이후 성공한 세 범위 리뷰에서 동적 FTS 이름의 schema 검증 우회, receipt/head/schema metadata 봉인, queue의 비종결 lease 정체, `inspect` 교차 불변식, SQLite 바인드 변수 상한을 확인해 코드·합성 테스트에 반영했다. source 포트의 제외 바이트 보존은 승인된 비삭제 정책에 맞고, 그 반대 문구를 수정했다. 최종 수정 부분의 추가 Opus 재리뷰도 실제 `claude-opus-5-5`였으며, `inspect()`가 열린 연결의 스키마 정의와 receipt proof를 다시 확인하지 않던 P2 두 건을 찾아 보강하고 합성 반례를 추가했다.

아래의 「역사 기록」 표와 선택지는 구현 전 설계 반례를 보존한다. 현재 보장 범위는 위 구현·합성 검증이며 성능, 자료 이관, 설치, 물리 삭제, 전원 손실은 제품 게이트로 남는다.

## 사용자 확정 계약 (2026-10-17 후속 승인)

- 다른 worktree의 **결과·후보·순위는 섞지 않는다**. 공유 SQLite 파일의 cache/WAL/lock에 따른 처리시간 측면 채널까지 차단하는 보안 격리는 요구하지 않는다. scope·세대별 검색 통계와 최종 결과 격리는 여전히 필요하다.
- 최신성 확인에 실패하면 오래된 결과나 묵시적인 부분 결과를 내지 않고 명시적인 **갱신 필요** 오류를 반환한다. 새로운 파일의 미탐지 창과 전체 재스캔 주기는 구현 검증에서 별도로 좁힌다.
- 색인 제외는 기존 pin·rollback 후보를 포함한 **검색 제공부터 차단**한다. 물리 삭제, 보존 기간, 파괴적 GC는 별도 정책 결정까지 보류한다. 이미 외부로 반환된 내용은 회수할 수 없다.
- 작업 큐는 중복 실행에 안전한 처리와 중단 복구를 설계한다. 분리된 저장소 사이의 exactly-once 또는 전원 손실 무손실은 검증 없이 보장하지 않는다.
- 격리 개발 폴더의 공식 패키지 레지스트리·고정 lockfile 의존성 설치는 허용되었다. 공급 출처·설치 script와 바이너리 경계를 확인해야 하며, 모델 실행·다운로드, 운영 전환·데이터 접근·배포는 허용 범위가 아니다.

아래의 과거 `결정 전 상태` 표는 이 승인 전 초안이다. 확정된 네 항목은 위 계약을 따른다. 구현되지 않은 기능과 남은 성능·schema·물리 삭제 정책은 완료로 표시하지 않는다.

## 구현 전 반례와 안전 기본값 (역사 기록)

| 영역 | 현재 증거와 반례 | 제품 연결 전 기본값 |
|---|---|---|
| 검색 범위·순위 | membership 필터는 다른 scope의 **결과 행**을 제외한다. 그러나 공유 `content_fts`의 BM25는 다른 scope, prepared·옛 세대, 철회 후 보존된 content의 통계도 사용한다. 같은 pin의 점수·순서·top-K가 달라질 수 있다. 범위 내 통계만 다시 계산해도 전역 `MATCH`나 공유 DB의 cache·WAL·lock을 통한 지연 채널은 남는다. | 프로젝트/worktree scope는 기본적으로 서로 기밀로 취급한다. **후보 탐색·순위의 scope/세대 격리**를 먼저 증명하고, 같은 DB 파일의 잔여 지연 채널 수용 여부 또는 기밀 도메인별 파일 분리를 사용자 결정으로 둔다. 결정 전에는 A-int 검색을 제품에 연결하지 않으며 전역 BM25 점수 공개·임계값 소비도 금지한다. |
| schema | 현재 trigger는 generation 상태 역행, membership/head/pin/receipt의 scope 결속, FK를 끈 connection, 가상 `content_fts`의 독립 변경과 다른 정의의 기존 테이블을 모두 막지 못한다. `content` UPDATE/DELETE trigger는 이미 있다. `IF NOT EXISTS`는 버전 검사나 migration이 아니다. | 명시적 새 DB 생성만 허용하고 기존·미지·더 새 버전 DB는 **쓰기 전에 변경 없이 거부**한다. 버전 표지, 단일 트랜잭션 초기화, scope/state 봉인, reader/writer 버전 fence를 제품 게이트로 둔다. 원형 v0 자동 업그레이드는 계획하지 않는다. |
| source 신선도 | `publish(expected, gen, claim)`은 후보의 base와 원문을 봉인하지 않는다. 오래된 prepared 후보가 새 expected로 다시 게시될 수 있다. `file_change_queue`의 nonce·snapshot과 성공한 일부 hook 이벤트는 완전한 단조 source sequence가 아니다. | 후보에 고정 `base_gen`, 완전 스캔의 manifest identity/scan epoch, 설정·파생 지문과 writer fence를 기록하고 **DB가** CAS에서 검증한다. 요청 시 반환 후보의 경로·바이트·권한을 다시 검사한다. 제공 가능한 raw 본문은 검증한 같은 버퍼에서 만들고, 실패·제외·부분 결과는 명시적 `index_stale`/`source_unavailable` 상태로 표면화한다. 이는 검사 시점의 증거일 뿐 이후 편집이나 누락된 새 파일을 막지 못한다. 미결정 시 제품 route 비활성이다. |
| queue ACK | A-int의 head·receipt는 한 DB 트랜잭션이다. fixture의 ACK queue는 별도 SQLite DB이고, 현재 `file_change_queue`의 pending 상태는 JSON 파일이다. 제품 연결할 실제 작업 저장소는 미정이다. 프로세스 종료 후 재전달은 합성 확인했지만 저장소 간 전원 손실 순서는 미검증이다. 현재 receipt에는 claim의 payload digest·lease epoch가 없다. | claim payload와 정체성, ACK 전후 상태를 보존한다. DB를 읽지 못하면 **DEFER**, receipt와 claim 증거가 정확히 맞으면 조건부 ACK, receipt가 없고 원본 작업이 남으면 재처리한다. queue의 `done`/삭제를 조기에 확정하지 않는다. 강한 DB 간 내구 계약을 정하기 전에는 제품 writer/ACK를 연결하지 않는다. |
| 철회·보존 | 현재 pin은 옛 세대 본문을 읽고 철회 필터가 없다. 삭제 없는 GC는 복구에 유리하지만 철회 바이트가 DB·WAL·FTS·백업에 남는다. 불변 FTS의 BM25 통계도 철회 뒤 계속 영향을 받을 수 있다. | opt-out·권한 철회·확정된 source 소실을 **현재 head, 옛 pin, rollback 후보, 위키 파생물**의 제공 경로에서 차단하도록 설계한다. 철회 commit 전 시작한 요청과 외부 authority 전달 지연의 창을 정의하며 “즉시 완료”를 주장하지 않는다. authority가 불명확하면 명시적 오류로 fail closed한다. 바이트 보존은 물리적 삭제가 아니며 삭제 의무가 있으면 별도 정책·승인 전까지 제품 연결을 보류한다. |

## 구현 전 검색·schema 선택지 (역사 기록)

- **검색:** 공유 FTS에서 `MATCH`한 뒤 scope로 거르는 것은 결과 행/상위 K의 범위 검사에 유용하지만 BM25 통계나 실행 비용을 격리하지 않는다. 점수만 제거하거나 문서 path 순서로 바꾸는 방식은 일부 채널을 줄일 뿐 기밀성·품질 계약의 대체물이 아니다. 제품 기본 요건은 scope·**generation**별 후보와 순위의 독립성이다. 구현 후보는 범위 밖 posting을 읽지 않는 scope·세대별 FTS 또는 기밀 도메인별 저장소다. 전역 `MATCH` 후 범위 내 통계만 다시 계산하는 방식은 순위 통계만 분리하고 실행 비용은 분리하지 못한다. 세대별 FTS는 토큰 저장량을 늘리지만 전체 DB 복사는 아니다. **엄격한 지연 비노출이 요구되면 같은 파일을 공유하는 A-int 자체가 제품 후보에서 탈락할 수 있다.** 철회가 생기면 불변 FTS의 내장 BM25에 철회 문서 통계가 남으므로 적격 집합 기준 순위 재계산 또는 철회 반영 새 세대가 준비되기 전에는 그 순위 결과를 제공하지 않는다. 물리 구조는 동일 fixture의 정확성, 지연, 저장량과 철회 시나리오를 측정한 뒤 선택한다. 점수 노출·hybrid 랭킹·허용 지연은 그때 결정한다.
- **schema:** 존재하는 파일을 열 때는 WAL 전환·DDL보다 먼저 read-only로 `application_id`, `user_version`, 객체 manifest를 분류한다. 새 DB는 명시적 `create`에서만 만들고 schema/meta/버전 표지를 한 트랜잭션에 기록한다. Python `sqlite3.executescript()`가 선행 트랜잭션을 커밋할 수 있으므로 초기화 구현 시 문장별 실행 또는 스크립트 내부 트랜잭션을 검증한다. generation의 허용 상태 전이와 scope 일치, head가 published 세대만 참조함, receipt 불변성은 FK 설정에만 의존하지 않는 제약으로 강제한다. open 시와 각 transaction 경계에서 버전을 확인한다. 알 수 없는 DB, 더 새 버전, 불일치 schema는 자동 수리·FTS rebuild·DROP 없이 거부한다. FTS 역색인 검사 명령은 쓰기 성질과 락을 임시 DB에서 확인하기 전에는 read-only `inspect`라고 부르지 않는다. 세대별 FTS를 택하면 객체 manifest는 고정 목록이 아니라 허용된 이름·정의 규칙과 예상 세대 집합을 검증해야 한다.
- **기존 원형:** 현재 v0 DB는 합성 데이터뿐이다. v0를 제품 DB처럼 자동 migration하지 않는다. 실제 사용자 DB 이관은 [별도 이관 계획](2026-10-17-embedded-engine-data-migration.md)의 inventory·복구 증거·승인 뒤 결정한다. 버전이 다른 프로세스의 동시 열기/업그레이드는 제품 경로를 멈추고 writer quiescence를 증명해야 한다. 새 코드의 fence만으로 옛 코드의 쓰기를 막았다고 주장하지 않는다.

## source 게시와 요청 제공의 서로 다른 시계

1. 후보는 stage 시작 당시의 `base_gen`, 불변 worktree/collection identity, 승인·설정 revision, 원문 파일 집합과 각 바이트 해시, parser/chunk/model 지문을 봉인한다. `stage` 재전달은 이 전체 identity가 같을 때만 멱등이다. git HEAD나 mtime은 단조 시계가 아니다.
2. hook 이벤트는 외부 편집·타 프로세스의 Git 작업·실패한 명령의 변경을 완전히 관측하지 못한다. 현재 nonce/dirty queue를 watermark라고 부르지 않는다. 제안된 W는 **완전 스캔을 기록한 authority의 incarnation과 단조 scan epoch + source manifest identity**다. 이벤트는 재스캔을 앞당기는 힌트다. 후보의 W, head의 W, 봉인된 base와 index DB의 정확한 writer fence를 한 게시 트랜잭션에서 비교한다. authority가 재생성되거나 스캔이 불완전하면 순서를 추측하지 않고 보류·재스캔한다. 늦은 writer가 새 head를 읽어 `expected`만 바꿔 재시도하는 것은 거부한다. 이 구조도 스캔 직후의 파일 변경을 막지는 않는다.
3. `lstat/open/fstat/read/fstat`와 같은 경계 검사, 같은 버퍼의 hash, 게시 직전 재확인은 흔들리는 파일을 찾는 데 유용하다. **마지막 확인과 commit 사이에도 파일은 바뀔 수 있다.** 완전 스캔 W가 있어도 보장은 “그 스캔에서 관측한 manifest의 게시”이지 “commit 순간의 최신 바이트”가 아니다. 스캔 뒤 변화의 발견은 다음 재스캔 또는 요청 시 검사에 달려 있으며, 미탐지 창과 검색 완전성은 별도 결정이다.
4. 제공 경로에는 기존 `qmd_scope.scoped_hits`의 **반환 결과 단위 현재 파일 해시 재검사**를 참고한 gate가 필요하다. 기존 gate의 선택적 drop 진단, 범위 불가 시 `[]`, 제외 뒤 top-K 미보충이라는 **조용한 부분 결과 의미론은 그대로 이식하지 않는다**. A-int는 실패·제외 건수와 이유를 응답 상태로 노출하고, raw 본문은 검증한 같은 버퍼에서 제공한다. 위키 같은 파생 본문은 각 source의 현재 provenance와 철회를 별도로 확인한다. 옛 pin의 hash가 현재 바이트와 다르면 pin만으로 제공하지 않는다. 검사 이후 편집, 반환되지 않은 새 파일, 이미 주입된 컨텍스트는 해결하지 못한다.
5. pin은 membership 세대의 일관성을 위한 것이다. source 변경·철회 뒤의 제공 허가가 아니다. 확인된 삭제/opt-out은 옛 pin에도 적용한다. 읽기 실패는 삭제의 증거가 아니지만 해당 결과의 제공 근거도 아니므로 보류한다. 이미 응답으로 나간 본문은 회수할 수 없으며 하류 컨텍스트·캐시·학습의 무효화 계약이 별도로 필요하다.

## 별도 queue의 재전달과 전원 손실 한계

| 재시작 시 관측 | 제안된 판정 |
|---|---|
| index DB·schema·receipt를 판정할 수 없음 | `DEFER`와 진단. 부재로 간주해 ACK하거나 동일 작업을 추측 실행하지 않음. |
| 같은 claim identity·payload digest·fence의 게시 receipt 확인 | head가 후속 세대로 전진했더라도 그 효과와 신선도 불변식을 검증한 뒤 **조건부 ACK**. 중복 게시 없음. |
| 정상 조회에서 receipt 부재, 원본 claim/payload 보존 | 후보 base·source identity를 다시 검증하고 재처리. 새 작업이 들어왔으면 옛 ACK는 0행이어야 함. |
| ACK 상태가 보이지만 receipt가 없거나 둘의 내구 결과가 어긋남 | ACK를 완료로 취급하지 말고 payload 보존 상태에서 `ack_without_receipt`로 격리·재조정. 원본이 없으면 자동 성공/삭제 금지. |
| claim incarnation·epoch가 뒤처짐 | fenced 또는 불확실 상태로 보류; 새 owner만 처리. queue 재생성·역행이면 전체 재스캔 필요. |

`acked_unverified`와 원본 payload 보존, 독립 재시작 뒤 receipt 재조회는 **설계 후보**다. 프로세스 kill/예외 합성 검사는 정상 OS의 미커밋 rollback과 ACK 재전달만 말한다. 실제 작업 저장소가 별도 DB이든 JSON 파일이든 기록 순서·fsync·directory sync·WAL checkpoint와 전원 손실 뒤 내구 상태는 미검증이다. 재시작 직후 receipt가 보인다는 사실은 저장장치 내구성 증명이 아니다. ACK가 삭제형이면 불일치 감지 자체가 불가능하므로 claim identity·digest·fence를 남기는 tombstone과 원본 payload 보존이 먼저 필요하다. 세대 ID에는 DB incarnation을 결속하고 역행 감지 시 하류 파생물 무효화·source 재스캔을 계획한다. `fullfsync` 설정만으로 저장소 간 원자성이 생기지 않는다. 제품 연결 전에는 queue claim/ACK를 index DB와 같은 원자 경계에 둘지, 아니면 복구 가능한 payload와 전체 재스캔으로 어떤 손실 상한을 허용할지 결정하고 검증해야 한다. **별도 저장소 reconciliation만으로 전원 손실 안전을 주장하지 않는다.**

## 철회와 데이터 보존

stage 전에 승인된 source root/파일 종류·ignore 정책·크기·안전한 경로를 검사하고, 불확실한 입력은 저장하지 않는다. 비밀 패턴 탐지는 보조 신호일 뿐 완전한 유입 차단 증거가 아니다. 철회 authority와 scope별 append-only 제공 차단 기록을 계획한다. 요청의 같은 snapshot에서 **후보 선택·순위 모집단·LIMIT보다 먼저** 철회를 적용해야 하며 FTS·사전계산 vector·wiki·old pin/rollback이 공통 적격 집합을 사용해야 한다. 불변 FTS의 내장 BM25가 철회 바이트를 계속 집계하면 해당 세대의 순위 결과는 보류한다. 출처가 불명확하거나 권한 정보를 읽을 수 없으면 본문을 제공하지 않고 오류를 낸다. `serve_until` 같은 pin 제공 만료는 권한 상한일 뿐 GC 생존 판정이 아니다. 요청 시작 뒤 철회가 commit될 수 있으므로 출력 직전 최신 epoch 재확인과 진행 중 요청을 추적·종료시키는 장벽을 별도로 설계한다. 재확인과 출력 사이에도 경합이 남고 이미 나간 결과는 회수할 수 없다. 철회 완료 통지는 authority의 commit·엔진 전달·진행 중 요청 종료/취소를 확인한 뒤에만 허용한다.

확정된 source 삭제는 옛 본문 제공을 막되, 일시적 읽기 실패를 “삭제 확정”으로 기록하지 않는다. rollback은 옛 generation id를 재선택해 삭제·철회된 경로를 되살리지 않고, 필요한 경우 검증된 현재 정책으로 **새 세대**를 만든다. 철회 이후의 학습/평가 재현 요청은 옛 본문을 다시 내보내지 않고 `invalidated_by_revocation`으로 표면화한다. 보존된 바이트 자체가 삭제 의무와 충돌할 수 있으므로 원본·공유 content·FTS·WAL·백업·하류 파생물의 물리 삭제 범위/기한은 별도 사용자 결정이다. 정책이 없을 때 파괴적 GC·자동 삭제·FTS rebuild는 하지 않는다.

## 구현 전 단계 제안과 사용자 결정 (역사 기록)

| 순서 | 합성 코드/fixture로 설계 후 구현 가능한 것 | 제품/운영 경계에서 필요한 결정·승인 |
|---|---|---|
| 1 | 전역 BM25 순서/점수 변동 및 scope 누출 반례, scope·세대 한정 후보/순위 원형의 정확성·비용 비교. | scope 간 기밀성 기본값을 유지할지, 검색 품질·허용 지연·저장량 기준, 점수/서수 API와 hybrid 랭킹 정책. 미결정 시 A-int 검색 route 비활성. |
| 2 | 명시적 v1 생성·버전 거부·원자 init·봉인·동시 open/kill/미지 schema 합성 검사. 기존 v0는 보존. | 실제 후보 DB 경로/설치 schema, 버전 migration 대상·호환성, 사용자 데이터 inventory·이관·복구 승인은 별도. |
| 3 | 봉인된 base/source manifest와 합성 scan epoch/fence, 늦은 writer·재스캔·반환 결과 gate의 반례. | 재스캔 주기와 미탐지 창·부분 검색 허용 여부, hook/queue 연결과 실제 host 검증. 완전한 hook 이벤트 관측을 전제로 하지 않는다. 미결정 시 오래된 결과 제공 금지. |
| 4 | 정확 claim identity의 receipt/조건부 ACK, 불확실 상태·소유 프로세스 종료·모의 DB 역행을 임시 DB에서 검증. | queue를 같은 DB로 옮길지, 별도 DB의 복구/데이터 손실 허용 한계, 실제 전원 손실·설치 시나리오. 미결정 시 제품 writer/ACK 비활성. |
| 5 | 범위별 제공 차단·옛 pin/rollback·철회 authority 장애·동시 read의 합성 반례. GC 삭제 없음. | 무엇이 철회인지(원문 삭제·opt-out·카드·비밀 유출), 완료 시점과 진행 중 요청, 보존 기간/물리 삭제 의무·승인자, 학습 재현 예외. 미결정 시 제공 거부·데이터 보존·제품 연결 보류. |

### 확정 계약과 남은 제품 게이트

| 항목 | 상태 |
|---|---|
| scope 간 결과·후보·순위 | 격리 확정. 동일 DB의 처리시간 측면 채널 차단은 요구하지 않음. BM25 범위별 정확성 검증은 남음. |
| 최신성 실패 | 명시적 갱신 필요 오류 확정. 미탐지 창·재스캔 주기·요청 일관성 검증은 남음. |
| 색인 제외·철회 | 옛 pin까지 제공 차단 확정. 물리 삭제·백업·WAL 보존 정책은 미정이며 파괴적 GC 보류. |
| queue | 중복 안전·중단 복구 확정. 별도 저장소 전원 손실/원자성 보장은 미검증. |
| schema·패키지·이관 | v1 생성/거부/봉인, 독립 빌드·패키지 경계, 실제 데이터 이전 승인과 운영 전환은 별도 게이트. |
| 성능·평가 | scope별 FTS/vec 규모와 점수/API, pin 수명, 모델 지문 및 학습 재현은 별도 검증·결정. |

왼쪽 열의 합성 원형도 hook·설치본에서 import되지 않고 릴리스 패키지에 포함되지 않는 격리 경계를 확인한 뒤에만 후속 작업으로 착수한다. 배포물 포함은 별도 승인 대상이다. 이 순서는 모델/임베딩 실행, 운영 DB·원고·사용자 설정 접근, hook/설치본 변경, 배포의 승인이 아니다. 격리 개발 폴더의 lockfile 의존성 설치 승인만 위 확정 계약에 따른다. 이 문서는 구현되지 않은 정책을 현재 A-int 또는 기존 QMD의 보장으로 승격하지 않는다.
