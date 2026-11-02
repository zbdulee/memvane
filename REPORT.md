# MemVane 0.43.34 검증 상태

기준 main은 `52ffcbf`이고 브랜치는 `codex/review-remediation-priority1`이다. 이 릴리스는 중간 리뷰의 1순위 운영 복구 항목을 다룬다. PR5 제품 구현은 포함하지 않는다.

| 항목 | 현재 동작 | 확인 범위 |
|---|---|---|
| RT-F2 | 일시적 시작 실패 3회 뒤 300초 cooldown을 거쳐 재시도한다. legacy 디스크 terminal도 회복한다. 잘못된 lock·socket·symlink lock은 identity terminal로 거절하며, 재시도는 자원 admission과 매 시작 attestation을 유지한다 | 합성 RED→GREEN, 실제 파일 보안 오류 3건 RED→GREEN, owner·recall·resident focused 5개 파일 통과 |
| RT-F1 | learning signature에 plugin version을 포함한다. 코드가 같은 새 버전도 정상 resident adoption 경로에 진입하고 같은 릴리스의 두 host는 signature를 공유한다 | version-only release RED→GREEN, inference fixture 38건 통과 |
| W1 | session-only 카드는 파일 출처 lifecycle 대상에서 제외한다. UNPROVEN은 대량 삭제 비율의 분자·분모에서 제외하고 퇴역 상한은 유지한다 | session 퇴역·guard 고착 RED→GREEN, lifecycle/trigger 8개 검사 통과 |
| U1 | journal pending 전의 upgrading fence에서 새 host target·동일 commit 재추출을 정상 재증명한다. 원래 backup·old record·counts와 fence를 유지한다 | 실제 fence 중단 RED→GREEN, fresh-bundle fixture 23건 통과 |
| U3 | 현재 route가 이동한 프로젝트의 legacy 비카드 잔여물은 재이동을 유발하지 않는다. 사용자 메모와 swap 파일은 보존한다 | layout adopt 오판 RED→GREEN, dir-move fixture 27건 통과 |
| W2 | notify는 reader와 같은 행 검증을 적용한다. 기존 inbox의 불량 행은 정상 hint와 discovery를 막지 않는다. binding·상한·유효 행 token 검사는 유지한다 | 원본 inbox 오류 RED→GREEN, routing fixture 55건 통과 |

U1의 journal pending 이후 target 교체·소실 복구는 남은 경계다. pending foreign target 요청은 현재 거절하며 intent·ledger·journal을 유지한다. 이 릴리스는 U1 전체 상황의 자동 복구를 주장하지 않는다. session-only 카드의 recall 신뢰 판정도 별도로 유지하며 파일 provenance를 합성하지 않는다.

보안 분류 수정이 반영된 canonical 임시 HOME 전체 `npm test`는 677개 중 674개 통과, 실패 0, 선택 실행 3개 skip이며 896.200초다. guard는 passed/exit0/completed:true/settled:true다. 첫 전체 실행은 HOME의 /tmp 별칭으로 경로 매칭 1건이 실패하여 guard 정리 후 중단 상태로 보존한다. canonical HOME의 해당 focused 11건과 전체 재검사는 통과한다.

`85e6005` 독립 exact-head 리뷰는 RT-F2-SEC 1건으로 hold다. 실제 파일로 재현한 보안 오류 분류 수정 후 focused 5개와 전체 검사는 통과하며, 새 HEAD의 좁은 재리뷰·추가 Agy 리뷰·PR-head CI·merge·양호스트 설치와 운영 검증은 대기 상태다. 보호 파일 3개는 무변경이다. 검사·모델 작업은 전역 개발 guard lease를 공유하며 테스트 HOME·runtime은 임시 경로다. Claude 호출은 없다.

상세 RED/GREEN 로그와 남은 항목은 `/Users/dulee/work/memvane-handoff/context/review-remediation-status.md` 및 같은 디렉터리의 `remediation-*.log`에 있다.
