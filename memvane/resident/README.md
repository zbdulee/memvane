# Resident service

설치별 provider worker와 요청을 사용자 단위 서비스에서 관리한다.

구현: `bundle_lifecycle.py`, `embed_resident.py`, `resident_service.py`.

서비스는 idle 상태로 끝나지 않는다. 대신 tick마다 서비스 root와 lease 파일이 처음 잡은 것(같은 경로·device·inode)인지 다시 확인하고, 사라지거나 바뀌면 worker를 drain한 뒤 종료한다(`resident.drain` 이벤트 reason `service_root_changed`).

실행 번들이 사라지거나 최신 설치 버전이 아니면 기존 요청과 worker를 drain한 뒤 기존 successor lock을 통해 새 번들에 인계한다. 설치가 진행 중이면 새 번들을 확인할 때까지 lease를 보유한다. 모델 worker는 해당 설치의 provider 증명을 통과한 뒤에만 시작한다.

호스트 설치 경로 밖의 플러그인 사본(dev worktree 등)은 live 서비스를 띄우거나 교체하지 않고, 프로젝트를 등록하거나 adopt를 요청하지도 않는다(`ResourceDeferred` `live_write_requires_plugin_cache`). 그 사본의 recall은 lexical 결과로 답한다.
