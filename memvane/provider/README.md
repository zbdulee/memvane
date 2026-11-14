# 임베딩 provider

모델과 런타임의 정체성을 검사하고 문서·질의 벡터를 생성한다. provider·모델·런타임이 바뀌면 다시 온보딩한다.

구현: `embed_runtime.py`.

증명은 런타임 세대의 파일을 매번 runtime.json과 attestation에 대조하되, 프로세스 안에서 (경로, inode, 크기, mtime_ns)가 그대로인 파일은 다시 읽지 않는다(`kernel/persistent_identity.cached_by_identity`).
