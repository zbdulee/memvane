# Git-blob candidate 저장 계약

`laya_blob_store.BlobStore`는 명시적인 private SQLite 파일에 위키 카드와
recipe/profile, 완전한 chunk/vector set, 원문 지문과 카드 attestation을 저장한다.
형식은 `memvane-git-blob-candidate-v1`, application id는 `0x4d564231`, schema는 `1`이다.
운영 hook·route·v6 reader와의 연결은 없으며 generation·FTS·migration·GC API는 제공하지 않는다.

## 소유권과 위키 소속

호출자는 PR1 repository/worktree 등록 JSON과 scope UUID를 제공한다.
저장소는 common Git dir의 경로·dev/ino와 프로젝트 상대 위치에 묶이며,
worktree는 checkout·Git admin 정체성과 실제 프로젝트 settings·wiki root를 묶는다.
등록 worktree들의 normalized settings policy는 같아야 한다. `skipPaths`도
policy에 포함되며 JSON 공백·키 순서·생략된 기본값과 두 설정 폴더 이름은 정규화한다.
각 worktree의 실제 settings 바이트 지문은 별도로 고정한다. 설정이나 root가 바뀌면
기존 binding을 자동 인계하지 않는다.

`ingest_card`는 등록 worktree와 **wiki 상대 Markdown 경로**만 받는다.
현재 settings에서 도출한 raw/wiki role, FD로 고정한 root·파일 정체성,
제한된 파일 읽기, 설정 지문과 checkout capture를 전후에 확인한다.
commit 직전 파일·설정을 다시 확인하며 변경이 관측되면 transaction을 취소한다.
이 관측은 파일시스템·Git의 원자적 snapshot을 보장하지 않는다.
절대 경로·traversal·dot 자식 경로·symlink·다중 hardlink·디렉터리는 거절한다.
wiki root의 `SCHEMA.md`·`index.md`·`log.md`는 제외하고 하위 디렉터리의 같은 이름은 카드다.

blob key는 읽은 정확한 UTF-8 바이트의 SHA256이다. decode/encode가 같아야 하며
BOM·CRLF·Unicode를 정규화하지 않는다. 같은 digest의 다른 바이트는 conflict다.
같은 내용의 재입력은 경로·worktree가 달라도 내용 행을 추가하지 않는다.
blob당 최초의 wiki admission 증거 하나를 보존하며 매 입력의 현재 소속은 다시 검증한다.
admission 이력은 view membership이나 다른 checkout의 신뢰 승계를 뜻하지 않는다.

DB의 leaf directory는 현재 UID의 `0700`, DB는 `0600`인 single-link 정규 파일이다.
경로 구성 요소의 symlink와 unsafe sidecar를 거절한다. 생성은 no-clobber이고
기존 v6/알 수 없는 파일은 읽기 전용 header 확인에서 거절한다.
열린 handle은 DB 파일 정체성·incarnation·scope·settings policy가 바뀌면 실패한다.

## 내부 API

| API | 결과 |
| --- | --- |
| `BlobStore.create(path, *, project_root, repository, worktree, scope_id)` | 새 candidate와 최초 worktree 등록 |
| `BlobStore.open(path, *, repository, scope_id)` | 동일한 저장소·scope의 candidate 열기 |
| `register_worktree(project_root, worktree)` | 같은 repository/project와 호환 policy의 worktree ID |
| `ingest_card(worktree_id, path)` | wiki 카드 blob SHA |
| `document(blob_sha)` | 저장한 wiki `{body,path,title}`. path는 `blobs/<sha>.md` |
| `register_recipe(spec)` / `register_profile(spec)` | canonical spec의 domain-separated SHA ID |
| `put_chunk_set(blob_sha, recipe_id, rows)` / `chunks(...)` | 완전한 chunk set의 digest / 행 |
| `vector_input_sha(blob_sha, recipe_id, profile_id, seq)` | 실제 provider 문서 입력 문자열의 UTF-8 SHA |
| `put_embedding_set(blob_sha, recipe_id, profile_id, rows)` / `vectors(...)` | 완전한 vector set의 digest / 행 |
| `put_fingerprint(spec)` / `fingerprint(id)` | 원문 **metadata** 지문 ID / 값 |
| `put_attestation(spec)` / `attestation(id)` | 개별 provenance를 묶은 trust digest / 값 |
| `verify()` | schema·FK·내용·recipe·set·지문·attestation의 전체 검증, 성공 시 `True` |
| `counts()` / `close()` | 테이블별 행 수 / 연결 종료 |

`BlobStoreError.reason`은 원문을 포함하지 않는 거절 코드다.
원문 body를 삽입하거나 읽는 공개 API는 없다. `document`가 반환하는 body는
위의 wiki admission을 통과한 카드다. 전체 `verify`는 관리 작업이며 검색 경로의 API가 아니다.

## recipe와 완전한 set

chunk recipe는 tokenizer/chunker artifact SHA, token window/overlap/search window,
`utf16-code-unit` offset, `qmd-markdown-v1` heading 규칙과 `blob-input-v1`을 묶는다.
title은 pinned `laya_qmd_chunking.mjs`의 ECMAScript 공백·줄 경계 규칙을 따른다.
heading 없는 카드의 title은 synthetic filename의 SHA여서 rename이 입력을 바꾸지 않는다.
문서 입력은 `title: {title || 'none'} | text: {chunk.text}`, query 입력은
`task: search result | query: {text}`다. 합성 fixture가 실제 JS 추출기와 title/input을 대조한다.

profile은 model ID뿐 아니라 model bytes SHA, embedding fingerprint,
dimension·cosine·문서/query input recipe를 묶는다. 등록된 spec은 candidate 선언이며
실제 tokenizer·모델 실행과의 호환성 증명을 대신하지 않는다.

chunk는 연속 seq와 증가 position을 가지며 text가 카드의 정확한 UTF-16 slice이고
text SHA가 일치해야 한다. vector는 해당 chunk/input SHA, 정확한 little-endian float32 길이,
유한 값과 nonzero norm을 검증한다. vector provenance는 blob·recipe·profile·seq·input SHA와
실제 vector 바이트를 커밋한다. set 조회도 count·행·digest를 다시 검증한다.

lookup·동일 replay 확인·삽입은 한 `BEGIN IMMEDIATE` transaction이다.
동일 replay는 no-op, 다른 replay는 conflict이며 기존 set을 덮어쓰지 않는다.
실패한 set의 부모·자식은 모두 rollback한다. recipe/profile이 다르면 별도 set이다.
모델 실행·중복 inference 조율·backfill은 이 API의 범위가 아니다.

## provenance와 검증의 의미

source fingerprint는 등록 worktree·branch·상대 path·SHA256·byte length·HEAD·tracked state·
binding state·scan UUID·관측 UTC ns를 커밋한다. 원문은 읽거나 저장하지 않는다.
값은 **호출자가 제공한 관측 주장**이며 이 API가 원문을 관측했다는 뜻이 아니다.
원문 변경은 새 지문을 만들며 기존 카드의 출처를 최신 지문으로 옮기지 않는다.

attestation은 blob·verification kind·engine·compiler policy·결과 digest와
순서 있는 fingerprint/span 목록을 묶는다. spans는 해당 원문 byte length 안의
UTF-8 byte locator이고 excerpt를 저장하지 않는다. 이 경계 검사는 검증된 인용을 뜻하지 않는다.
receipt는 version·verdict·result digest·prior trust만 허용하며 raw body와 transcript 필드는 거절한다.
같은 blob이 다른 source provenance를 가지면 별도의 attestation을 보존한다.

`compiled`는 observed source claim만 받으며 source 없는 candidate receipt도 표현한다.
`legacy_verified`는 `legacy_unbound`를 그대로 보존한다.
`rebound`는 동일 blob·engine·policy의 기존 trust를 참조하는 새 불변 증거다.
새 source는 observed·known HEAD·known tracked state여야 하고 기존 지문과
모든 path/SHA/length/spans가 같아야 한다. empty/unknown/mismatch는 거절한다.
기존 provenance와 receipt는 바뀌지 않는다.

SQLite는 PK/FK·형식·타입·불변성·no-REPLACE 구조를 보호하며 API와 `verify`가
hash·완전성·참조 의미를 검증한다. 내부 connection과 직접 SQL 쓰기는 지원 인터페이스가 아니다.
**trust digest와 candidate 검증 성공은 compiler 실행 사실·원문 일치·운영 권한의 증명이 아니다.**
운영 owner의 실제 증명·policy·publication 검증은 후속 통합이 소유한다.

## 유한 입력

| 대상 | 상한 |
| --- | --- |
| 카드 본문 / settings 읽기 | 4 MiB / 1 MiB |
| chunk set | 1,024행, 합계 UTF-8 text 16 MiB |
| vector dimension | 1–2,048, 행당 최대 8,192 bytes |
| recipe token window | 1–8,192, overlap은 window 미만 |
| attestation source / source별 spans | 64개 / 256개 |
| SQL busy | 2초 |
| Git capture | PR1의 유한 예산·버전 조건 |

schema·등록 정체성·수명 주기·운영 데이터의 자동 전환이나 삭제는 이 인터페이스에 없다.
