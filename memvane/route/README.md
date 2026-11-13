# Route

프로젝트 route 기록과 settings 정체성·색인 루트를 검증한다. route 선택은 journal이 담당한다.

구현: `index_host_route.py`.

v4 route 분류는 store의 `store_meta`를 `mode=ro`와 `query_only`로 읽는다. commit 도중 죽은 writer가 남긴 hot rollback journal 때문에 `SQLITE_READONLY_ROLLBACK`이 나면 그때만 `mode=rw`(`query_only` 유지)로 다시 열어 SQLite가 journal을 되돌리게 한다. 평상시 분류는 쓰지 않는다.
