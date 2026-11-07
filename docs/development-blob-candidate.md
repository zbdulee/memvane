# Git-blob candidate 저장 계약

`laya_blob_store.BlobStore`는 명시적인 private SQLite 파일에 위키 카드와
recipe/profile, 완전한 chunk/vector set, 원문 지문과 카드 attestation을 저장한다.
형식은 `memvane-git-blob-candidate-v3`, application id는 `0x4d564231`, schema는 `3`다.
candidate v1/v2와 운영 v6 파일은 자동 변환하지 않고 읽기 전용 preflight에서 거절한다.
`BlobStore`는 내용·provenance API를 소유하고 `laya_view_store.ViewStore`가 같은 형식의
view generation·FTS·lexical reader를 제공한다. 세대 API는
[`development-view-candidate.md`](development-view-candidate.md)에 있다.
`BlobStore`에는 hook·route·reader 선택·migration·activation·GC API가 없다. 운영 연결은
호출자가 소유한다: v6 reader가 선택된 설치에서는 shadow migration 후보(`laya_shadow_migration`),
schema3 reader가 선택된 설치에서는 sync 게시(`laya_product_sync_worker`)와 recall·dedup retrieval
(`laya_journal_reader`·`laya_reader_dispatch`·`wiki_compile_retrieval`). 어느 store를
읽고 쓸 권한은 설치 journal의 schema3 endpoint record와 writer scope가 정한다.

## 소유권과 위키 소속

호출자는 PR1 repository/worktree 등록 JSON과 scope UUID를 제공한다.
저장소는 common Git dir의 경로·dev/ino와 프로젝트 상대 위치에 묶이며,
worktree는 checkout·Git admin 정체성, settings 정체성, wiki root 디렉터리 노드를 묶는다.
settings 정체성은 v6 route의 `settings_identity_digest`와 같은 범위다: 프로젝트 root와
도출한 collection마다 `{name, role, sourceRoot}`. 저장소 policy는 같은 행을 프로젝트
상대 경로(두 설정 폴더 이름은 정규화)로 다이제스트하므로 등록 worktree들의 policy는 같아야 한다.
settings 바이트, `skipPaths`와 그 밖의 옵션은 묶지 않는다. `skipPaths`는 쓰는 시점의
현재 settings에서 읽고 prepare가 세대 stage에 봉인한다. 정체성이나 wiki root가 바뀌면
기존 binding을 자동 인계하지 않는다.

`ingest_card`는 등록 worktree와 **wiki 상대 Markdown 경로**만 받는다.
현재 settings에서 도출한 raw/wiki role, FD로 고정한 root·파일 정체성,
제한된 파일 읽기, settings 정체성과 checkout capture를 전후에 확인한다.
commit 직전 파일·설정을 다시 확인하며 변경이 관측되면 transaction을 취소한다.
이 관측은 파일시스템·Git의 원자적 snapshot을 보장하지 않는다.
절대 경로·traversal·dot 자식 경로·symlink·다중 hardlink·디렉터리는 거절한다.
wiki root의 `SCHEMA.md`·`index.md`·`log.md`는 제외하고 하위 디렉터리의 같은 이름은 카드다.

`ingest_attested_card(worktree_id, path, *, decision, provenance_scan)`는 같은 admission에
카드 신뢰 판정을 더한다. `decision`은 호출자의 선택 route 결정이며 그 `projectRoot`·`scopeId`와
wiki collection이 이 store의 worktree·scope와 맞아야 한다. 저장소는 읽은 카드 바이트의
frontmatter를 recall의 신뢰 규칙(`legacy_card_evidence`, shadow migration import와 같은 함수)으로
직접 판정한다. 통과한 카드에는 각 `sourceRevisions` 항목을 `binding_state='legacy_unbound'`·
HEAD 없음·`tracked_state='unknown'` 지문으로 기록하고 `legacy_verified` attestation을 붙인다.
통과하지 못한 카드는 trust 없이 admission만 한다. 현재 HEAD를 과거 compile commit으로 쓰지 않으며
호출자가 trust 값을 넘기는 인자는 없다. `provenance_scan`은 지문의 scan UUID·관측 시각이다.

blob key는 읽은 정확한 UTF-8 바이트의 SHA256이다. decode/encode가 같아야 하며
BOM·CRLF·Unicode를 정규화하지 않는다. 같은 digest의 다른 바이트는 conflict다.
같은 내용의 재입력은 경로·worktree가 달라도 내용 행을 추가하지 않는다.
blob당 최초의 wiki admission 증거 하나를 보존하며 매 입력의 현재 소속은 다시 검증한다.
admission 이력은 view membership이나 다른 checkout의 신뢰 승계를 뜻하지 않는다.

DB의 leaf directory는 현재 UID의 `0700`, DB는 `0600`인 single-link 정규 파일이다.
경로 구성 요소의 symlink와 unsafe sidecar를 거절한다. 생성은 no-clobber이고
candidate v1/v2·기존 v6·알 수 없는 파일은 읽기 전용 header 확인에서 바이트 변경 없이 거절한다.
열린 handle은 DB 파일 정체성·incarnation·scope·settings policy가 바뀌면 실패한다.

## 내부 API

| API | 결과 |
| --- | --- |
| `BlobStore.create(path, *, project_root, repository, worktree, scope_id)` | 새 candidate와 최초 worktree 등록 |
| `BlobStore.open(path, *, repository, scope_id)` | 동일한 저장소·scope의 candidate 열기 |
| `register_worktree(project_root, worktree)` | 같은 repository/project와 호환 policy의 worktree ID |
| `ingest_card(worktree_id, path)` | wiki 카드 blob SHA |
| `ingest_attested_card(worktree_id, path, *, decision, provenance_scan)` | `{blob_sha, trust_digest}`. 신뢰 규칙을 통과하지 못하면 `trust_digest`는 null |
| `document(blob_sha)` | 저장한 wiki `{body,path,title}`. path는 `blobs/<sha>.md` |
| `register_recipe(spec)` / `register_profile(spec)` | canonical spec의 domain-separated SHA ID |
| `put_chunk_set(blob_sha, recipe_id, rows)` / `chunks(...)` | 완전한 chunk set의 digest / 행 |
| `vector_input_sha(blob_sha, recipe_id, profile_id, seq)` | 실제 provider 문서 입력 문자열의 UTF-8 SHA |
| `embedding_inputs(blob_sha, recipe_id, profile_id)` | 같은 입력 생성기를 사용하는 순서 있는 문자열 목록 |
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
모델 실행은 호출자가 제공하는 callback이 소유한다.

## Content 채우기와 재사용

`laya_blob_fill.ContentFiller(store)`는 content별 complete set을 재사용한다.
`ensure_content(blob_sha, recipe_id, profile_id, *, chunker, embedder, write_fence,
deadline_seconds=30)`의 결과는 `status`(`cache_hit` 또는 `filled`), 세 ID,
`set_digest`, `chunk_count`다. blob·recipe·profile은 같은 store에 등록되어 있어야 한다.
세 callback은 캐시가 있어도 callable이어야 한다. 최초 조회에서 완전한 캐시를 찾으면
count·입력 SHA·실제 vector 바이트와 provenance를 다시 확인하고 DB 쓰기·fence·callback·
lock 디렉터리 생성 없이 반환한다. 손상된 set은 캐시 누락으로 간주하거나 덮어쓰지 않는다.

캐시가 없으면 store incarnation·blob·recipe·profile별 비차단 `flock`을 획득한다.
private sibling `<DB 파일명>.content-locks`는 `0700`, lock은 현재 UID의
single-link `0600` 정규 파일이다. symlink를 따라가지 않으며 DB·parent·root·
이름이 가리키는 inode와 보유 FD를 재확인한다. 경합은 기다리지 않고 거절한다.
lock 파일은 지우지 않으며 TTL 인계·프로세스 신호를 사용하지 않는다.

`claim_content(blob_sha, recipe_id, profile_id)` context manager는 opaque claim을
반환한다. `validate_claim(claim)`은 성공 시 `None`을 반환하며 claim은 발급
filler/store·정확한 객체·보유 FD에 묶인다. context 종료 뒤 claim은 유효하지 않다.
claim은 DB lease 행을 만들지 않으며 FD는 callback에 전달하지 않는다.
이 협력 계약은 같은 UID의 임의 FD 조작·명시적 unlock·같은 inode 재개방에 대한
OS 보안 경계가 아니다. 프로세스 종료 시 kernel이 lock을 해제한다.

lock 획득 뒤 캐시를 다시 확인하며 이때 완전한 set이 있으면 DB 쓰기·fence·callback 없이
반환한다. 첫 candidate DB 변경 전에 필수 `write_fence()`를 한 번 호출한다.
lock 파일 생성은 그 전에 일어날 수 있다. `chunker(document, recipe)`는
`BlobStore.document()`의 `{body,path,title}`와 등록 recipe를 받고 기존
`{seq,position,text,text_sha}` 행 목록을 반환한다. 유효한 chunk set이 있으면 chunker를
건너뛴다. `embedder(inputs, profile)`는 정확한 입력 순서의 `N × dimension` 숫자 배열을
반환한다. filler가 seq·입력 SHA·little-endian float32 바이트를 구성한다.
행은 최대 1,024개, packed vector 합계는 최대 16 MiB다. bool·nonfinite·zero norm·
float32 overflow·개수·차원 불일치는 거절하고 완전한 vector set만 원자적으로 기록한다.
recipe/profile ID는 callback 구현이 선언과 일치한다는 증명이 아니다.
callback과 flock 획득 중 SQLite transaction을 유지하지 않는다.

deadline은 유한한 `0 < seconds <= 300`이며 callback 전후와 캐시 검증 후에도
확인한다. 라이브러리의 경과 시간 확인은 callback을 선점하지 못하므로 호출자가
외부 timeout을 제공한다. 중단 후 유효한 chunk set은 남을 수 있으며, 재시도나
crash 전후의 provider 실행을 exactly-once로 보장하지 않는다.
자동 재시도는 없다. lock 경합은 `content_claim_busy`, 잘못된 deadline은
`content_deadline_invalid`, 경과 시간 초과는 `content_fill_deadline`으로 거절한다.

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
candidate의 generation seal도 같은 구조적 commitment다. 운영 owner의 증명·policy·
publication 권한은 journal endpoint record와 sync owner의 writer scope
(`laya_writer_scope`·`laya_writer_open_plan`)가 확인한다.

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
