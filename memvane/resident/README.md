# Resident service

설치별 provider worker와 요청을 사용자 단위 서비스에서 관리한다.

구현: `bundle_lifecycle.py`, `embed_resident.py`, `resident_service.py`.

서비스는 idle 상태로 끝나지 않는다. 대신 tick마다 서비스 root와 lease 파일이 처음 잡은 것(같은 경로·device·inode)인지 다시 확인하고, 사라지거나 바뀌면 worker를 drain한 뒤 종료한다(`resident.drain` 이벤트 reason `service_root_changed`).

MemVane을 켠 호스트가 하나도 없으면 서비스는 drain하고 끝나며 successor를 띄우지 않는다(reason `bundle_uninstalled`). 판정은 30초 간격의 번들 확인에서 한다: Codex·Claude·Hermes의 설치 기록을 전부 읽었고 켜진 번들이 0개인 상태가 `host_bundle.UNINSTALLED_GRACE_SECONDS`(300초) 넘게 이어졌을 때만이다. 읽지 못한 기록은 아무것도 철회하지 않고, 유예는 호스트가 한 버전을 내리고 다음 버전을 설치하는 사이를 넘긴다. 확실히 꺼진 것만 꺼진 것으로 센다: Codex 목록이 비었는데 `config.toml`이 MemVane을 켜 두었으면(이 프로세스의 PATH에 `codex`가 없는 경우) 모름이고, Claude 목록에 MemVane이 설치돼 있으나 꺼진 행이 있으면(프로젝트가 켤 수 있다) 모름이다.

실행 번들이 사라지거나 최신 설치 버전이 아니면 기존 요청과 worker를 drain한 뒤 기존 successor lock을 통해 새 번들에 인계한다. 설치가 진행 중이면 새 번들을 확인할 때까지 lease를 보유한다. 모델 worker는 해당 설치의 provider 증명을 통과한 뒤에만 시작한다.

기동 직전 증명에서 거부되거나 증명 파일을 읽지 못하면 그 설치의 상태와 재시도 횟수만 기록한다. 같은 모델을 쓰는 다른 설치는 자신의 증명으로 기동할 수 있다. terminal 증명 실패는 기록 시각에서 한 시간 뒤 다시 시도하고, 서비스는 남은 기한과 5분 중 짧은 시간 뒤 설치 상태를 확인한다. 벽시계가 기록 시각보다 이르면 저장된 대기를 만료로 취급한다.

호스트 설치 경로 밖의 플러그인 사본(dev worktree 등)은 live 서비스를 띄우거나 교체하지 않고, 프로젝트를 등록하거나 adopt를 요청하지도 않는다(`ResourceDeferred` `live_write_requires_plugin_cache`). 그 사본의 recall은 lexical 결과로 답한다.
