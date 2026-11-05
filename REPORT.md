# MemVane 0.43.43 PR5 bridge 운영 검증

활성 번들 전환은 같은 작업 안에서 backup의 전체 해시와 SQLite 무결성을 한 번 증명한다. 이후 같은 원본 증거가 재사용되는 동안 SQLite 변경 버전과 파일·WAL·journal의 정체성 및 수정 상태를 확인하며, 변경이 감지되면 전환을 거절한다. 증명과 감시 연결은 해당 프로세스의 한 전환에서만 유지한다.

v4 reader bridge는 게시 generation의 catalog scan이 구버전 정리로 사라졌을 때 같은 raw digest의 현재 완료 scan을 전체 행·파일 수·바이트 수로 재검증한다. v4의 미완료 intent는 원래 ledger·journal·backup·선택 metadata와 새 대상 번들·provider를 재증명할 때만 교체한다.

동일 커밋의 세 shard와 독립 리뷰 결과, 실제 양 호스트 설치·bridge 전환·운영 검증은 `PROGRESS-CODEX.md`와 검사 로그에 기록한다. 합성 검사와 실제 호스트 영수증은 별도다. PR7의 합성 view 지연 목표 미달과 fork 자식의 -11 종료는 별도 미해결 항목이다. 기존 백업과 QMD origin rollback DB의 실제 삭제는 별도 사용자 요청이 필요하다.
