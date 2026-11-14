# 임베딩 provider

모델과 런타임의 정체성을 검사하고 문서·질의 벡터를 생성한다. provider·모델·런타임이 바뀌면 다시 온보딩한다.

구현: `embed_runtime.py`, `query_embed_child.py`.

provider·worker·chunker 파일의 bundle 상대 경로와 `memvane/provider/adapters/` 접두사는 `kernel/bundle_paths.py`가 정의하고 provider 코드도 따로 정의하지 않는다. 보호 파일 `adapters/local_embed_provider.py`는 manifest의 `providerRelativePath` 모양을 스스로 검사한다.

질의 벡터는 `query_embed_child_argv()`가 주는 자식 프로세스에서 만든다. stdin으로 `{manifest, prompt, timeout, allowed}` JSON을 받고 stdout에 `{"vector": [...]}` 또는 `{"error": code}`를 쓴다. `allowed`에 없는 오류 코드는 `provider_result_invalid`로 바꾼다.

증명은 런타임 세대의 파일을 매번 runtime.json과 attestation에 대조하되, 프로세스 안에서 (경로, inode, 크기, mtime_ns)가 그대로인 파일은 다시 읽지 않는다(`kernel/persistent_identity.cached_by_identity`).
