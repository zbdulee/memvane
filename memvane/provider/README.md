# 임베딩 provider

모델과 런타임의 정체성을 검사하고 문서·질의 벡터를 생성한다. provider·모델·런타임이 바뀌면 다시 온보딩한다.

구현: `embed_runtime.py`, `query_vectors.py`.

provider·worker·chunker 파일의 bundle 상대 경로와 `memvane/provider/adapters/` 접두사는 `kernel/bundle_paths.py`가 정의하고 provider 코드도 따로 정의하지 않는다. 보호 파일 `adapters/local_embed_provider.py`는 manifest의 `providerRelativePath` 모양을 스스로 검사한다.

질의 벡터는 `api.query_vectors(provider, prompt, budget, chunks=, allowed=)`가 hook 프로세스 안에서 만든다(자식 프로세스 없음). 요청 하나는 reviewed provider(attested adapter)의 socket 왕복이고 그 왕복은 socket 연산마다 시한을 건다. 요청 전체의 절대 시한은 여기서 지킨다: 왕복을 daemon thread에서 부르고 recall의 deadline까지만 기다린다. 그때까지 끝나지 않은 요청은 `provider_unavailable`로 포기하고 그 thread는 adapter의 socket 시한 안에 끝나거나 프로세스와 함께 끝난다. `allowed`에 없는 오류 코드는 `provider_result_invalid`로 바꾼다. 요청에 주는 시간은 recall에 남은 시간이고 2초를 넘지 않는다. `chunks`가 참이면 긴 프롬프트를 조각으로 묻는다: 6,000자 이하는 전체 한 번, 6,000자를 넘거나 2,000자를 넘는 전체 요청이 실패하면 1,200자 창 최대 4개(끝에서부터 3개와 첫 창)를 순서대로 묻고, 실패한 창은 뒤 절반으로 한 번 더 묻는다. 시간이 끝나면 남은 창은 버리고 그때까지 받은 벡터로 답한다. 응답 정체성이 다르거나 연결한 socket이 교체되면 즉시 질의를 거절하고 이미 받은 창의 벡터도 버린다. 답은 `(벡터 1~4개, 물은 횟수)`이고 하나도 없으면 `IndexContractError`다. 편집 recall은 `chunks` 없이 질의를 전체로 한 번만 묻는다.

증명은 런타임 세대의 파일을 매번 runtime.json과 attestation에 대조하되, 바뀌지 않은 파일은 다시 읽지 않는다. 프로세스 안에서는 (경로, inode, 크기, mtime_ns)로(`kernel/persistent_identity.cached_by_identity`), 프로세스 사이에서는 런타임 루트의 `embed-runtime/file-digests.json`(소유자 전용 0600, 0700 루트 안에서 원자적 교체)에 남긴 (경로, inode, 크기, mtime_ns, ctime_ns)로 판정한다(`runtime_digests.py`). 이 파일은 캐시일 뿐 증거가 아니다: 없거나 손상되거나 안전하지 않으면 전부 다시 해시하고, 장치 번호는 키에 넣지 않는다.

실행 파일의 해시가 설치 때와 달라지면 worker 기동 전에 재증명 원장에서 승인된 파일 쌍과 임베딩 정체성을 확인한다. 쌍이 미등록이거나 재증명 원장의 형식·소유권·권한이 유효하지 않거나 경로가 심볼릭 링크이면 `attestation_invalid`로 기록한다. 원장을 읽는 중 I/O 오류가 발생하거나 파일이 변경되면 `startup_failed`로 기록하고 재시도한다.

시험은 `fixture_port.py` 한 모듈로 설치된 provider를 대신한다. use case의 `provider=`·`port=` 인자가 먼저이고, 인자가 없으면 `MEMVANE_TEST_RUNTIME_ROOT`가 절대 경로이며 그 아래 소유자 전용 일반 파일 `provider-fixture.json`(선언형 spec)이 있을 때만 이 모듈 안의 결정적 fixture provider가 답한다. 둘 다 없으면 제품 경로만 탄다. fixture handle은 문서당 chunk 하나만 만들고 2,700 UTF-16 단위를 넘는 본문은 거절한다. 제품 모듈은 `tests` 패키지를 import하지 않는다.

seam이 켜졌을 때만(`fixture_port.active`) `api.is_reviewed_provider`가 fixture handle을 인정하고, 질의 벡터 요청이 spec의 `queryEmbedding`(기본 `vector`, cold provider는 `provider_unavailable`)으로 답한다. spec 파일이 안전하지 않거나 형식이 틀리면 두 검사는 seam이 꺼진 것으로 보고 제품 경로를 탄다. 제품 코드는 `MEMVANE_TEST_RUNTIME_ROOT`를 설정하지 않는다(정적 검사 `tests/structure/static.test.mjs`). spec의 `queryMaxChars`는 그보다 긴 질의 텍스트를 `provider_result_invalid`로 거절하고, `queryLog`는 물은 텍스트의 글자 수를 spec 옆 `provider-query-log`에 적는다.
