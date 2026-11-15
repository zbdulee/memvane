# 공통 도구

경로, 자원 보류, 시간 예산과 파일 identity를 제공한다.

`bundle_paths.py`는 실행 중인 플러그인 루트와 그 아래 provider 파일의 상대 경로(provider·worker·chunker), `providerRelativePath` 접두사 검사를 정의한다. 이 값은 attestation과 artifact 기록에 그대로 들어가므로 다른 도메인은 문자열을 다시 쓰지 않고 이 이름을 쓴다.

구현: `atomic_rename.py`, `bundle_paths.py`, `cooldown.py`, `errors.py`, `hook_budget.py`, `memvane_execution_guard.py`, `memvane_paths.py`, `memvane_version.py`, `package.py`, `paths.py`, `persistent_identity.py`, `process.py`, `resource_deferred.py`, `safe_rmtree.py`.
