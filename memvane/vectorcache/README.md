# Vector arena

view store 옆의 파생 vector 파일(arena)을 읽고 scan한다. store는 vector 하나를 SQLite 행 하나로 두므로 한 view의 set을 읽으면 행마다 임의 page를 읽는다. arena는 같은 바이트를 append-only 파일 하나에 이어 두어 recall이 매핑해 순서대로 scan한다.

구현: `arena.py`(형식과 reader), `scan.py`(구간 묶기와 커널 scan), `types.py`(형식 번호·이름·상수).

- 위치: `<install state root>/vector-cache/`(디렉터리 0700, 파일 0600, `O_NOFOLLOW`). 파생물이라 지워도 되고 1.x 영속 계약 밖이다. `current.json`(활성 쌍과 정체성: 형식 번호·nonce·profile·recipe·dimension·dtype·store incarnation), `arena-<nonce>.vec`(헤더 한 페이지 + float32 × dimension 행), `arena-<nonce>.idx`(헤더 + 고정 길이 레코드 `(blob sha, first row, chunk count, set digest, vec crc, record crc)`).
- reader(`arena.open_arena`)는 lock을 쥐지 않고 쓰지 않는다. 디렉터리·파일이 없거나 안전하지 않거나 형식 번호·정체성·헤더가 다르면 "arena 없음"(None)이다. idx는 `record crc`가 맞는 접두까지만 믿고 같은 blob의 레코드는 마지막 것을 쓴다.
- set이 "덮였다"는 것은 마지막 유효 레코드가 store의 set digest와 chunk 수를 가리키고, 행 구간이 vec 안에 있고, `vec crc`가 매핑한 바이트와 맞는 것이다(`Arena.cover`). 그렇지 않은 set은 missing이고 호출자가 store에서 읽는다. 레코드는 있는데 맞지 않은 set은 inconsistent로도 센다.
- vec은 private 매핑(`ACCESS_COPY`)으로 연다. scan 커널의 버퍼가 쓰기 가능한 버퍼를 요구하기 때문이고 파일에는 아무것도 쓰이지 않는다.
- scan(`scan.scan`)은 덮인 set의 행 구간을 틈 256행(`GAP_ROWS`) 이하면 한 커널 호출로 묶는다(커널 호출 하나가 약 190행의 scan과 같다). 틈의 점수는 버린다. 커널이 바이트를 거절한 묶음(norm 0·비유한 값)은 set 단위로 다시 돌고, 혼자서도 거절되는 set은 호출자의 store 읽기로 넘긴다. deadline은 그대로 호출자에게 간다.
- 이 도메인은 store와 sync를 부르지 않는다. recall의 reader(store)와 sync owner의 writer가 `api`로 쓴다.
