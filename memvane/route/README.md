# Route

프로젝트 route 기록과 settings 정체성·색인 루트를 검증한다. route 선택은 journal이 담당한다.

구현: `index_host_route.py`.

v4 route 분류는 store의 `store_meta`를 `mode=ro`와 `query_only`로 읽는다. commit 도중 죽은 writer가 남긴 hot rollback journal 때문에 `SQLITE_READONLY_ROLLBACK`이 나면 그때만 `mode=rw`(`query_only` 유지)로 다시 열어 SQLite가 journal을 되돌리게 한다. 평상시 분류는 쓰지 않는다.

알 수 없는 schema id의 route나 selection marker, 그리고 `stateFormat`이 다른 메이저인 v4 route는 `blocked`/`route_major_mismatch`(`majorMismatch`: 더 높은 메이저면 `newer`, 그 밖은 `other`, `recallQuiet`)로 분류한다. hook 분류는 이것을 모든 action에서 `legacy`로 출력해 컨텍스트 없이 exit 0으로 끝내고, SessionStart 안내는 메이저 불일치 한 줄을 낸다. writer·setup은 선택 증명에서 거절한다.
