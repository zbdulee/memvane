# MemVane 0.43.36 검증 상태

기준 main은 `463e029`이며 브랜치는 `codex/review-runtime-portability`다. 범위는 중간 리뷰의 R1·U2·R2다. git-blob PR5 제품 구현은 포함하지 않는다.

자동 runtime 준비는 지원 Node 후보의 npm을 함께 확인하고, 실제 Node 설치 prefix의 일반 경로와 Homebrew libexec 경로를 읽는다. npm 없는 후보는 다음 후보로 넘기며 explicit Node 선택과 Node-only 탐색 계약은 유지한다.

번들·provider 증명은 committed tree OID와 설치 바이트를 대조한다. 실제 ENOENT인 skip-worktree 파일만 설치 바이트로 증명하며, materialized source·부모 링크·cache mode·추가 파일 검사는 유지한다. 제품 Git은 replacement·graft·lazy fetch·transport를 금지한다. 지원 Git 판별이 불가능한 partial clone과 읽을 수 없는 설정은 전용 재시도 사유로 거절한다. 비활성 checkout capture는 pure Git 버전 판별만 공유한다.

runtime producer는 자신이 봉인·복사·활성화한 generation을 반환한다. bundle upgrade는 그 generation을 최초 provider review와 active·bound·inactive·fenced recovery 재검토까지 전달한다. named generation은 현재 소유 runtime의 canonical/private 경로와 전체 봉인 파일을 재검증하며 active/previous 포인터를 읽거나 쓰지 않는다. 명시 generation 없는 수동 review는 기본 active 선택을 유지한다.

R1의 Homebrew·npm-less 첫 후보는 원본에서 실제 2건 RED이며 관련 2파일 검사는 32.465초로 통과한다. U2는 실제 file:// partial clone에서 online fetch2·missing33→31과 offline fetch1을 재현한다. 최종 관련 3파일 검사는 29.416초로 통과하며 missing34→34·fetch/transport0을 확인한다. 기존 replacement-ref 거절 사유도 유지한다. R2의 원본 5건은 unlock→return 및 ensure→실제 review 경계의 G1 오선택으로 RED다.

R2 보강 fixture8개는 실제 G0 파일 digest와 proposal, global G1·pointer 불변을 확인한다. binding+manifest5개 Node 검사는24.215초로 통과한다. 첫 두 보강 실행의 socket 길이·상속 mock·patch 대상 오류는 fixture에서 보정하며 실패 로그를 보존한다. 리뷰 전 전체 검사는683개 중680 pass·0 fail·3 skip,1253.244초이며 guard는 exit0/passed/completed:true/settled:true다. guard-unit21도 통과한다. 독립 Astra의 P2 두 항목은 실제 RED다. true,false 등록 설정의 object 명령 진입과 과거 engine bundle/journal identity 거절을 재현한다. helper를 기존 필수 laya_package.py로 옮겨 정확한 artifact/schema 계약을 유지하며 모든 config 행의 등록 증거를 누적한다. 해당 focused7파일 중6개는 통과하고 capture1개는 기존3초 예산 소진으로 실패한다. 코드·예산·판정 변경 없는 단독 재검사는71.499초로 통과하며 첫 실패는 보존한다. 수정 후 전체v2는684개 중680 pass·1 fail·3 skip,1237.148초다. 삭제한 helper가 미반영 Git index에 남아 bytecode fixture 복사에서 FileNotFoundError가 난다. 삭제를 index에 반영한 뒤 동일 검사4.020초는 통과하며 제품·테스트 코드/예산/판정은 무변경이다. clean HEAD의 최종 전체와 exact-head 재리뷰는 별도 검증한다. 각 전체 검사 중에는 제품 코드·테스트·개발문서를 고정하고 REPORT에는 종료 결과만 반영한다. 독립 exact-head 리뷰·PR·양호스트 설치와 운영 검증 상태는 외부 감사표에 기록한다. 보호 파일 3개는 무변경이며 7개 manifest는 0.43.36이다. 실제 모델·운영 DB는 fixture 입력이 아니며 개발용 Claude 호출은 없다. CI는 개발과 별도로 관찰한다.

현재 계약은 `docs/development-runtime-bundle-proof.md`, 세부 근거는 `/Users/dulee/work/memvane-handoff/context/priority2-*`다. 설치된 0.43.35의 운영 검증 근거는 같은 외부 디렉터리의 `u1-release-completion-audit.md`다. 0.43.36 설치·운영 gate는 아직 미확인이다.
