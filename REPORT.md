# MemVane 0.43.41 PR5 bridge 운영 검증

v4 journal에서 v5 reader bridge를 준비할 때 이전 번들의 완료된 upgrade intent가 있으면 기존 복구 증명으로 완료 여부와 선택 route를 확인한 뒤 receipt로 보존하고 새 intent를 준비한다. 완료되지 않은 intent는 보존하고 전환을 거절한다. 백업과 선택된 DB는 삭제하지 않는다.

동일 커밋의 세 shard와 독립 재리뷰 결과, 실제 양 호스트 설치·bridge 전환·운영 검증은 `PROGRESS-CODEX.md`와 해당 검사 로그에 기록한다. 합성 검사와 실제 호스트 영수증은 별도다.

PR7의 합성 view 지연 목표 미달과 fork 자식의 -11 종료는 별도 미해결 항목이다. 기존 백업과 QMD origin rollback DB의 실제 삭제는 별도 사용자 요청이 필요하다.
