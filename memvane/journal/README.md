# 설치 journal

route를 분류·선택하고 선택된 store와 reader·writer 권한을 검증한다. writerFloor 미달 시 쓰기를 거절한다.

구현: `route_selection.py`, `preview_proof.py`, `notification_route.py`, `install_journal.py`, `journal_paths.py`, `journal_reader.py`, `reader_lease.py`, `reader_windows.py`, `writer_scope.py`.

`types.py`는 다른 도메인이 쓰는 writer 핸들 멤버(`WriterHandle`, 구현 `WriterToken`)와 reader 값 객체(`VerifiedEndpoint`, `ReaderSelection`)를 선언한다. 이 객체들은 journal만 발급한다.
