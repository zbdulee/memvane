# Vector arena

view store 옆의 파생 vector 파일(arena)을 읽고 scan한다. store는 vector 하나를 SQLite 행 하나로 두므로 한 view의 set을 읽으면 행마다 임의 page를 읽는다. arena는 같은 바이트를 append-only 파일 하나에 이어 두어 recall이 매핑해 순서대로 scan한다.

구현: `arena.py`(형식과 reader), `scan.py`(구간 묶기와 커널 scan), `writer.py`(sync owner의 writer), `types.py`(형식 번호·이름·상수·writer 상한).

- 위치: `<install state root>/vector-cache/`(디렉터리 0700, 파일 0600, `O_NOFOLLOW`). 파생물이라 지워도 되고 1.x 영속 계약 밖이다. `current.json`(활성 쌍과 정체성: 형식 번호·nonce·profile·recipe·dimension·dtype·store incarnation), `arena-<nonce>.vec`(헤더 한 페이지 + float32 × dimension 행), `arena-<nonce>.idx`(헤더 + 고정 길이 레코드 `(blob sha, first row, chunk count, set digest, vec crc, record crc)`).
- reader(`arena.open_arena`)는 lock을 쥐지 않고 쓰지 않는다. 디렉터리·파일이 없거나 안전하지 않거나 형식 번호·정체성·헤더가 다르면 "arena 없음"(None)이다. idx는 `record crc`가 틀린 레코드(찢어진 append)를 건너뛰고 같은 blob의 레코드는 마지막 것을 쓴다.
- set이 "덮였다"는 것은 마지막 유효 레코드가 store의 set digest와 chunk 수를 가리키고, 행 구간이 vec 안에 있고, `vec crc`가 매핑한 바이트와 맞는 것이다(`Arena.cover`). 그렇지 않은 set은 missing이고 호출자가 store에서 읽는다. 레코드는 있는데 맞지 않은 set은 inconsistent로도 센다.
- vec은 private 매핑(`ACCESS_COPY`)으로 연다. scan 커널의 버퍼가 쓰기 가능한 버퍼를 요구하기 때문이고 파일에는 아무것도 쓰이지 않는다.
- scan(`scan.scan`)은 덮인 set의 행 구간을 틈 256행(`GAP_ROWS`) 이하면 한 커널 호출로 묶는다(커널 호출 하나가 약 190행의 scan과 같다). 틈의 점수는 버린다. 커널이 바이트를 거절한 묶음(norm 0·비유한 값)은 set 단위로 다시 돌고, 혼자서도 거절되는 set은 호출자의 store 읽기로 넘긴다. deadline은 그대로 호출자에게 간다.
- writer(`writer.Writer`)는 설치의 sync owner 하나뿐이고 sync pass마다 하나를 연다. store를 읽지 않고 호출자가 주는 살아 있는 set(`{blob: (chunk 수, set digest)}`)과 `read(blob)`만 쓴다.
  - `start`: 디렉터리와 활성 쌍을 준비하고, 살아 있는 set의 레코드를 store의 digest·chunk 수·행 구간·`vec crc`로 대조하고(자가 대조), 필요하면 compaction하고, 레코드가 없는 set을 pass당 4,096 vector·2초 안에서 append한다(catch-up. store가 읽지 못한 set의 시도도 그 상한에 든다). `add`: 게시 직전에 그 세대가 보이게 할 set을 같은 크기의 따로 둔 상한 안에서 append한다.
  - 대조에서 어긋난 set은 store에서 다시 읽어 append한다(`repaired`). catch-up의 진행으로 세지 않는다. 행 구간은 맞는데 바이트가 `vec crc`와 다른 레코드가 있으면(전원 차단이 남긴 0행 등. 살아 있는 set이든 죽은 set이든) 그 pass에서 compaction해 그 행을 set 사이에 남기지 않는다.
  - append는 vec 행을 쓰고 fsync한 뒤 idx 레코드를 쓰고 fsync한다. 찢어진 꼬리는 다음 append 전에 행(vec)·레코드(idx) 경계까지 0으로 채운다. vec은 자르지 않는다.
  - compaction: 죽은 행이 절반 이상이거나 64MiB 이상이면 대조를 통과한 set만 `vec crc`를 다시 확인해 새 nonce 쌍으로 복사하고 `current.json`을 원자적 rename으로 바꾼 뒤 옛 파일을 지운다(매핑한 recall은 끝까지 읽는다). 복사에 필요한 바이트를 디스크 빈 공간에 먼저 묻고 모자라면 그 pass는 `skipped`(`write_unavailable`)다(quota는 묻지 않는다: 복사본은 바꿔 치울 쌍보다 크지 않다). 활성 쌍이 되지 못한 새 쌍은 닫고 지운다(실패한 pass는 파일도 descriptor도 남기지 않는다). 교체 뒤에 실패한 pass가 남긴 옛 쌍은 다음 pass가 arena를 열 때 지운다.
  - 전체를 새로 만드는 경우는 `current.json`을 읽을 수 없거나 정체성·헤더가 다르거나 형식 번호가 낮을 때뿐이다. 형식 번호가 높은 arena는 건드리지 않는다(`skipped`·`format_newer`).
  - quota: 설치당 max(살아 있는 vector 바이트 × 2, 256MiB), 모든 설치의 합(install state root의 형제 `*/vector-cache/`) 2GiB. 합을 넘으면 `current.json`의 mtime(writer가 pass마다 갱신하는 마지막 사용 시각)이 14일 넘게 오래된 다른 설치의 arena를 오래된 순으로 지우고, 그래도 넘으면 append하지 않는다(`skipped`·`quota`).
  - 남은 set이 있는데 catch-up이 한 행도 못 한 pass는 `skipped`와 사유(`quota`·`format_newer`·`write_unavailable`·`set_unreadable`)를 낸다. writer의 어떤 실패도 게시를 막지 않는다.
- 이 도메인은 store와 sync를 부르지 않는다. recall의 reader(store)와 sync owner의 writer가 `api`로 쓴다.
