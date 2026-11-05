# MemVane 0.43.44 PR5 bridge 운영 검증

v4 reader bridge는 backup과 reader seal 준비가 끝난 뒤 양 호스트의 등록 hook preflight를 다시 확인한다. 기존 영수증이 유효하면 재사용하고 만료됐으면 호스트를 다시 관측한다. 영수증 유효기간과 변환 직전 검증은 그대로 유지한다.

활성 번들 전환은 같은 작업에서 backup 전체 해시와 SQLite 무결성을 한 번 증명하고, 같은 원본 증거가 재사용되는 동안 SQLite 변경 버전과 파일·WAL·journal의 정체성 및 수정 상태를 확인한다. 변형이 감지되면 전환을 거절한다. 증명과 감시 연결은 해당 프로세스의 한 전환에서만 유지한다.

동일 커밋의 세 shard와 독립 리뷰, 실제 SE 양 호스트 설치·전환·proof·recall 및 단계별 시간은 `PROGRESS-CODEX.md`와 검사 로그에 기록한다. PR5의 다른 프로젝트 운영 검증은 범위 밖이며 정상으로 추정하지 않는다.

PR7의 합성 view 지연 목표 미달과 fork 자식의 -11 종료는 별도 미해결 항목이다. 기존 백업과 QMD origin rollback DB의 실제 삭제는 별도 사용자 요청이 필요하다.
