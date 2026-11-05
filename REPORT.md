# MemVane 0.43.42 PR5 bridge 운영 검증

v4 reader bridge는 게시 generation이 봉인한 catalog scan이 이전 catalog 정리로 사라진 경우, 같은 raw digest를 가진 현재 완료 scan의 모든 행과 파일 수·바이트 수를 다시 검증해 검색 증명에 사용한다. 값이 다르거나 완료 snapshot이 없으면 거절한다.

이전 번들의 완료 intent는 검증 후 receipt로 보존한다. 전환 전 v4의 미완료 intent는 기존 ledger·journal·backup·선택 metadata가 그대로임을 증명하고 새 대상의 번들과 provider를 다시 검증할 때만 교체한다. 중단된 전환의 기존 backup은 재사용하며 삭제하지 않는다.

동일 커밋의 세 shard와 독립 재리뷰 결과, 실제 양 호스트 설치·bridge 전환·운영 검증은 `PROGRESS-CODEX.md`와 해당 검사 로그에 기록한다. 합성 검사와 실제 호스트 영수증은 별도다.

PR7의 합성 view 지연 목표 미달과 fork 자식의 -11 종료는 별도 미해결 항목이다. 기존 백업과 QMD origin rollback DB의 실제 삭제는 별도 사용자 요청이 필요하다.
