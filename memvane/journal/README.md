# 설치 journal

route를 분류·선택하고 선택된 store와 reader·writer 권한을 검증한다. writerFloor 미달 시 쓰기를 거절한다.

구현: `route_selection.py`, `preview_proof.py`, `notification_route.py`, `install_journal.py`, `journal_paths.py`, `journal_reader.py`, `reader_lease.py`, `reader_windows.py`, `writer_scope.py`.
