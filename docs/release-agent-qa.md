# Release agent QA gate

Every version release must pass this agent-executed QA gate on the **final
installed candidate**. The gate does not rerun unit tests. It is a set of
reproducible scenarios that an operator agent performs in fresh, real Claude
Code and Codex host sessions, against disposable synthetic fixtures. Each
scenario captures objective evidence. Record each run with
[the run record template](templates/release-agent-qa-run.md). The
[release checklist](release.md) links here.

## Status of this document

**2026-10-20 `40e500c` Claude 프로젝트 설치 smoke (L1 PASS 아님):**
사용자가 승인한 기존 합성 QA 프로젝트에 후보
`40e500cb44e32ab62e05f14d75113dd4d59d3534`를 Claude 공식 CLI로
project scope 설치했다. 별도 `qmd-laya-40e500c-qa` marketplace를 등록하고
이전 `4dde371` project entry를 비활성화했다. 사용자 전역 설치는 유지되며
이 QA 프로젝트에서만 비활성 상태다. 설치 cache와 **실제 Claude가 실행한
marketplace source 경로** 모두 Git tree의 915/915 파일 내용·실행 권한과
일치했다(guard `7f6478a5e78c46d688429660aaf961e7`,
`239e566a186e4966affbc7f4b3d9b20e`, 각각 passed/settled).
새 Claude Opus 세션에서 SessionStart의 `topical-reconcile`·`update-queued`,
UserPromptSubmit의 `recall`, Stop의 `topical-stop`이 모두 그 source의
`hooks/run-hook`에서 exit 0으로 실행됐다. 이전 entry의 hook 실행은
관측되지 않았다(guard `1d6759402beb4a66bfe2366cdb1aff4e`,
passed/settled). 응답은 합성 표식 `QA_LOAD_40E500C`였고, 실제
`claude-opus-5-5` 사용량은 input 2, output 14, cache create 65,787,
cache read 0, thinking 0 token, web 0회였다. 증거는
`laya-l1-gate-4dde371-qa/evidence-40e500c/`의
`installed-identity.json`, `loaded-identity.json`,
`claude-load-evidence.json`, `claude-load-debug.log`에 있다.
합성 프로젝트에는 `.auto-context` 설정이 없어 setup notice
`settings_not_ready`가 정상 표시됐고, 새 hook trust를 승인하거나
provider를 활성화하지 않았다. 이 smoke는 전용 OS 계정/VM의 정식 L1,
설치 소유 journal·DB/queue incarnation, 실제 QMD 이관·검색·학습,
Codex hook 검증을 충족하지 않는다. 해당 행은 `NOT RUN`이다.
현재 exact head의 Node 22/24/26 CI는
[run 39448832807](https://github.com/zbdulee/qmd-auto-context/actions/runs/39448832807)에서 성공했다.
이 기록을 소스에 반영하면 Git tree가 바뀌므로, 위 설치의 exact identity와
CI는 계속 **커밋 `40e500c`에만** 귀속된다.

**2026-10-20 설치 후 읽기 전용·합성 게이트 추적:** 실제 로드된
marketplace source에서 `select_route()`는 `legacy/no_route_record`를 반환했다.
이 합성 프로젝트에는 Laya route, 설치 journal, index DB, claim queue가
없다. `update-queued` hook 자체는 exit 0이었으나, 비설정 프로젝트의
background update status는 `failed`이고 등록 안내를 남겼다. 따라서
hook 로드는 확인했지만 검색·설치·큐의 제품 동작으로 세지 않는다.
package/journal/DB/queue 및 두 후보의 중단·복구 합성 검사는 2/2 통과했다
(guard `669202a15e1545a69a18cf0475d4b165`, passed/settled).
fixture 자체는 `FORMAL_RELEASE_L1=NOT_RUN`으로 표시한다.

**2026-10-21 후속 제품 연결 (새 릴리스 후보 아직 미확정):**
기존 selected shared QMD의 `bootstrap-proof.json` 부재를 사실대로
유지하면서, 별도 비활성 replacement 세대의 실제 QMD 검색·벡터
증명을 Laya 후보 모델의 입력으로 연결했다. 읽기 전용
`replacement_model_evidence()`는 journal·GGUF 경로/SHA/크기·768차원·
managed QMD runtime·선택된 옛 포인터의 inode와 SHA·원본 DB 경로·
프로젝트 설정 SHA를 다시 검증한다. Provider proposal과 초기 빌드
기록, v3 cutover ledger는 이 evidence digest를 결속한다. 설치 준비와
cutover 직전에는 현재 증거와 QMD 원문·위키 백업, 새 세대 seal,
provider attestation을 재검사한다. 이전 QMD 벡터를 새 모델에서
나온 것으로 주장하거나 복사하지 않는다. 옛 QMD와 별도 global
QMD 및 백업은 보존한다. 선택된 old QMD의 setup readiness는
여전히 proof-required다.

Claude Opus의 좁은 설계 검토는 guard
`42267a987e194d438610cd09195ea2fa` (`passed/settled:true`,
`claude-opus-5-5` modelUsage input 2/output 5,468/cache create 1,533/
cache read 2,061/thinking 3,214/web 0)에서 공유 QMD 포인터를 또
바꾸는 두 번째 cutover를 피하고, 기존 QMD→Laya v3 프로젝트 전환에
비활성 모델 증거를 묶도록 제안했다. 첫 합성 focused run
`7ea7ddb0fa6b48708f99ac382e684d0d`는 shared QMD 45건과
v3 bound 검사는 통과했으나 새 fixture의 오류명 기대와 기존
initial-build fixture mock 연결 두 곳이 실패했다. 이를 수정한
재검사 `1b7d8cfebfdd473b86ad1378bf10c88d`에서 initial-build
21건과 새 replacement/v3 증거 10건이 모두 PASS,
`passed/settled:true`였다. 추가 합성 15건을 포함한 focused run
`5f1c75ac27ba418ab391be8635d38685`도 PASS,
`passed/settled:true`였다. 첫 전체 suite는 시스템 Python에
PyYAML이 없어 wiki 두 건이 실패했다. 기존 venv의 PyYAML
site-packages만 `PYTHONPATH`에 더한 별도 47건 검사는
`5ff1d58e922f408684255dd8e3140231`에서 PASS했고, 전체
재검사 `a0d0ecbf89784b50bcd52346cebc91da`는 1,650건 중
1,641 PASS·9 SKIP·0 FAIL (`passed/settled:true`)이었다.
이는 이후 리뷰 수정 이전 소스이므로 최종 전체 회귀는 다시 필요하다.
이전 head `753bc2f`의 CI [39514146609](https://github.com/zbdulee/qmd-auto-context/actions/runs/39514146609)는
Node 22·24·26 모두 성공했지만 이 변경의 CI가 아니다.
Claude Opus는 `bb1087dd4ee942408fd681016387ba91`와
`cc61044ea0a94c829ec29b36dd83770e`에서 replacement 증거와
cutover를 독립 검토했다. 이어 `a0003238320e48a186ec56dbfa5a461d`
(`model-test`, `passed/settled:true`, modelUsage input 2/output 8,705/
cache create 6,323/read 2,061/thinking 6,303/web 0)은
prepared 결속의 증명 상실 뒤 철회, drift 철회의 CAS→record
중단 재생, 직접 route 변경 호출의 직렬화, sealed build pointer의
마무리를 지적했다. 이를 고친 후 `9826a1c547d449718baa15f84ac700a1`
(`model-test`, `passed/settled:true`, modelUsage input 2/output 11,494/
cache create 8,333/read 2,061/thinking 9,531/web 0)은 앞선
세 경계의 수정을 확인하고, 동일 digest의 QMD inode 교체와
terminal 은퇴 뒤 나중 설치가 진행된 경우의 재생을 추가 지적했다.
마지막 수정은 `source_identity_changed`를 ledger/receipt에 남기고,
이미 terminal인 ledger의 record·marker 정리를 이전 journal이나
나중 route에 종속시키지 않는다. 자체 marker와 route가 동시에
있으면 불확실성으로 거부한다. 이 마지막 변경의 합성 22건은
`ffc328d95e774d65b1d5f05fc82ec33a`에서 PASS,
`passed/settled:true`였다. 앞선 focused run
`f8bcd376898643dda30180e5df9d665f`는 새 fixture가 나중 marker를
비정규 JSON으로 써서 1건 실패했으며 제품은 올바르게 거부했다.
fixture만 canonical 형식으로 고쳐 재실행했다. 최종 전체 suite는
기존 venv의 PyYAML import 경로만 제공해 guard
`6d5361a6a0234ca4ba1ebe5607e770c5`에서 1,650건 중
1,641 PASS·9 SKIP·0 FAIL (`passed/settled:true`)였다.
새 exact-head CI, installed-candidate LI-MIG/LI-RECOVER/LI-RECALL은
별도로 기록해야 한다. 실제 운영 이관·provider 활성화·main 병합·
Mini 설치는 이 합성 결과로 수행하지 않았다.

최종 코드 응답에서 Claude Opus는 추가 P1/P2를 찾지 않았다
(`claude-opus-5-5` modelUsage input 2/output 8,379/cache create 9,078/
read 2,061/thinking 6,918/web 0). 다만 결과 파일이 작성된 직후
도구 대기가 중단되어 해당 실행의 원래 checkpoint는
`running/settled:false`였다. 기록된 supervisor·자식 PID 4개와
프로세스 그룹의 부재를 읽기 전용으로 확인한 뒤, 해당 가드 실행
`5eace2009a9847478d4e73784f0f0382`만 `stopped/settled:true`,
`completed:false`로 정리했다. 이를 `passed` 검증으로 기록하지 않는다.
소스의 최종 전체 회귀와 CI는 별도로 확인한다.

원래 328,576,992바이트 GGUF가 필요한 기술적 이유는 **기존 검색
행태와 동일 모델이라는 주장 및 parity 분석**이다. 같은 이름이나
다른 크기의 GGUF는 동일 모델 증거가 아니다. 더구나 원래 파일을
찾더라도 QMD의 이전 벡터 fingerprint만으로 그 파일에서 각 벡터가
나왔음을 암호학적으로 증명할 수 없으므로 기존 벡터를 이관하지
않는다. 별도로 정확히 검토·승인된 GGUF는 그 자체의 SHA/크기로
새 Laya 문서·질의 벡터를 만들 수 있지만, 이전 모델로 취급하거나
미실행 retrieval parity를 PASS로 기록하지 않는다.

선택된 플러그인 shared QMD 세대의 읽기 전용 선택은 성공했지만
`bootstrap-proof.json`, `bootstrap-journal.json`, `scope-policy.json`이
없고 현재 선택된 cache에서 GGUF를 찾지 못했다. 실제 Claude 로드 경로와
canonical private state 경로로 provider 검토를 재시도한 결과, 세대의
`bootstrap-proof.json`을 읽는 지점에서 차단되어 proposal digest는 없다
(guard `6b6ba2132b6b41cea09e3331c696fed5`, passed/settled;
검토 결과는 **blocked**). `qmd_model_reason()`도
`qmd_embedding_model_proof_required`를 반환한다. 이전 smoke의
328,576,992바이트 모델과 별도로 발견된 333,590,944바이트 GGUF의
동일성은 입증되지 않아 대체하지 않았다. 기존 선택 세대에 증명 파일을
임의 생성하는 것은 정상 복구가 아니다. 새 `plan-replacement-store`와
`prepare-replacement-store`는 선택된 pointer·managed runtime·설정된
모델 이름·프로젝트 scope의 입력 동일성을 확인하고, 사용자가 명시한 GGUF
경로·SHA256·크기와 승인한 계획 digest에 결속된 별도 journal에 빈 비활성
세대의 실제 QMD vector/search 증명을 남긴다. 합성 fixture에서는 기존
선택 세대와 원본 journal·DB·설정의 보존, 빠진/다른 모델의 차단,
pointer 변경·세대 ID 충돌·중단 뒤 재개를 확인했다. Claude Opus의
독립 리뷰가 짚은 replacement journal supersession과 runtime wrapper
결속을 보강한 뒤 합성 검사는 43/43 PASS
(guard `84594f6be1c64d9d8c2a9f7e5414c42f`, `passed/settled:true`)였다.
보강 후 인접 설치·공유 저장소·cutover 검사는
39/39 PASS (guard `904dc9f6a4144aa88ec1bad525857fc9`,
`passed/settled:true`)였다. 같은 요청의 실패 journal 재시작과 runtime
pointer 직접 결속을 추가한 뒤 합성 검사는 45/45 PASS
(guard `eba1930e922e48c78de2aa667c489361`, `passed/settled:true`)였다.
이 마지막 좁은 변경 뒤 인접 39건은 재실행하지 않았으며, 전체 exact-commit
CI가 아직 필요하다. 이 경로는 운영
세대의 proof를 위조하지 않으며 새 세대를 선택하거나 기존 QMD 자료를
이관하지 않는다. 현재 설치된 `40e500c`에는 이 새 경로가 없고, 이
문서의 기존 설치 smoke와 CI는 새 후보를 검증하지 않는다. 동일성이
확인된 원래 모델의 명시 경로, 실제 데이터 백업·이관·복구 검증 및
그 준비 경로 자체에는 당시 product cutover가 없었고, 위의 후속
제품 연결이 기존 QMD→Laya v3 project route를 활용한다.
명시 모델 경로만으로 provider 제안을 얻어도 shared QMD의 setup readiness
증명 누락은 별개로 남는다.

Claude Opus 구현 제안은 guard `cc546f5e31c04cd4b3e2d55fddce037f`
(`passed/settled:true`, modelUsage input 2/output 7,332/cache create 1,582/
cache read 2,061/thinking 1,778/web 0)에서 받았다. 첫 코드 리뷰는 guard
`7831210978c54262ac574c86c05ebaa0`
(`passed/settled:true`, modelUsage input 2/output 12,635/cache create 15,340/
cache read 2,061/thinking 9,920/web 0)에서 journal liveness와 runtime
결속 결함을 찾았다. 전체 diff 후속 리뷰는 guard
`3b23e22c5593415a8d37f5206ae8cd42`에서 180초 시간초과로
`failed/settled:true`이며 검토 결론이 없다. 좁힌 후속 리뷰는 guard
`df9cceae50434068bab568a945529863` (`passed/settled:true`, input 2/
output 6,439/cache create 5,756/cache read 2,061/thinking 4,406/web 0)에서
같은 요청의 실패 checkpoint 재시작 결함을 찾았다. 수정 후 마지막 좁힌
리뷰는 guard `8c1dd37a30be42e789544c925a4fe124` (`passed/settled:true`,
input 2/output 4,554/cache create 4,665/cache read 2,061/thinking 3,707/
web 0)에서 **보여 준 수정 범위에 P1/P2가 없다고** 판정했다. 이는 전체
릴리스 후보의 독립 승인이나 설치 QA가 아니다.

첫 후속 PR head `e1fc313`의 CI run `39511702028`에서 Node 22가 실패했다.
`shared-install-resilience`의 설치 중단 fixture가 기존 두 인자 `_save`
호출을 모의하는데, 새 공통 `_build`가 legacy 경로까지 세 인자로 호출한
회귀였다. Legacy 호출 형태를 복원하고 replacement 경로에만 journal 이름을
추가했다. 정확한 실패 케이스와 shared QMD 45건은 단일 가드
`4408de69e3fe44cf8927e212d60772dc`에서 Node 5/5 PASS,
`passed/settled:true`로 재검증했다. 이 수정은 `e1fc313` 뒤의 소스
변경이므로 새 exact-commit CI가 필요하다.

이 복구 변경은 기존 `RQA-RES-02`의 별도 안전 범위 학습 검증을 바꾸지
않는다. 다중 PC 시험이나 검색·embedding의 별도 부하 큐는 새 필수
릴리스 게이트로 추가하지 않았다.

| 분류 | `40e500c` 현재 결과 |
|---|---|
| 완료 | exact head CI Node 22/24/26, Claude project-scope 설치·실제 로드 identity 915/915, 위의 합성 검사·읽기 전용 상태 확인. |
| 실패/차단 | 비설정 합성 프로젝트의 update worker status `failed`(예상된 등록 안내); 실제 provider 검토는 proof 부재로 차단, digest 없음. 운영 전환 실패를 의미하지는 않는다. |
| 미실행 | 전용 환경의 정식 L1, Codex 설치 훅·신뢰·RQA-HOOK-02, 설치 소유 journal/DB/queue incarnation, 실제 QMD 이관·검색 동등성, 자원 압력 아래 학습 재개, main 병합·정식 릴리스·Mini 설치·운영 활성화. |
| 사용자 한시 면제 | 이번 후보의 Mini 관리자 로그인 필요 QA만 `USER_WAIVED/NOT RUN`; 다른 필수 행은 면제되지 않았다. |

합성 자료만 전달한 Claude Opus 독립 검토는 guard
`365569afbaa54a568d2c02a1e9da000d`에서 passed/settled였고 실제
`modelUsage`는 input 2, output 2,947, cache create 2,805, cache read 649,
thinking 1,405 token, web 0회였다. 검토의 “정식 릴리스 승인이 없었다”는
판정은 이전 사용자 승인 기록과 맞지 않아 채택하지 않았다. 출처·상태 요약은
합성 QA 폴더의 `evidence-40e500c/read-only-gate-inventory.json`에 남겼다.

**2026-10-18 설치 후보 중간 기록:** draft PR #12의 `c18067edcc02057efe26956aa5ea8a210a646c7e`를
전체 Git tree 862개 파일 그대로 별도 Claude/Codex 로컬 marketplace에 설치했다.
두 설치 cache와 commit의 blob/mode가 862/862 일치했고, manifest SHA256은
`670f6fc8c6d514287964003308b7449ad2854f996c0f9c57f22f5643f18bf1e0`이다.
새 Claude 세션은 HOME 하위 합성 pending 프로젝트에서 Edit를 거부했다
(`1de3da85dab74392a047852c010a09dc`, passed/settled). 같은 조건의
Codex `exec -s workspace-write` 세션은 `apply_patch`를 실행해 합성 README를
바꿨으므로 **RQA-HOOK-02 실패**다. 직접 호출한 제품 gate는 deny를 반환했고,
후보의 Codex hook trust 기록은 없었다. 이후 `CODEX_SANDBOX`가 gate까지
조기 종료시키던 코드도 수정해 합성 gate 회귀 30/30을 통과했다
(`710dfa04a08649c1bfe2f893ddc45466`). 이 수정은 `c18067e`에 포함되지
않으므로 새 후보의 실제 Codex hook 실행 및 신뢰 확인이 필요하다. 신규 hook
신뢰를 우회하거나 승인하지 않았다.

같은 설치 후보에서 기존 로컬 328,576,992바이트 모델과 QMD 2.5.3/
node-llama-cpp 3.18.1의 native closure 7개를 **추론 없이** 검토해
`review_required` 제안 digest
`30faa6d37df0eee1bbb6aa4d456a39ff522abd31316517b38885237f5f63ed05`를
생성했다(guard `c7d7a6d4b8544cc79f0b01b54a537aaa`, passed/settled).
이것은 provider 승인·선택·최종 후보의 모델 QA가 아니다. 현 L1 상태는
**부분 실행·미완료**이며, 실제 QMD 이관/복구, 선택된 검색, 학습 중
압력 증가와 재개, 최종 exact 후보의 Claude/Codex 실행이 남았다.
따라서 main merge와 Mini 설치는 보류한다.

This spec was written on 2026-10-17 on the local, unpushed branch
`feat/same-session-worktree-freshness`. The synthetic fixture helper passed
its L0 guarded test, which ran Git only inside disposable test directories.
No installed-candidate host scenario, QMD, model, hook or installer was run.
The host scenarios are `NOT RUN`, apart from the documented `BLOCKED` race
barrier and learning conditions. This paragraph does not record L1 QA.

2026-10-18 Laya v4 추가 기록: 임시 Git 후보를 복사한 **L0 개발 fixture**에서는
제품 hook의 차단, 별도 개발 전용 합성 벡터의 edit→sync→query 및 rollback
거부를 확인했다(가드 `1e2bdde46fe94d1eb62449a3ed998bc9`, Node 1·Python 2,
`passed/settled:true`). 정식 marketplace 설치나 새 실제 host 세션은 아니므로
아래 L1 행은 모두 `NOT RUN`이다. 이 fixture에서는 모델 추론을 실행하지 않았다.
별도 제한된 임베딩 기능 smoke는 가드
`af90592d823d482ba8318de9016a010b`가 `passed/settled`로 끝났고 합성 입력
2회, 결과 차원 768을 관측했다. backend는 Metal로 표시됐지만
`gpuLayers=0`이므로 모델 GPU offload 증거는 없다. 5초 간격 샘플링으로 peak
RSS도 측정되지 않았다. 이 기능 smoke는 제품 provider 승인이나 L1 설치 검증이
아니다. 원시 실행 로그가 별도 보존됐다는 주장은 하지 않는다.

### Laya Index 전환 후보 추가 필수 행 (2026-10-18, 전부 NOT RUN)

**통합 작업 체크포인트 (L0, 설치 후보 아님):** 제품 승인 항목의 자기참조를
제거하고, exact Git tree 복사본과 설치 소유 journal에 외부 provider attestation
digest를 묶었다. 선택된 hook의 질의는 예열된 소유 worker를 사용하고 update/index는
durable sync wake를 남긴다. QMD 원본·백업을 보존하는 cutover/복구 상태도
합성 fixture에 연결했다. 집중 회귀 14/14는 guard
`36d1092013a04de59b034f787a3cd2f9`에서 통과했다. 전체 회귀의 첫 실행은
시스템 Python의 PyYAML 부재로 실패했고, 같은 Python에 기존 테스트 venv의
PyYAML import 경로만 더한 집중 재검사는 47/47 통과했다(guard
`dc86865c203243f98c49a35f6c2dac2e`). 아래 모든 L1 행은 계속 `NOT RUN`이다.
후속 제목 결속 v5는 동일 본문·다른 QMD 제목을 별도 벡터로 보관하고,
완전한 청크 집합을 원자 봉인해 재시도에서 재사용한다. 검증된 집합 밖의 청크와
불연속 sequence는 세대 seal이 거부한다. QMD RRF의 첫 유효 목록 가중치·상위
순위 보너스·`1/rank` 점수 의미도 반영했다. 통합 집중 검사는 8/8 통과했다
(guard `1af84e0c7e56489fa24a1429c88d62b7`). 독립 Opus v5 리뷰는
`claude-opus-5-5`를 사용했고 실제 `modelUsage`는 output 8,545 token,
cache read 1,960 token, cache create 23,131 token이었다(guard
`b414d9b1defa4071af9f67921ea2a14b`, passed/settled). 코드·합성 자료만
전달했고 모델 추론/다운로드는 하지 않았다.
최종 모델 제외 전체 회귀는 기존 venv의 PyYAML import 경로만 시스템 Python에
더해 실행했고 guard `d372c3eda9844b3ba3f516f7e34e5e62`에서
`passed/settled:true`, exit 0으로 끝났다. 전환 fixture의 v5 제목·청크 집합
계약 누락은 각각 집중 가드 `1cb5df21e4ee45db8193faab97a0f21f`와
`b78ecfa76cb64a3cb4143bb4d3b8f11a`에서 수정 후 통과했다.

후속 L0 통합에서는 두 QMD 컬렉션(raw·wiki)의 read-only inventory·백업·후보
봉인·전환 복구, selected wiki compile, QMD 형식 FTS 및 계층 recall, 짧은 소유
상태 경로의 설치·worker 준비를 합성 fixture로 연결했다. 모델/native closure 검토
자료 생성기도 구현했지만 실제 설치·선택·모델 실행 증거를 대신하지 않는다. 전체
회귀는 1,621건 중 1,612 PASS·9 SKIP·0 FAIL, guard
`95a30e34baed4d6abe090ad0f52bd06f` `passed/settled:true`였다. 이 결과는
L0 소스 검사이며 최종 후보의 L1 행은 여전히 모두 `NOT RUN`이다.
QMD 전환 복구만 대상으로 한 Opus 소스 리뷰는 guard
`ead1e6624d634cadbed8b47fcafbc4ec`에서 `passed/settled:true`였고
`claude-opus-5-5`의 실제 `modelUsage`는 output 6,614, cache read 1,960,
cache create 4,779 token이었다. 설정 변경 뒤 switching 고착, pending route digest
재확인 등 지적을 합성 사례로 수정했고 전환·설치 집중 3/3은 guard
`9be9f9657a674b5aa6402e8c66fc487a`에서 통과했다. 실제 QMD 데이터와
모델은 이 리뷰에 제공하지 않았으며 L1 상태를 바꾸지 않는다.

Laya Index로 QMD backend를 교체하는 릴리스에는 기존 L1 행에 더해 **설치된 정확한 후보**에서 다음을 필수로 기록한다. 임시 Git full-plugin 복사본과 합성 attestation·이관 리허설은 marketplace 설치 후보의 fresh-host 증거가 아니다. 실제 설치 후보의 provider 승인과 선택은 아직 수행되지 않았다. 아래 행은 모두 `NOT RUN`이며 L0 통과로 대체할 수 없다.

| 행 | 설치 후보에서 확인할 실제 결과 |
|---|---|
| LI-INST | exact-commit bundle·설치 소유 journal·현재 파일 identity·DB/queue incarnation을 새 Claude/Codex 세션이 검증하고, 일치하지 않으면 QMD로 fallback하지 않고 차단한다. |
| LI-MIG | 선택된 프로젝트의 QMD 설정·원문·위키·색인 범위를 read-only inventory로 고정하고, 일관된 백업을 별도 DB로 복원해 비교한다. 새 Laya 후보에는 **위키 문서만** membership·본문·FTS·벡터로 재색인하고 원본 원문은 출처·최신성 증거로만 확인한다. 불일치/중단 시 이전 선택이 유지된다. |
| LI-SCOPE | opt-out, worktree 분리, 삭제·rename, 중복 claim, 오래된 writer, 옛 pin 철회를 새 host 경로에서 확인한다. 다른 scope의 후보·순위·결과와 원문 독립 문서가 섞이지 않고 갱신 불가 시 명시 오류를 낸다. |
| LI-RECOVER | 게시/ACK 및 전환 각 중단점의 재시작, 첫 새 쓰기 전 rollback, 첫 새 쓰기 뒤 자동 rollback 차단과 전진 복구를 확인한다. 실제 전원 손실은 별도의 시험 없이는 주장하지 않는다. |
| LI-COEXIST | 별도 global QMD를 그대로 보존하고, 플러그인 소유가 증명된 QMD runtime만 복원 가능한 격리 후보로 분류한다. 불확실한 소유권은 보존하고 영구 삭제는 별도 결정한다. |
| LI-VECTOR | 선택된 모델 파일·Node/native dependency closure·전처리/청킹 recipe의 출처와 hash, 위키 문서·질의 차원과 profile, 위키 전량 재색인 seal, scope/세대 필터 선행 및 과예산 무결과를 증명한다. 원본 원문은 문서 임베딩 대상이 아니다. 모델 실행에는 별도 승인된 자원 gate를 사용한다. |
| LI-RECALL | 기존 QMD `rerank:false` 어휘 변형+벡터 결합과 정확한 식별자/EP를 기준으로, Laya의 **위키 카드만** 검색·선별·주입하고 검증된 원문 위치를 제공하는지 lex probe, minScore/topN, 결과 포맷을 새 Claude/Codex 세션에서 비교한다. 위키에 없는 원문 전용 표식은 Laya 결과·자동 주입에 없어야 한다. 위키 카드에 정상적으로 포함된 발췌와 링크는 유지한다. 벡터 단독 합성 검색은 이 행을 통과시키지 않는다. |

모델/벡터 재생성이 필요한 경우 provenance 또는 명시 승인·자원/비용 gate를 함께 기록한다. 필수 행의 `NOT RUN`/`BLOCKED`/`FAIL`은 전환 릴리스를 막는다.

현재 코드의 직접 blocker와 누락된 설치 후보 QA (L0 상태는 제품 합격 판정이
아니다):

| 행 | 현재 경계와 필요한 증거 |
|---|---|
| LI-INST/LI-VECTOR | 자기참조 없는 attestation, 실제 설치 cache의 0755 파일 검증, 소유 journal·private socket, 모델/native closure 검토 자료 생성은 합성 구현됐다. 정확한 최종 commit을 실제 marketplace가 로드한 위치에서 검토·승인하고, 선택된 모델의 tokenizer/청킹·자원 동작을 확인해야 한다. 기존 2회 기능 smoke는 소진됐으며 이 행의 L1 증거가 아니다. |
| LI-MIG/LI-RECOVER | raw+wiki 두 컬렉션의 inventory, 백업·복원 비교, 후보 봉인, route/journal 중단점 복구를 합성 구현했다. 실제 QMD 소유 데이터의 read-only 범위·일관성 검토와 설치 후보에서 변경·중단점 재시작·전진 복구가 필요하다. 원본과 백업은 보존한다. |
| LI-RECALL | 선택된 hook의 warm provider, QMD 형식 FTS, 위키 계층·EP·DF 정책은 소스 및 합성 fixture에 연결됐다. 희귀 CJK Script 경계와 반복 DF 탐색의 시간 제한·부분 결과 폐기는 합성으로 확인했다. 실제 최대 규모 corpus의 훅 지연과 같은 corpus의 fresh Claude/Codex 검색 결과는 측정하지 않았다. |
| RQA-RES-02 | durable learning queue의 중복 방지·재시작·부하 대기/재시도는 합성 검사했다. 실제 설치 후보에서 OS 압력 증가 시 중지·회복 후 재개는 별도 안전 범위의 실제 head 학습 검증이 필요하다. |


독립 Opus QA 후속 확인 대상 (2026-10-18, 제품 출시 전 해결 또는 실측 필요):

| 코드 경계 | 필요한 수정·검사 |
|---|---|
| `core/laya_product_bundle.py`의 production approval | 자기참조는 installer-owned attestation으로 제거했다. 이제 실제 host가 로드한 exact commit의 파일·모델·native closure를 검토해 journal에 봉인하고, 설치 뒤 source와 바이트가 같은지 확인해야 한다. |
| `core/laya_qmd_migration.py`의 vector candidate | 제품 cutover는 boolean 승인 대신 journal-bound provider/profile을 검사한다. 단일 raw 범위만 지원하므로 원문·위키 별도 collection을 하나의 세대에 봉인하고 동일 tokenizer/청킹 결과를 확인해야 한다. |
| `core/laya_host_response.py`의 selected route | 선택된 recall은 warm provider를 호출하고 update/index는 durable wake로 보낸다. 최종 설치 후보의 승인된 provider, 실제 fresh Claude/Codex 세션, QMD 대비 검색 정책을 검증해야 한다. |

Laya 검색·임베딩의 운영 부하 큐 **확장**은 v2 오픈 이후 선택 검토 항목이다.
학습 작업의 운영 부하 큐와 admission·중지/재개 검증은 아래 `RQA-RES-02`의
필수 조건이다. 이 구분은 검색·임베딩 기능 자체를 제품 범위에서 제외하지
않는다.

**2026-10-20 후속 L0 기록 (설치 후보 QA 아님):** 비활성 초기 빌드와
QMD 결속 전환/복구, 기존 승인 소유자의 Laya 경로, durable 학습 child 및
overflow wake를 소스와 합성 fixture에 연결했다. 전체 `npm test` 출력은
1,648건 중 1,639 PASS·9 SKIP·0 FAIL이었지만 실행 도구 시간 한계로
guard supervisor가 정산 전에 종료됐다. 소유 프로세스가 모두 종료됐음을
검사하고 해당 checkpoint를 `stopped`/`settled`/`completed:false`로 기록했다.
동일한 273개 test 파일을 직렬 guard 6회로 재실행해 1,645건 중
1,636 PASS·9 SKIP·0 FAIL을 얻었고, Node가 자동 발견하는 helper 파일
3개도 별도 guard에서 모두 PASS였다. 정산된 범위 합계는 1,648건 중
1,639 PASS·9 SKIP·0 FAIL이며 **하나의 정산된 full-suite 실행은 아니다**.
세부 run ID와 Opus 사용량은
[후속 통합 기록](plans/2026-10-20-laya-index-followup-integration.md)에 있다.
이 기록은 `LI-*` 또는 `RQA-*` 설치 후보 행을 통과시키지 않는다.
후속 독립 Opus 소스 리뷰는 v3 선택 검색이 **반환된** 외부 수정 후보를
조용히 버려 짧거나 빈 결과를 정상으로 제공할 수 있음을 확인했다. 이는
반환 후보의 현재 파일 검증 정책과 어긋난다. 미반환 새 파일은 색인 지연으로
별도 취급하며 매 검색의 전체 코퍼스 증명은 확정 정책이 아니다. QMD digest 변경 뒤
같은 Laya 세대를 재결속할 위험, 제품 복구 진입점 부재, 전체 벡터 seal을
질의 기한 전에 재검사하던 지연 문제도 지적했다. 반환 후보의 명시적 실패,
drift 거부·제품 복구 진입점, 벡터 검사 기한은 합성 수정과 직렬 가드로
확인했다. 큰 세대의 벡터 가용성과 drift 후 재빌드, 설치 후보 QA는 남았다.
이 기록은 L1 행을 변경하지 않는다.

## 1. Source verification and installed-candidate QA are separate

| Level | What runs | Can it satisfy this gate? |
|---|---|---|
| L0 source verification | `npm test`, focused tests, synthetic fixtures, direct `core/*.py` calls in the source checkout | **No.** L0 is a prerequisite only (see [release.md](release.md)). |
| L1 installed-candidate QA (this gate) | Host marketplace install of the exact candidate, then fresh host sessions in which the real hooks fire, with natural-language requests and exposed skills | Yes. This is the only level that counts. |

Hook-path evidence cannot be replaced by a direct script call. A cached plugin
directory can carry the same version string while containing different code;
[the hook-path E2E plan](plans/2026-07-30-hook-path-e2e-test-plan.md) records
such a case. Identity is therefore proved by content hashes (section 5), not by
version strings.

Operator rules:

- Product interaction uses natural-language requests or the exposed skills
  (`setup`, `query`, `sync`, `update`, `enable-compile`, `wiki-compile`). The
  operator must not run `core/*`, `core/update.sh`, `skills/*/scripts/*` or
  backend scripts directly to make a scenario progress. Doing so invalidates
  that scenario's run. The scenario must restart from its preconditions.
- Outside the product, the operator may do only three kinds of work: actions
  that simulate the user (the fixture helper's Git actions, host marketplace
  install/update, host hook trust limited to the reviewed exact commands in
  the preflight, section 3), reading evidence, and computing hashes.
- Probe prompts forbid tool use. If the transcript shows a tool call in a probe
  turn, the probe is invalid. Repeat it; do not count it.

## 2. Status values and the release rule

| Status | Meaning |
|---|---|
| `PASS` | Every expected outcome was observed. All required evidence is attached. The installed artifact identity equals the candidate identity. |
| `FAIL` | An expected outcome was contradicted, a product timeout was exceeded, a hook reported a failure or timeout, or the product claimed success without the required proof. |
| `BLOCKED` | The scenario could not start or finish for a reason outside product behavior: missing approval, hardware, host authentication, host outage, or lost evidence. Write down the blocker. |
| `NOT RUN` | Not attempted. |

- Never record a missing, partial, blocked or unobservable result as `PASS`. A
  product hang or a silent no-op is `FAIL`, not `BLOCKED`.
- A release requires `PASS` for every mandatory scenario in every mandatory
  matrix cell (section 4).
- A `FAIL`, `BLOCKED` or `NOT RUN` result in a mandatory matrix cell blocks a
  formal release. Record the reason and corrective work without changing its
  status. An optional, unverified host/OS combination may be left out of the
  claimed support matrix, but must be named in the release notes.
- Before judging the final candidate, calibrate the evidence review on a
  copy of one synthetic probe record with its hook log line removed and on a
  copy with an intentionally wrong selected path. The reviewer must classify
  both as non-PASS. Save only those disposable calibration records and the
  verdict in `$EVID`; never alter real host logs. If either is accepted as
  PASS, the QA evidence procedure is `BLOCKED` until repaired and repeated.
- Some expected outcomes below are policy goals that the
  [architecture boundaries](plans/2026-10-17-architecture-boundaries.md) and the
  [worktree policy](git-worktree-search-sync-policy.md) still list as
  unimplemented. Examples: query-time freshness, single-generation reads, and
  wiki revision idempotency. The gate keeps those outcomes as the expected
  result. Until they are implemented, they will `FAIL` and block a formal
  release. The gate must not be weakened to describe the current gap as correct.

## 3. QA environment and isolation

- **QA host account.** Use a dedicated OS user account, or a disposable VM or
  snapshot, on supported hardware. It must have no personal projects,
  manuscripts or operating databases. An isolated `HOME` is not enough: the
  runtime wake queue and the coordination and development locks are rooted at
  the OS UID home (see
  [runtime resource coordination](runtime-resource-coordination.md) and
  [development resource safety](development-resource-safety.md)). Never run
  the gate in a maintainer's daily account.
- **Clean state.** A clean state has no qmd-auto-context plugin in either host,
  no plugin runtime roots or shared store, no `~/.config/qmd` plugin state, no
  previous fixtures, and no global Git `core.hooksPath`. Record how the state
  was reset: new account, snapshot restore, or documented removal.
- **Approvals recorded before running:**
  - QA host use and model-usage cost cap for host sessions.
  - Package execution and lifecycle scripts, if the managed QMD install path is used.
  - Wiki extractor/verifier cost cap (RQA-WIKI-*).
  - Learning approval with safe test resources (RQA-RES-02 only).
- **Data limits.** Real learning, manuscripts and operating DB writes are out
  of scope unless separately approved. Without that approval, the dependent
  scenarios are `BLOCKED`.
- **Evidence directory.** Use
  `EVID=/tmp/qmd-release-qa-evidence-<runId>`, mode 0700, outside every
  fixture, so fixture cleanup cannot delete evidence. Launch each fresh host
  session with its own `QMD_RECALL_LOG="$EVID/recall-<host>-<run-session-id>.log"`.
  This setting writes diagnostics
  to a file only; it never writes to model context. Do not set test-only
  variables such as `QMD_SKIP_BACKGROUND_WORKER`, `QMD_FORCE_DEAD_PIDS`,
  `QMD_QUERY_FIXTURE` or `QMD_FAKE_PLATFORMS`. If any is present, the run is
  invalid. `LAYA_QA_SYNC_BARRIER` is allowed only in the RQA-RACE-01 Part B
  session, with that part's arm file. In any other scenario it invalidates
  the run.
- **Fresh session.** A host process started after the candidate install and
  host reload completed, with its working directory equal to the scenario's
  checkout. Sessions that predate the install are "old sessions" and appear
  only in RQA-INST-02.

### Preflight before functional QA

Run this preflight per host and matrix cell before the first functional request.
For a fresh install, RQA-INST-01 steps 1–2 first install and identify the
candidate; finish the preflight before its edit in step 4. Repeat it after an
update or host reload. It checks whether the environment can produce valid
evidence. It is not a scenario and never changes a scenario status to
`PASS`. Record each item as `ready`, `not ready` or `NOT RUN`, with evidence,
in the run record's preflight table. A check that was not performed is
`NOT RUN`, never `ready` or `PASS`. The preflight result is `READY` only when
every item is `ready`.

| ID | Check | `ready` requires |
|---|---|---|
| PF-ID | Exact candidate | RQA-ID-00 evidence for this host: the commit (40 hex), version string, and an empty or allowlisted `identity-diff-<host>.txt`. A matching version string alone is not enough. |
| PF-FOLDER | Project folder access | The host can read and write the exact fixture checkout. If the host presents a folder/project trust prompt, a human approved only that exact checkout. Record an official no-prompt path as such; never infer trust for `$HOME`, `/tmp` or a parent. Resolve OS privacy prompts before the run. |
| PF-TRUST | Plugin hook trust | Compare the installed `hooks/hooks.json` or `hooks/hooks-codex.json` under `INSTALL_PATH` against the candidate and record the full command list. For a host that requires per-hook trust, a human approves only those exact commands in its official UI. For a host without per-hook review, record official plugin install/enable evidence and the loaded command path. No wildcard, global "trust all", config edit or bypass flag. |
| PF-FRESH | Fresh host session | A session started after install, trust and reload, with cwd equal to the fixture checkout. A unique run/session marker and SessionStart output or host hook trace identify this session. Give it a new `QMD_RECALL_LOG` file for later prompt-turn evidence; the file alone does not prove SessionStart ran. |
| PF-HOST | Sandbox and event support | Record the host version, surface (CLI or app), sandbox mode and sandbox environment variables. Check the host's documented/advertised support for every event the planned scenarios require (for example, Codex has no Bash PostToolUse). Observe actual delivery in the corresponding synthetic scenario; do not treat capability metadata alone as a hook PASS. |
| PF-RUNTIME | OS and runtime dependencies | OS build and architecture match the matrix cell. The hook `python3`, Node, Git (2.36+) and hashing tools resolve from the host's hook environment, and their versions are recorded. No forbidden test variable is set (section 3). |
| PF-NET | DNS and network | Record host CLI/app and hook-environment connectivity separately: online, intentionally offline, sandbox-denied, DNS failure or remote-service outage. Probe only endpoints a scenario actually needs. Intentional offline mode can be ready for local-only scenarios; a required endpoint failure is not ready. |

Classify every non-`ready` item, and every unexpected functional result
observed before the preflight was `READY`:

| Class | Typical evidence | Status effect | Retest rule |
|---|---|---|---|
| Environment unready | Wrong candidate selected, folder access denied, missing or wrong runtime, a required sandbox/DNS route denied, a session that predates install | Dependent scenarios `BLOCKED` (or `NOT RUN` if not attempted) | Fix the environment, then repeat the whole preflight in a new fresh session. Earlier functional results in that session are discarded. Investigate an installed-byte mismatch before assigning cause: a product installer defect is `FAIL` once reproduced with an otherwise ready environment. |
| Approval pending | A trust prompt or folder approval not yet given; hooks listed but untrusted; a recorded approval (section 3) missing | `BLOCKED` with the pending item named | A human approves the exact scope in the official UI. Start a new fresh session, repeat PF-TRUST and PF-FRESH, then restart the scenario from its preconditions. |
| Host unsupported | The host version or surface cannot fire a required event, or cannot run in the cell | Optional cell: `NOT RUN` plus the release-note wording (section 4). Mandatory cell: `BLOCKED`, which blocks release. | Repeat on a host version or surface that supports the event, starting from the preflight. |
| Product defect | With a `READY` preflight and a supported event, a trusted hook does not fire or the product contradicts the expected outcome | `FAIL` (section 2) | Fix the product, then rerun the affected set under section 11 on a new candidate hash. |

- The operator never trusts hooks or folders automatically, by script, by
  editing host trust or config files, or beyond the reviewed exact commands.
- A non-`READY` preflight never skips, waives or reorders a required gate.
  Required scenarios stay `BLOCKED` or `NOT RUN` until a `READY` preflight
  allows them to run.
- An untrusted hook is an approval blocker. A trusted hook that fails to fire
  on a supported event after a `READY` preflight is a product failure. A
  missing deny or injection is never `PASS`.
- This preflight does not change any status that is already recorded in this
  document.

**Reproducible Codex example (synthetic fixture, generic paths).** This
example shows how to tell "approval pending" apart from a product defect. It
does not change any recorded scenario status.

1. Install the exact candidate in the QA account and record PF-ID. Create a
   fixture with
   `python3 -B test/fixtures/release-qa-fixture.py create /tmp/qmd-rqa-<runId>-codex-pf`.
   Leave `project/` unconfigured, so the pre-edit gate should hold edits
   pending.
2. In an earlier candidate revision without hook trust, a fresh Codex session
   asked "README.md 끝에 한 줄 추가해줘" and `apply_patch` changed the file.
   Classify that observation as **approval pending** (PF-TRUST `not ready`),
   not `PASS` or a proved product defect. Run `cleanup` on the fixture and
   create a new one; do not repair the old one by hand.
3. In the Codex official UI, trust only the folder `<fixture>/project`. Then
   review and trust exactly the nine commands from the installed
   `hooks/hooks-codex.json`. Each command has the form
   `"<INSTALL_PATH>/hooks/run-hook" <name> codex`, where `<name>` is one of
   `update-queued`, `topical-reconcile`, `recall`, `gate`, `posttool`,
   `index`, `compile`, `topical-event` and `topical-stop`. Record this list as
   the trust scope.
4. On the revised candidate, start a fresh Codex session in the new fixture
   and repeat the request. The PreToolUse `gate` hook denied `apply_patch`;
   `git status --porcelain -- README.md` stayed empty. Preserve the hook
   denial trace and compare the file SHA-256 before and after in future runs.
   The two observations involve different candidate revisions, so they do
   not isolate trust as the only cause. PF-TRUST is now `ready`; functional
   scenarios (RQA-INST-01, RQA-HOOK-02) still run separately and record
   their own status.

## 4. Supported OS and host matrix

| Cell | Platform | Product support | Gate requirement |
|---|---|---|---|
| T1 | macOS arm64, current maintainer macOS release (record exact build) | Managed QMD + v2 routing | **Mandatory:** all scenarios for Claude Code and Codex |
| T1-node | Same as T1, second Node LTS (22 vs 24) | Managed QMD accepts both | **Mandatory:** RQA-INST-01 + core smoke |
| T1-py | Same as T1, hooks run by the system `python3` (macOS ships 3.9) | Hook floor is Python 3.9 | **Mandatory:** core smoke with the host-resolved `python3`; record its version |
| N26 | T1 with Node 26 Current | Native compatibility probe still pending (README) | Optional; release notes must say "unverified" unless RQA-INST-01 + core smoke PASS |
| U-intel | macOS x86_64 | `setup_required`; existing global QMD preserved | RQA-PLAT-01 if hardware exists; otherwise `NOT RUN` + release note |
| U-linux | Linux x86_64/arm64 | Managed v2 unsupported; existing global hook path only | RQA-PLAT-01 recommended; otherwise `NOT RUN` + release note |
| U-wsl / U-win | WSL guest / native Windows | Unverified / unsupported | Not claimed; no gate cell |
| H-hermes | Hermes Agent on T1 | README lists it as supported | Core smoke equivalent recommended; if not run, release notes must say Hermes was not verified in this release |

Rules for unverified OS/host combinations:

- A combination without a `PASS` record must not be described as supported or
  verified for this release. Release notes keep the explicit wording
  "not verified in this release".
- Results from one cell are never copied to another cell. Each cell's record
  carries its own host, OS build and artifact identity.
- An unsupported platform can pass only the negative-path scenario
  RQA-PLAT-01. That scenario checks preservation and `setup_required`; it does
  not check functionality.

## 5. Candidate and installed artifact identity

Run hashing from the source checkout through the shared development lock:
`npm run dev:guard -- --kind hash -- bash "$EVID/identity.sh"`. Record each
output file's SHA-256 in `$EVID/evidence-index.sha256`.

Candidate identity (Git 2.36+ for `--format`):

```bash
COMMIT=$(git -C "$CANDIDATE" rev-parse "$REF^{commit}")
git -C "$CANDIDATE" rev-parse "$COMMIT^{tree}"                    > "$EVID/candidate-tree.txt"
git -C "$CANDIDATE" status --porcelain                           > "$EVID/candidate-dirty.txt"   # must be empty
git -C "$CANDIDATE" ls-tree -r --full-tree \
  --format='%(objectmode) %(objectname) %(path)' "$COMMIT" | LC_ALL=C sort > "$EVID/candidate-blobs.txt"
shasum -a 256 "$EVID/candidate-blobs.txt"                         # artifactManifestSha256
git -C "$CANDIDATE" archive --format=tar "$COMMIT" | shasum -a 256  # informational; record git --version
```

Installed identity, per host (`INSTALL_PATH` is the path the host reports for
the loaded plugin):

```bash
cd "$INSTALL_PATH" && find . -type f ! -path './.git/*' -print | LC_ALL=C sort |
while IFS= read -r f; do
  m=100644; [ -x "$f" ] && m=100755
  printf '%s %s %s\n' "$m" "$(git hash-object --no-filters -- "$f")" "${f#./}"
done | LC_ALL=C sort > "$EVID/installed-blobs-$HOST.txt"
diff "$EVID/candidate-blobs.txt" "$EVID/installed-blobs-$HOST.txt" > "$EVID/identity-diff-$HOST.txt"
```

- Every tracked path must match: same blob id, same executable bit.
- Any extra file must be named in the run record's host-metadata allowlist
  with a justification. Extra files must not be executable, hook-related or
  under `core/`, `hooks/`, `skills/`, `backend/` or `hermes_adapter/`. A
  symlink difference is shown in the diff and must be explained.
- Also record the host-reported plugin version and commit. For Claude Code,
  this is the installed-plugin registry entry; for Codex, the plugin list.
- Version consistency: one version string across `package.json`, `plugin.json`,
  `plugin.yaml`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` and the
  `test/probe-manifest.test.mjs` assertions.
- Runtime identity, reported by the `setup` skill status request: QMD version
  and entry, Node, embedding model file SHA-256, Laya state (`deferred` or a
  reviewed identity), and selected store generation.

## 6. Evidence and search-result proof

Probe prompt (one turn; replace `<question>`). This observes what the
installed host received. For a raw-only fact the expected no-tools answer
is `없음`, even when the correct source locator was selected:

> 도구를 쓰지 말고 이번 프롬프트에 자동 주입된 프로젝트 컨텍스트만 근거로 답해줘.
> `<question>` 주입된 컨텍스트에 없으면 "없음"이라고만 답해.

| Question key | `<question>` | Tokens it can reveal |
|---|---|---|
| Q-alpha | release QA alpha 문서의 codename은? | `ALPHA_MAIN`, `ALPHA_FEATURE`, `ALPHA_WORKTREE`, `DIRTY_TRACKED` |
| Q-beta | release QA beta revision marker는? | `BETA_V1`, `BETA_V2` |
| Q-gamma | release QA gamma marker to be deleted는? | `GAMMA_DELETE_ME` |
| Q-delta | release QA added delta marker는? | `DELTA_ADDED` |
| Q-feature | release QA feature-only marker는? | `FEATURE_ONLY` |
| Q-worktree | release QA worktree-only marker는? | `WORKTREE_ONLY` |
| Q-untracked | release QA untracked marker는? | `UNTRACKED_NEW` |
| Q-wiki | release QA wiki source fact는? | `WIKI_FACT_R1`, `WIKI_FACT_R2` |
| Q-agent | release QA agent note marker는? | `AGENT_NOTE` |

Token strings come from the fixture manifest (`show`). They contain a random
per-fixture id, so an earlier fixture cannot satisfy them. Record whether each
token occurs in a raw source or in a verified wiki card before applying a
verdict.

All scenario rows below that say `Present(T)` or `Absent(T)` for a raw-only
marker use the legacy QMD meaning while QMD is selected. After a Laya v2
cutover, evaluate the selected, current wiki card and verified locator instead;
raw-only markers must not create independent Laya hits. Do not relax the
scenario's installation, sync, freshness, scope, backup or recovery checks.

- **Present(T) for a raw-source token** applies only to the legacy QMD
  bootstrap/update path. Its selected route and fresh `qmd_recall_selection`
  line identify the current, index-verified source path in the correct
  `project_root`; the injected context contains a locator/title but no raw
  body or T. On a selected Laya v2 route, raw files are not independent
  indexed/searchable documents: a raw-only T must be absent from results and
  automatic context. Source freshness and the verified locator are checked
  through the selected wiki card, not a raw search hit.
- **Present(T) for a verified wiki-card token** additionally requires the
  selected card's bounded summary in the injected context to contain T and
  its verified source locator to be correct. A no-tools answer containing T
  is useful host evidence, but the selected card and injected bytes are the
  plugin proof; the answer alone is insufficient.
- **Absent(T)** means no selected current wiki card carrying T, no T in
  the injected context, and no unsupported answer containing T. An old or
  excluded source must not be served as current. A raw-only token must not
  create a selected Laya result; the original file may still exist in QMD or
  as a verified source of a wiki card.
- A selected path by itself does not prove that its source revision is current;
  retain the index/generation and scope evidence required by the scenario.
- In a **separate optional prompt that permits tools**, record whether the
  host agent chooses to Read the selected source and what it answers. That is
  host-agent behavior, not a plugin release condition.
- A missing log line means the hook did not run. Treat it as `FAIL` unless a
  sandbox or headless host is documented.
- For each fresh host session, use a unique QA run/session marker and a fresh
  `QMD_RECALL_LOG` file under `$EVID`. Capture the host's loaded plugin path,
  hook command path and session/turn identifier alongside the log file's
  before/after size and timestamps. Correlate the selected line to that host
  turn. A direct CLI query or a line from an earlier session never proves the
  installed host hook ran. If the host does not expose a hook command trace,
  record that limitation and use its official plugin registry, isolated log
  path and exact turn timing together; an ambiguous match is `BLOCKED`.
- The optional query skill check ("`<question>` 질문에 어떤 문서가 recall되는지
  확인해줘") is secondary evidence. It does not replace the hook path.
- Per probe, record: wall-clock time, host session id, cwd, branch,
  `git status --porcelain` summary, the recall log line, a transcript excerpt
  and the verdict.
- Each convergence probe must show at least one positive current token and
  the relevant negative old token together. An empty result can be a safe
  intermediate state during a switch, but never satisfies a final searchable
  state or a convergence deadline.
- Store transcripts privately. Release notes get summaries and hashes only.

## 7. Timeouts

| Name | Value | Applies to |
|---|---|---|
| Host hook timeouts | `hooks/hooks.json` / `hooks-codex.json`: SessionStart 5 s and 3 s, recall 22 s, gate 3 s, PostToolUse 22/5/5/5 s, Bash 3 s, Stop 3 s | Any host-reported hook timeout or failure is `FAIL` |
| T_TURN | 90 s | One probe turn, prompt submit to final answer |
| T_FRESH | 180 s | From the trigger (SessionStart, agent edit, agent Bash Git command) to the expected Present/Absent state. Probe at +30 s, +90 s and +180 s; stop at the first full success |
| T_LATE | T_FRESH + 120 s | Recheck that a late worker did not republish stale results |
| T_GIT | 30 s | Each fixture helper action (enforced by the helper) |
| T_INSTALL | 30 min | Install or update: plan, prepare and apply on the fixture |
| T_ROLLBACK | 10 min | Rollback, or a recovery status request |
| T_WIKI | 15 min | Compile and verify of one source revision |
| T_SCENARIO | 45 min (60 min for RQA-INST-02/04, RQA-RES-02) | Whole scenario |
| T_CELL | 8 h | One matrix cell |

- Exceeding a timeout because of product behavior is `FAIL`.
- Exceeding a timeout because of an external outage (host service,
  authentication expiry) is `BLOCKED`, with evidence.
- These values are the initial release policy. The owner may change them only
  through a reviewed edit to this document.

## 8. Fixture

Use [`test/fixtures/release-qa-fixture.py`](../test/fixtures/release-qa-fixture.py).
It is an operator tool only; it never runs QA itself.

- **Location.** It writes only below a caller-provided new or empty directory
  under `/tmp` or the current workspace. It refuses a location inside host or
  runtime state, and one whose ancestors hold auto-context settings (upward
  config discovery would contaminate opt-in). Prefer `/tmp`.
- **Git isolation.** Git runs with fixture-local config, an empty fixture-local
  `core.hooksPath` and the file protocol only. That local config persists, so
  an agent's own Git commands inside the fixture also skip the QA account's
  global hooks.
- **No side effects.** It invokes no QMD, host, model, API, hook, installer or
  network. It never overwrites an existing path. `cleanup` deletes the fixture
  only after it verifies the owner marker.

```bash
python3 -B test/fixtures/release-qa-fixture.py create  /tmp/qmd-rqa-<runId>-<host>-<scenario>
python3 -B test/fixtures/release-qa-fixture.py show    <fixture>              # tokens and event log
python3 -B test/fixtures/release-qa-fixture.py stage   <fixture> <action>     # simulated user action
python3 -B test/fixtures/release-qa-fixture.py cleanup <fixture>
```

**Layout.**

- `project/`: a clone on `main`, with `docs/qa/{alpha,beta,gamma,wiki-source}.md`
  and `README.md`.
- Local branches:
  - `qa/feature`: alpha changed to `ALPHA_FEATURE`, plus `feature-only.md`.
  - `qa/worktree`: the same `alpha.md` contains `ALPHA_WORKTREE`, plus
    `worktree-only.md`.
- `upstream.git` and `seed/` simulate the remote.
- `worktree-add` creates `wt-worktree/` on `qa/worktree`.

**Stage actions.**

| Action | Effect |
|---|---|
| `upstream-change` | Adds `delta.md`, changes beta to `BETA_V2`, deletes `gamma.md` upstream |
| `pull` | `git pull --ff-only` in `project/` |
| `switch-feature` / `switch-main` | Branch switch in `project/` |
| `worktree-add` | Creates `wt-worktree/` |
| `dirty` | Uncommitted alpha edit with `DIRTY_TRACKED`, plus untracked `untracked-note.md` with `UNTRACKED_NEW` |
| `deny-read` / `restore-read` | Toggles read permission on `beta.md` |
| `wiki-source-edit` | External edit of the wiki source to `WIKI_FACT_R2` |

Use one fixture per host per scenario group, so state from the shared runtime
cannot satisfy another host's expectation. `AGENT_NOTE` is the token the
operator asks the agent to write.

**Visibility matrix.** These are the expected states; scenarios refer to them
by name.

| State | Present | Absent |
|---|---|---|
| S-main | `ALPHA_MAIN`, `BETA_V1`, `GAMMA_DELETE_ME` | `FEATURE_ONLY`, `WORKTREE_ONLY`, `DELTA_ADDED` |
| S-pulled | `ALPHA_MAIN`, `BETA_V2`, `DELTA_ADDED` | `BETA_V1`, `GAMMA_DELETE_ME` |
| S-feature | `ALPHA_FEATURE`, `FEATURE_ONLY` | `ALPHA_MAIN`, `WORKTREE_ONLY` |
| S-worktree (cwd `wt-worktree/`) | `ALPHA_WORKTREE`, `WORKTREE_ONLY` | `ALPHA_MAIN`, `ALPHA_FEATURE`, `FEATURE_ONLY` |
| S-main-after-worktree (cwd `project/`) | `ALPHA_MAIN` | `ALPHA_WORKTREE`, `WORKTREE_ONLY` |
| S-dirty | `DIRTY_TRACKED`, `UNTRACKED_NEW`, `ALPHA_MAIN` | none |

## 9. Scenarios

Each scenario lists: purpose, owning domain (section 10), hosts,
preconditions, steps, expected outcome, PASS evidence, timeout and
cleanup/recovery. The general rules (sections 2–7) apply to every scenario.
**Core smoke** is RQA-ID-00 + RQA-HOOK-01 + RQA-HOOK-02 on the final artifact.

### RQA-ID-00 Candidate and installed identity

- **Purpose:** bind every result to one exact artifact.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - The candidate commit is frozen and the candidate checkout is clean.
  - The host installs from a candidate source pinned to that commit: a pushed
    pre-release ref, or a local marketplace clone checked out at the commit,
    where the host supports one.
- **Steps:**
  1. Compute the candidate identity (section 5).
  2. Install or update through the host's marketplace flow.
  3. Reload the host.
  4. Compute the installed identity and the host-reported version and commit.
  5. Run the version consistency check.
- **Expected:** the candidate is clean. The installed blobs equal the
  candidate blobs, apart from allowlisted host metadata. One version string
  appears everywhere.
- **Evidence:** `candidate-*.txt`, `installed-blobs-<host>.txt`, the empty
  (or allowlisted) `identity-diff-<host>.txt`, `artifactManifestSha256`.
- **Timeout:** 15 min.
- **Cleanup:** none; the scenario is read-only.

### RQA-INST-01 Fresh install

- **Purpose:** check first install, the unconfigured-project gate, opt-in,
  managed setup and the first injection on a clean machine.
- **Owner:** install, with search for the final probe. **Hosts:** Claude Code
  and Codex, each starting from clean state.
- **Preconditions:**
  - Clean QA state.
  - A supported Node LTS.
  - A reviewed local embedding GGUF; record its SHA-256. Setup does not
    download weights.
  - Package-execution approval, if the managed QMD install path is used.
  - Fixture created.
- **Steps:**
  1. Install the candidate through the host marketplace. On Codex, review and
     trust the exact hook commands in the official UI (preflight PF-TRUST).
  2. Run RQA-ID-00 for this host.
  3. Start a fresh session in `project/`. Record SessionStart output: at most
     one short setup notice, and no hook failure.
  4. Ask "README.md 끝에 한 줄 추가해줘". Expected: the pre-edit gate stops
     the edit and offers choices; `README.md` is unchanged (record
     `git status`). Decline the edit.
  5. Ask "이 프로젝트 auto-context 설치 상태 보여줘". Expected: a read-only
     report; `git status` and the `.auto-context/` absence are unchanged.
  6. Ask "이 프로젝트에서 auto-context 켜줘. 문서 범위는 docs/qa만." Expected:
     `.auto-context/settings.json` names only the `docs/qa` collection.
  7. Ask the `setup` skill for a plan ("설치 계획 보여줘"), review its roots,
     input hashes and indexing impact, then approve ("검토한 설치를
     진행해줘"). Grant only the approvals that are already recorded.
  8. Record the result: `qmdReady`, `layaReady`, `overallReady`,
     `componentReadiness`, `completionScope`.
  9. Reload the host and start a fresh session. Probe Q-alpha.
- **Expected:**
  - `qmdReady: true`, with the managed binary version, index path and
    embedding model file hash recorded separately from search proof.
    `layaReady` is `deferred` unless the release advertises learning; in that
    case `overallReady` and a separately reviewed Laya identity are required.
    A QMD bootstrap PASS never implies Laya setup, training or model-use PASS.
  - Nothing outside the fixture, the QA account's plugin runtime roots and the
    host plugin directories is written.
  - On the QMD bootstrap route, Present(`ALPHA_MAIN`) within T_FRESH by the
    legacy raw-source definition above. On a selected Laya route, verify the
    current wiki card and its source locator instead; a token found only in
    the raw file must not be returned. No hook timeout or failure.
- **FAIL if:**
  - The gate does not stop the edit.
  - Install reports `completed` without scoped-search proof.
  - The probe stays absent at T_FRESH.
  - Any hook fails.
- **Evidence:** setup outputs, settings file with its hash, `git status`
  snapshots, recall log lines, probe transcript excerpt.
- **Timeout:** T_INSTALL for steps 7–8; T_SCENARIO overall.
- **Cleanup/recovery:**
  - A `deferred` (75) result allows one identical retry.
  - `recovery_required`: stop, preserve every journal and pointer, record
    `FAIL`, and open an issue.
  - Keep the install for later scenarios, or restore the clean state before
    the next host.

### RQA-INST-02 Update from the previous release

- **Purpose:** prove that a real update from release N-1 to the candidate
  preserves data and old sessions, and ends routed to the candidate.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - Clean state.
  - The previous public release (N-1) is installed from the public
    marketplace, with its identity recorded per section 5.
  - The fixture was opted in and indexed under N-1. Record the selected file,
    installed identity and consistent backup before updating; N-1 need not
    auto-inject or answer a token found only in the raw source.
  - The RQA-INST-03 preparation steps are done.
- **Steps:**
  1. Keep one N-1 session open (the "old session").
  2. Update the plugin to the candidate through the host's update flow.
     Reload as the host requires.
  3. In the old session, probe Q-alpha. Record the behavior: old code may
     still answer. Expected: no crash, no hook failure, no corruption.
  4. Start a fresh session. Expected: at most one setup or update notice.
  5. Ask "auto-context 업데이트 확인해줘" (`setup` skill `check-update`;
     read-only). If it reports work, approve plan, prepare and apply in
     separate requests.
  6. Run RQA-ID-00 against the candidate.
  7. Start a fresh session and probe Q-alpha.
- **Expected:**
  - The installed identity equals the candidate.
  - The check-update report is read-only.
  - After update, verify a current wiki card and its source locator on the
    selected Laya route. A raw-only `ALPHA_MAIN` token is absent from Laya
    results; its source remains in the preserved QMD inventory and backup.
  - Settings, wiki and index preserved (RQA-INST-03).
  - The old session caused no damage.
- **Evidence:** both identities, notices, check-update and apply outputs,
  probes.
- **Timeout:** 60 min.
- **Cleanup/recovery:** same as RQA-INST-01. The state carries over to
  RQA-INST-03 and RQA-INST-04.

### RQA-INST-03 Settings preservation

- **Purpose:** an update or migration must never drop or rewrite user
  decisions.
- **Owner:** install. Settings are owned by the project and user.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** under N-1, before the update in RQA-INST-02:
  1. Ask "auto-context 설정에서 injectSourcePathsPerCard를 2로, queryTimeout을
     4로 바꿔줘".
  2. Confirm `compile.mode` is explicitly `"off"`.
  3. Create a second fixture B in the same session flow and ask "이
     프로젝트에서는 auto-context 꺼줘" there.
  4. Record the settings bytes, SHA-256 and canonical JSON for A, the
     absence of `.auto-context/` in B, and the wiki file hashes for A.
  5. Record N-1's on-disk format/version markers and representative index,
     wiki and decision-store identities before update. Use actual N-1 output,
     not a hand-written imitation of the old format.
- **Steps:**
  1. After RQA-INST-02 completes, re-read A's settings and wiki hashes.
  2. Start a fresh session in B and probe Q-alpha.
- **Expected:**
  - A's settings are byte-identical, or differ only by a migration that the
    release notes document and that keeps every user value.
  - A's wiki bytes are unchanged.
  - Every prior-format index and decision-store record is either readable by
    the candidate or migrated through a documented path with no lost user
    decision or source identity. Record the before/after format and any
    migration receipt. A fresh install alone cannot satisfy this check.
  - In B the decline still holds: the recall log shows `no_collections`,
    nothing is injected, and no `.auto-context/` is created.
- **Evidence:** before/after hashes and diff, recall log lines for B.
- **Timeout:** 20 min.
- **Cleanup:** clean up fixture B.

### RQA-INST-04 Rollback and recovery

- **Purpose:** check that an applied update can be rolled back without
  losing data, and that an interrupted setup never reports false success.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:** RQA-INST-02 state. Record a hash snapshot of settings,
  wiki and the `setup` skill's status report before the apply.

**Part A: rollback.**

1. Ask "직전 적용을 되돌려줘".
2. Expected: the skill reports pointer and settings restoration, or names
   the concurrent change that blocks it.
3. Start a fresh session and probe Q-alpha. Expected: Present, served by the
   restored selection.
4. Settings and wiki hashes equal the pre-apply snapshot. The original DB,
   wiki and models are not deleted.
5. Ask "준비된 설치를 적용해줘" again. Expected: idempotent re-apply, then
   Present again in a fresh session.
6. Host-level downgrade: if the host can pin N-1, reinstall N-1 and open a
   fresh session. Expected: no hook failure, and either a working state or
   one setup notice. If the host cannot pin a version, mark this step
   `NOT RUN` with the reason.

**Part B: interrupted recovery.**

1. Use a fresh fixture C and opt it in.
2. Start the setup install ("검토한 설치를 진행해줘").
3. As soon as the skill reports that preparation started, terminate the host
   process. Do not signal plugin children individually.
4. Start a fresh session and ask "이 프로젝트 auto-context 설치 상태 보여줘".
   Expected: an in-progress, resumable or `recovery_required` state. Never
   `completed`.
5. Repeat the identical request. Expected: it resumes to completion, or
   stays `recovery_required` with a named reason. Never a silent partial
   success.

- **Evidence:** status outputs before and after, hash comparisons, probes.
- **Timeout:** T_ROLLBACK per request; 60 min overall.
- **Cleanup/recovery:**
  - Never delete a journal or pointer to make progress.
  - A persistent `recovery_required` is `FAIL`. Preserve the state for
    diagnosis, then restore the clean QA state.

### RQA-HOOK-01 Claude Code real hooks and injected search

- **Purpose:** every bundled Claude hook fires on the installed candidate,
  and recall injects real search results.
- **Owner:** search; file sync (edit indexing); runtime.
- **Host:** Claude Code.
- **Preconditions:**
  - RQA-INST-01 is `PASS` for Claude on this candidate.
  - The fixture is opted in and in S-main.
- **Steps:**
  1. Start a fresh session in `project/`. Expected: both SessionStart hooks
     run without failure.
  2. Probe Q-alpha. On QMD, expect the verified raw locator. On selected
     Laya, expect a current wiki card and its verified `docs/qa/alpha.md`
     locator if a card covers that source; never require the raw-only token
     or a standalone raw hit.
  3. Ask "docs/qa/agent-note.md 파일을 만들고 내용은 'The agent note marker is
     <AGENT_NOTE>.' 한 줄로 해줘". Expected: the gate allows the edit;
     posttool, index, compile and topical-event run without failure.
  4. Probe Q-agent until T_FRESH. On QMD, expect the verified raw locator;
     on selected Laya, expect a current wiki card and verified locator after
     compile. Until a card exists, no raw fallback is expected.
  5. Ask "Bash로 docs/qa/agent-note.md를 삭제해줘". Expected: the Bash
     file-change-event hook runs.
  6. Probe Q-agent until T_FRESH. Expected: Absent(`AGENT_NOTE`).
  7. End the session normally. Expected: the Stop hook runs without failure.
- **Evidence:** transcript excerpts, recall log lines, and the host's hook
  failure/timeout output (none). Host debug output is optional.
- **Timeout:** T_SCENARIO.
- **Cleanup:** the fixture stays for later scenarios, or is cleaned up.

### RQA-HOOK-02 Codex real hooks and injected search

- **Purpose:** same as RQA-HOOK-01 for the Codex hook map
  (`hooks-codex.json`).
- **Owner:** search, file sync, runtime.
- **Host:** Codex.
- **Preconditions:** RQA-INST-01 is `PASS` for Codex; the preflight is
  `READY`, with the exact hook commands trusted in the official UI.
- **Steps:** as RQA-HOOK-01, with these differences:
  - The edit goes through `apply_patch`.
  - The deletion in step 5 is also done through `apply_patch`. Codex has no
    Bash PostToolUse matcher.
  - Record one extra observation: delete through the shell tool, then probe.
    Expected per the current hook map: detection only at the next SessionStart
    or on request "훅이 놓친 변경 다시 동기화해줘". This observation is
    informational; a missed detection is not a FAIL. A stale injection after
    the sync request is a FAIL.
- **Expected and evidence:** as RQA-HOOK-01.
- **Timeout:** T_SCENARIO.

### RQA-SYNC-01 Pull add/update/delete with actual search results

- **Purpose:** changes that arrive through a user `git pull` (the plugin never
  pulls) become searchable, and deletions disappear.
- **Owner:** file sync (change set), search (results). Wiki (no enqueue
  while compile is off).
- **Hosts:** Claude Code, Codex (separate fixtures).
- **Preconditions:** RQA-HOOK-0x is `PASS` for this host; the fixture is
  opted in and in S-main; compile is off; no session is open.
- **Steps:**
  1. In a fresh session, probe Q-beta, Q-gamma and Q-delta. Expected: S-main.
     Close the session.
  2. Record a wiki-state listing (`.auto-context/compile`, `.auto-context/wiki`).
  3. Run `stage upstream-change`, then `stage pull`.
  4. Start a fresh session. Probe Q-beta, Q-gamma and Q-delta at the T_FRESH
     schedule.
  5. Re-list the wiki state.
  6. Optional, secondary: run the same questions through the query skill.
- **Expected:**
  - S-pulled within T_FRESH: Present `DELTA_ADDED` and `BETA_V2`; Absent
    `BETA_V1` and `GAMMA_DELETE_ME`.
  - The wiki state is unchanged.
- **Evidence:** helper event log with HEADs, probes, the two wiki listings.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-GIT-01 Same-session branch switch

- **Purpose:** after a branch switch inside an open session, the
  previous branch is not injected as current.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture, opted in and indexed, S-main confirmed in
  an open session.
- **Steps (Claude Code):**
  1. Ask "git switch qa/feature 실행해줘". The Bash tool runs; Claude fires
     the Bash file-change-event hook.
  2. In the same session, probe Q-alpha and Q-feature at the T_FRESH
     schedule.
  3. Ask "git switch main 실행해줘" and probe again.
- **Steps (Codex):**
  1. Ask the agent to run the same switch.
  2. Probe immediately (strict check).
  3. Ask "훅이 놓친 변경 다시 동기화해줘".
  4. Probe at the T_FRESH schedule.
  5. Switch back and repeat.
- **Expected:**
  - At no probe after the switch completed is `ALPHA_MAIN` injected as
    current content. An empty result, or an explicitly marked stale result,
    is acceptable.
  - S-feature holds within T_FRESH (Codex: after the sync request).
  - After switching back, S-main holds within T_FRESH.
- **Evidence:** transcript timestamps of the switch result and of each probe
  hook, recall log lines, helper `show` with the branch.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-GIT-02 New worktree and actual cwd switch isolation

- **Purpose:** a new worktree has its own search view, and neither the main
  checkout nor the worktree leaks into the other.
- **Owner:** file sync, search; install for enrollment.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture opted in and indexed in `project/`
  (S-main); `stage worktree-add` done. The worktree branch predates opt-in,
  so the worktree has no settings.
- **Steps:**
  1. Start a fresh host process whose actual working directory is
     `wt-worktree/` (start it from that directory; do not pass a path
     argument). Probe Q-alpha and Q-worktree. Expected: no main-checkout
     content is injected. Absent `ALPHA_MAIN`. The unconfigured worktree is
     not indexed.
  2. In that session, ask "이 프로젝트에서 auto-context 켜줘. 문서 범위는
     docs/qa만." If the `setup` skill asks for enrollment of the new root,
     review it and approve it.
  3. Start a fresh session in `wt-worktree/` and probe at the T_FRESH
     schedule. Expected: S-worktree. In particular, the same relative path
     `docs/qa/alpha.md` yields `ALPHA_WORKTREE`, never `ALPHA_MAIN`.
  4. Start a fresh session in `project/`. Probe Q-worktree and Q-alpha.
     Expected: S-main-after-worktree, including the opposite result for that
     same `alpha.md` path.
- **Evidence:** cwd and `git rev-parse --show-toplevel` per session, recall
  `project_root` per line, probes, setup outputs.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; this removes the worktree too.

### RQA-GIT-03 Dirty and untracked files

- **Purpose:** uncommitted changes to tracked files and new untracked files
  inside an approved collection count as the current worktree's content.
  Reverting them removes them.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture opted in and indexed (S-main); no open
  session.
- **Steps:**
  1. Run `stage dirty`.
  2. Start a fresh session. Probe Q-alpha and Q-untracked at the T_FRESH
     schedule. Expected: S-dirty.
  3. Ask "docs/qa/alpha.md 변경을 되돌리고 docs/qa/untracked-note.md를
     삭제해줘". Claude does this with Bash. On Codex, follow with "훅이 놓친
     변경 다시 동기화해줘".
  4. Probe again. Expected: Absent(`DIRTY_TRACKED`, `UNTRACKED_NEW`),
     Present(`ALPHA_MAIN`).
- **Evidence:** `git status --porcelain` at each step, probes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RACE-01 Mid-switch query, late queue worker and retry

- **Purpose:** a query during a switch, a worker that finishes late, and an
  I/O retry never publish stale or deleted content as current.
- **Owner:** file sync, search; runtime (wake queue, retry).
- **Hosts:** Claude Code (Parts A and B use the Bash hook); Codex (Part C,
  plus Parts A and B with a sync request).
- **Preconditions:** a fresh fixture, opted in and indexed, S-main.

**Part A: mid-switch query.**

1. Ask "git switch qa/feature 실행해줘".
2. Submit Q-alpha as the very next prompt, within 5 s of the tool result.
3. Expected: `ALPHA_MAIN` is not injected as current. Either S-feature
   content, an empty result, or a marked stale result.

**Part B: late worker flip-flop (selected Laya Index engine).**

Laya Index owns search and indexing in this release. Part B therefore runs on
a fixture whose route was selected by the installer (LI-MIG in the same
fixture), not on the legacy QMD writer.

How the selected engine enforces the invariant, and why the order differs
from the old wording:

- One runtime dispatcher per OS UID runs `laya-sync` wakes
  (`core/runtime_work.py` `drain`, `runtime-worker.lock`). A wake re-put while
  a sync runs bumps its revision and survives the revision-scoped ACK. A held
  old attempt therefore **delays** the newer revision. The newer revision
  cannot publish first; a second selected writer would be deferred
  (`owner_busy`) by the queue owner lock (`core/laya_index_sync.py`
  `_owner_lock`).
- When the old attempt resumes, `sync_full` rescans the source. A vector
  batch for another manifest is refused before any queue/index write
  (`vector_batch_stale`).
- A retained old claim is replanned against the current source.
  `publish` checks the sealed manifest, base head and revocation epoch in one
  transaction (`index_stale`/`stale`).
- Every recall rescans the source against the pinned seal, so a stale head
  fails closed rather than being injected.
- The source test is in `test/fixtures/laya-index-sync-checks.py`
  (`test_late_feature_attempt_after_switch_back_never_publishes`,
  `test_dead_feature_worker_claim_and_generation_cannot_follow_newer_publish`).
  It is L0 only.

QA-only barrier (`core/laya_product_sync_worker.py`):

- **Default.** Without `LAYA_QA_SYNC_BARRIER`, no file is read and nothing
  waits.
- **Variable value.** With the variable set, it must name
  `<fixture>/.qa-laya-sync-barrier.json`. `<fixture>` is the resolved
  `root` from the fixture `MANIFEST.json`, for example `/private/tmp/...` on
  macOS.
- **Arm file contents.** The arm file is mode 0600 and must match the
  fixture owner marker. It names the exact selected project
  (`paths.project`), `scopeId`, `dbIncarnation` (from the installer
  `status`), a fresh 32-hex `nonce` and `timeoutSeconds` (1–600).
- **Validation.** A project outside that fixture is untouched. A malformed or
  unsafe arm, or the right project with a different scope or DB, fails that
  sync with `qa_sync_barrier_invalid` before the model starts.
- **Hold.** The first matching attempt holds after its complete vector batch
  is built and before anything is enqueued. It writes `.qa-laya-sync-barrier-<nonce>.held`
  (source manifest, pid, time).
- **Release.** It waits for a regular file `.qa-laya-sync-barrier-<nonce>.release`
  that the operator creates.
- **Outcome record.** It then writes `.done` (release time, finish time,
  state/generation or refusal reason). A timeout writes `.done` with
  `laya_qa_barrier_timeout` and leaves the wake pending.
- **Later attempts.** Later attempts with the same nonce never wait.
- **Scope of the variable.** The variable is valid only in the Part B host
  session. It invalidates every other scenario (section 3).

1. Preconditions: Claude Code; selected fixture at S-main, indexed; no
   runtime dispatcher process alive for the QA UID. Start the fresh host
   session with the variable naming the as-yet-absent arm path. Wait until
   its SessionStart `laya-sync` wake has finished and S-main remains current.
   Only then write the arm file. A missing arm file is ignored, so this
   SessionStart attempt cannot consume the nonce.
2. Ask "git switch qa/feature 실행해줘". The successful Bash PostToolUse wakes
   `laya-sync`. Wait for `.held`; record its source manifest, pid and both
   Git HEADs (fixture `stage` events or `git rev-parse`).
3. Ask "git switch main 실행해줘". Read-only, confirm the `laya-sync` wake
   revision advanced. Probe Q-alpha and Q-feature while the old attempt is
   still held.
4. Create the release file. Wait for `.done`, then for the retained wake to
   settle. Probe Q-alpha and Q-feature immediately and again at T_LATE.
5. Copy the barrier records into `$EVID` before fixture cleanup. End the
   session and confirm that no process of the QA UID still carries the
   variable before the next scenario.
6. Expected:
   - `.held` `sourceManifest` is the S-feature manifest. An S-main hold is
     `BLOCKED`, even if the later probes appear correct.
   - `.done` `finishedAt` is later than the switch-main event and `releasedAt`.
   - The old attempt reports a refusal (`vector_batch_stale`, `index_stale`
     or a replan) and never `indexed` with the held feature manifest.
     `laya_sync_yield_priority` is inconclusive: repeat with a new nonce.
   - The installer `status` head ends at a generation whose manifest is S-main.
   - S-main is current at every probe after the second switch. `FEATURE_ONLY`
     and `ALPHA_FEATURE` are never injected.
   - A missing `.held` (for example, a dispatcher without the variable)
     means the race did not run: `BLOCKED`, repeat with a new nonce. A fast
     flip-flop without the records cannot be `PASS`.

**Legacy QMD route limitation.** A project that has not been migrated still
uses the preserved QMD writer, which has no QA barrier or inactive DB
publication path
([published view investigation](plans/2026-10-17-qmd-published-view-options.md)).
That historical document does not decide the Laya engine, and this release
does not build a QMD A-copy. Late-worker safety is not claimed for the legacy
QMD route. Release notes must say "late-worker race verified only on the
selected Laya Index engine" until a separate decision changes that.

**Part C: I/O retry.**

1. Close the session and run `stage deny-read`.
2. Start a fresh session and probe Q-beta. Expected: beta is not treated as
   deleted. Its previous content stays searchable, and the I/O condition is
   reported in hook or backend logs without a hook failure.
3. Run `stage restore-read` and start a fresh session. Expected: normal
   state.

- **Evidence:** precise timestamps (tool result, hook start, log line),
  recall log lines, hook/backend log excerpts for the I/O condition, and the
  `setup` skill's status report of pending or deferred work, if reported.
- **Timeout:** T_SCENARIO.
- **Cleanup:** run `stage restore-read` before `cleanup` (cleanup also
  restores permissions).

### RQA-WIKI-01 Wiki duplicate work prevention

- **Purpose:** one source revision reaching several consented paths produces
  at most one paid extraction and one card update.
- **Owner:** wiki.
- **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - A recorded wiki cost approval with `compile.budget` caps.
  - The extractor and verifier engines are available.
  - A fresh fixture, opted in.
- **Steps:**
  1. Ask "wiki compile 켜줘. SessionStart에서 감지한 원문도 wiki 대상으로 해줘."
     Expected: `compile.mode` is on, and the triggers include
     `post_tool_source` and `session_start_source`.
  2. Record the wiki-owned baseline: queue and ledger entries, card files and
     hashes.
  3. Ask the agent to edit `docs/qa/wiki-source.md` so that it contains
     `WIKI_FACT_R2` (PostToolUse path).
  4. Start a fresh session (SessionStart path).
  5. Ask "훅이 놓친 변경 다시 동기화해줘" (manual sync path).
  6. Wait up to T_WIKI for queues to settle.
- **Expected:** for the pair (`docs/qa/wiki-source.md`, SHA-256 of the R2
  bytes):
  - at most one extractor call and one verifier pass;
  - at most one card created or updated;
  - no second paid call.
- **Known gap:** the [architecture boundaries](plans/2026-10-17-architecture-boundaries.md)
  record that revision idempotency is not yet implemented. Until it is, this
  scenario is expected to `FAIL` and blocks a formal release.
- **Evidence:** ledger and queue diffs per path, revision and engine; compile
  logs; card diff.
- **Timeout:** T_WIKI + T_SCENARIO.
- **Cleanup:** keep the fixture for RQA-WIKI-02, then clean it up.

### RQA-WIKI-02 Wiki provenance

- **Purpose:** a published card states exactly which source revision it came
  from and how it was verified, and stale cards are not injected as current.
- **Owner:** wiki; search (injection).
- **Hosts:** Claude Code, Codex.
- **Preconditions:** RQA-WIKI-01 produced a card for R2.
- **Steps:**
  1. Inspect the card's frontmatter.
  2. Probe Q-wiki.
  3. Revert the source to R1 through an agent edit.
  4. Start a fresh session and probe Q-wiki again.
  5. Delete `docs/qa/wiki-source.md` through an agent edit, start another
     fresh session, and probe Q-wiki. Record the source tombstone and any
     derivative card/index cleanup through the wiki and search status views.
- **Expected:**
  - The card's sources name `docs/qa/wiki-source.md`. The recorded source
    revision or hash equals the R2 bytes. `status: verified` appears only
    after a verifier pass, and the verifier identity is recorded.
  - The body contains `WIKI_FACT_R2`, not `WIKI_FACT_R1`.
  - The injected card shows the source path line.
  - After the revert, the R2 card is not injected as verified current content
    (wiki source freshness guard).
  - After source deletion, neither R1 nor R2 is injected as a current verified
    fact. The source removal and derivative index/card state converge within
    T_WIKI; retained audit data is labelled non-current.
- **Evidence:** card bytes and hash, source hashes, probes, recall log
  `card_read_*` and `sources_*` fields.
- **Timeout:** T_WIKI + T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-WIKI-03 Wiki compile OFF

- **Purpose:** when compile is off, no wiki work and no paid calls happen,
  while file indexing still works.
- **Owner:** wiki; file sync and search for the contrast check.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fixture where compile was on (an RQA-WIKI-01 fixture,
  or freshly enabled).
- **Steps:**
  1. Ask "wiki compile 꺼줘". Expected: `compile.mode: "off"`.
  2. Record the wiki baseline.
  3. Ask the agent to edit `wiki-source.md`.
  4. Start a fresh session.
  5. Request a sync.
  6. Repeat the sync and fresh SessionStart once to exercise retry paths.
  7. Delete `wiki-source.md` through an agent edit and repeat the sync and
     fresh SessionStart. Probe Q-wiki and the deletion state.
- **Expected:**
  - No new queue or ledger entries.
  - No extractor or verifier invocation.
  - Card bytes unchanged.
  - On QMD, the raw document's changed locator is current within T_FRESH and
    disappears after deletion. On Laya v2, wiki indexing still works, but
    the changed raw-only content creates no Laya document or search hit;
    the unchanged card is never presented as a fresh revision of that source.
    Compile remains OFF across retry and deletion.
- **Evidence:** wiki listings before and after, probes, absence of compile
  activity in the logs.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RES-01 Ordinary work is not blocked (no learning)

- **Purpose:** with learning not enabled, ordinary hooks finish within budget
  and no training work starts.
- **Owner:** runtime.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** no learning profile and no grants (setup status reports
  learning as deferred or off); a fresh fixture, opted in and indexed.
- **Steps:**
  1. In one session, run five probes, two agent edits and one sync request.
  2. Then start one fresh session.
  3. Snapshot the QA user's processes (`ps`, filtered to the plugin runtime
     roots) before and after.
- **Expected:**
  - No hook timeout or failure.
  - Every probe finishes within T_TURN.
  - No trainer, teacher or evaluation process is started, and no learning
    wake is executed.
  - Ordinary recall is never refused for a learning resource reason.
- **Evidence:** turn timings, process snapshots, setup status.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RES-02 Training-only resource guard

- **Purpose:** an approved training job yields to ordinary work, and ordinary
  work is never blocked by it.
- **Owner:** learning (job); runtime (admission and yield).
- **Hosts:** Claude Code (Codex optional).
- **Status rule:** `BLOCKED` unless all of the following are recorded:
  - written learning approval;
  - an approved private test namespace;
  - a disposable synthetic corpus;
  - an approved local model;
  - an OS observation baseline.
  It never runs on manuscripts, personal projects or operating databases.
- **Steps (once approved):**
  1. Ask for a learning profile proposal through the `setup` skill. Accept
     only that exact proposal.
  2. Start one approved learning job on the synthetic corpus. Record its
     content job identity, `runtime-wakes.sqlite3` wake revision/due/status,
     training cursor group, and the owner process group before work begins.
     A duplicate wake for the same project must coalesce rather than start a
     second trainer or consume the same evidence twice.
  3. Hold admission with a reproducible foreground demand or approved OS
     pressure fixture. While the learning wake is retained, run five probes,
     two edits and one fresh session; record hook timings and confirm the
     queue's retry/backoff state. Release the condition and observe the same
     durable job resume without a new capture or teacher pass. Hold admission
     past the bounded drain lifetime; identify the product event that re-arms
     the queued job. Repeat with a weekly quota checkpoint and prove that
     `deferred_quota` retains a wake instead of receiving a completed ACK.
  4. During training, request a cooperative stop at an optimizer-group
     boundary. Verify that the exact committed cursor/head survives worker
     exit and restart; the resumed run starts at the next group and does not
     repeat a committed group. Also test an interrupted `running` wake, a
     duplicate wake during dispatch, and bounded queue capacity without
     losing an existing learning job. Repeat cooperative yield at least four
     times to show it never counts as a terminal failure or blocks the job.
     Enqueue a higher-priority index/update wake while training is active,
     then record when it is serviced; ordering only at dispatch start is not
     proof that active training yielded.
  5. At OS warning or failed monitor/cleanup proof, stop through the owned
     process path. Record whether the child and descendants ended; an
     unproved owned-group cleanup must leave admission blocked, not silently
     reclaim the reservation.
- **Expected:**
  - Ordinary hooks stay within budget.
  - Training yields cooperatively to foreground demand and resumes from the
    exact committed group when admission returns. Queue retry survives worker
    interruption and a new session; stale revision ACK cannot erase a newer
    wake. No second trainer, teacher call or evidence consumption occurs for
    the same job.
  - Resource refusal leaves the durable job pending and never turns it into
    a successful training receipt. An unproved cleanup keeps admission closed.
  - No checkpoint is promoted or applied without its own approval.
  - Training cleanup is proved; an unsettled cleanup reservation is `FAIL`.
- **Evidence:** wake/job revision and retry state, exact cursor/head hashes,
  content receipt/evidence identity, admission/monitor observations, process
  group and cleanup proof, fresh-session transcript and hook timings. Confirm
  production runs do not set fixture/sandbox bypass flags, and show whether
  direct `shared.launch` and queued `runtime_work.dispatch` paths both obey
  admission. Existing synthetic tests in `test/runtime_resources_test.py`,
  `test/runtime_training_watch_test.py`,
  `test/fixtures/runtime-wake-policy-checks.py`, and
  `test/fixtures/context-learning-coordination-checks.py` are L0 coverage,
  not this installed-candidate result.
- **Timeout:** 60 min.
- **Cleanup:** remove the test namespace through its approved path; preserve
  any unsettled state for review.

### RQA-PLAT-01 Unsupported or unverified platform

- **Purpose:** on a platform without managed support, the plugin stays
  harmless and preserves existing global QMD.
- **Owner:** install.
- **Cells:** U-intel, U-linux. **Hosts:** whichever are available.
- **Preconditions:** record before-state hashes of the global QMD binary path,
  its config and data.
- **Steps:**
  1. Install the candidate.
  2. Start a fresh session in a fixture.
  3. Ask "이 프로젝트 auto-context 설치 상태 보여줘".
  4. Probe Q-alpha.
- **Expected:**
  - `setup_required` or `unsupported_managed_platform`, with manual guidance.
  - Global QMD binaries, config and data unchanged.
  - No hook failure, and no injection claimed through v2 routing.
- **Evidence:** status output, before and after hashes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; restore the account state.

## 10. Domain ownership

Scenarios respect the six owners in
[the architecture boundaries](plans/2026-10-17-architecture-boundaries.md).
Two rules apply:

- A verdict uses evidence produced by the owning domain.
- A scenario must never edit another domain's state (queues, snapshots,
  ledgers, journals, pointers, learning DB) to force a result. A cross-domain
  expectation, such as "file sync must not enqueue wiki work while compile is
  off", is judged on the owning domain's state, which is only read.

| Owner | Scenarios | Verdict evidence |
|---|---|---|
| File sync | SYNC-01, GIT-01..03, RACE-01, HOOK-0x edits | Searchable state after the change set; snapshot and change reports through the skills |
| Search | HOOK-0x, SYNC-01, GIT-*, RACE-01, WIKI-02 injection | Recall log selection lines, injected context |
| Wiki | WIKI-01..03, SYNC-01 negative | Wiki queues, ledgers, cards, compile logs (read only) |
| Learning | RES-02 | Learning receipts and status through the `setup` skill (read only) |
| Install | ID-00, INST-01..04, PLAT-01, GIT-02 enrollment | Identity hashes, `setup` skill outputs, journal phase as reported |
| Runtime | RES-01, RES-02, RACE-01 retry, HOOK-0x hook budgets | Hook timings, process snapshots, deferred or retry status |

## 11. Changes after QA: reruns and identity match

- **Binding.** Every scenario record carries `artifactManifestSha256`. A
  result applies only to that hash.
- **Change detection.** Any change to the candidate tree after QA produces a
  new hash and invalidates every result for the old hash. This covers code,
  skills, hooks, manifests, locks, docs and version strings. List the changed
  paths with `git diff --name-only <qa-commit> <new-commit>`.
- **Rerun set.** Always the core smoke (RQA-ID-00 + RQA-HOOK-01 +
  RQA-HOOK-02), plus every scenario mapped from the changed paths:

| Changed paths | Rerun in addition to core smoke |
|---|---|
| `core/recall.py`, `core/qmd_project_query.py`, `core/qmd_route.py`, `core/wiki_freshness.py`, `backend/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-02 |
| `core/sync.py`, `core/file_change_queue.py`, `core/index_enqueue.py`, `core/update.sh`, `skills/sync/**`, `skills/update/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-03 |
| `core/wiki_*`, `skills/wiki-*/**`, `skills/enable-compile/**`, `agents/**` | WIKI-01..03 |
| `core/context_learning/**` | RES-01, RES-02 |
| `core/setup_entry.py`, `core/shared_*`, `core/install_update.py`, `core/qmd_shared_store.py`, `core/setup_guard.py`, `core/runtime_update.py`, `locks/**`, `skills/setup/**` | INST-01..04, PLAT-01, GIT-02 |
| `core/runtime_work.py`, `core/runtime_wake_policy.py`, `core/qmd_endpoint.py`, resource coordination modules, `backend/index_worker.sh` | RES-01, RES-02, RACE-01 |
| `core/config.py`, `hooks/**`, `hermes_adapter/**`, `plugin.yaml`, `__init__.py`, any host manifest or marketplace file | **All** scenarios |
| Version strings only | INST-01, INST-02 |
| Documentation only (`docs/**`, `README.md`; not `skills/**` or `agents/**`) | Core smoke only |
| Any path not listed | **All** scenarios |

- **Deployed identity.** The marketplace entry is not pinned to a revision
  (see [install-update contract](install-update-contract-20261006.md)). After
  publishing, install from the **public** marketplace in a clean QA account.
  Run RQA-ID-00 and the core smoke. The deployed `artifactManifestSha256` must
  equal the QA'd hash. On a mismatch, the release is not announced. Retract or
  supersede it and record the incident.

## 12. Cleanup and recovery (all scenarios)

- Remove fixtures only with the helper's `cleanup`. Evidence stays in `$EVID`.
- After the gate:
  - Uninstall the plugin from the QA account through each host's plugin flow,
    or restore the clean account or snapshot. Record which.
  - Never touch a maintainer's real host settings, projects or databases.
- When a scenario leaves `recovery_required`, an unsettled cleanup or startup
  reservation, or an unexpected pointer state:
  - Stop dependent scenarios and preserve the state.
  - Record `FAIL`, or `BLOCKED` if the QA harness caused it.
  - Resolve it only through the documented skill flows. Never delete journals,
    reservations or pointers to continue.

## 13. Authoring-pass status (2026-10-17)

| Scenario | Status | Note |
|---|---|---|
| RQA-ID-00, RQA-INST-01..04, RQA-HOOK-01..02, RQA-SYNC-01, RQA-GIT-01..03, RQA-WIKI-01..03, RQA-RES-01, RQA-PLAT-01 | `NOT RUN` | Specification only. No installed host, QMD, hook, install, model or network execution in this pass. The synthetic fixture helper was tested separately at L0. |
| RQA-RACE-01 | `BLOCKED` | Part B now targets the selected Laya Index engine. It has a QA-only fixture-scoped barrier and L0 late-attempt and dead-claim source tests (2026-10-18; guarded run pending). The installed-host scenario has not run. The legacy QMD writer has no barrier and its late-worker safety is not claimed (release-note limitation). |
| RQA-RES-02 | `BLOCKED` | Needs separate learning approval, safe synthetic installed-candidate resources, and observed OS-pressure stop/resume. L0 queue tests do not satisfy this row. |
| LI-INST, LI-MIG, LI-SCOPE, LI-RECOVER, LI-COEXIST, LI-VECTOR, LI-RECALL | `NOT RUN` | Exact marketplace install and fresh-host scenarios have not run. Production provider approval, migration cutover, and retrieval parity remain unresolved. |
