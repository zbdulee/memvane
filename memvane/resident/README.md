# Resident service

설치별 provider worker와 요청을 사용자 단위 서비스에서 관리한다.

구현: `embed_resident.py`, `resident_service.py`.

서비스는 idle 상태로 끝나지 않는다. 대신 tick마다 서비스 root와 lease 파일이 처음 잡은 것(같은 경로·device·inode)인지 다시 확인하고, 사라지거나 바뀌면 worker를 drain한 뒤 종료한다(`resident.drain` 이벤트 reason `service_root_changed`).
