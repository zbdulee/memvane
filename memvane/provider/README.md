# 임베딩 provider

모델과 런타임의 정체성을 검사하고 문서·질의 벡터를 생성한다. provider·모델·런타임이 바뀌면 다시 온보딩한다.

구현: `embed_runtime.py`, `query_embed_child.py`.

provider·worker·chunker 파일의 bundle 상대 경로와 `memvane/provider/adapters/` 접두사는 `kernel/bundle_paths.py`가 정의하고 provider 코드도 따로 정의하지 않는다. 보호 파일 `adapters/local_embed_provider.py`는 manifest의 `providerRelativePath` 모양을 스스로 검사한다.

질의 벡터는 `query_embed_child_argv()`가 주는 자식 프로세스에서 만든다. stdin으로 `{manifest, prompt, timeout, allowed}` JSON을 받고 stdout에 `{"vector": [...]}` 또는 `{"error": code}`를 쓴다. `allowed`에 없는 오류 코드는 `provider_result_invalid`로 바꾼다.

증명은 런타임 세대의 파일을 매번 runtime.json과 attestation에 대조하되, 바뀌지 않은 파일은 다시 읽지 않는다. 프로세스 안에서는 (경로, inode, 크기, mtime_ns)로(`kernel/persistent_identity.cached_by_identity`), 프로세스 사이에서는 런타임 루트의 `embed-runtime/file-digests.json`(소유자 전용 0600, 0700 루트 안에서 원자적 교체)에 남긴 (경로, inode, 크기, mtime_ns, ctime_ns)로 판정한다(`runtime_digests.py`). 이 파일은 캐시일 뿐 증거가 아니다: 없거나 손상되거나 안전하지 않으면 전부 다시 해시하고, 장치 번호는 키에 넣지 않는다.

시험은 `fixture_port.py` 한 모듈로 설치된 provider를 대신한다. use case의 `provider=`·`port=` 인자가 먼저이고, 인자가 없으면 `MEMVANE_TEST_RUNTIME_ROOT`가 절대 경로이며 그 아래 소유자 전용 일반 파일 `provider-fixture.json`(선언형 spec)이 있을 때만 이 모듈 안의 결정적 fixture provider가 답한다. 둘 다 없으면 제품 경로만 탄다. fixture handle은 문서당 chunk 하나만 만들고 2,700 UTF-16 단위를 넘는 본문은 거절한다. 제품 모듈은 `tests` 패키지를 import하지 않는다.
