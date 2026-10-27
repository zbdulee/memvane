"""로드맵 3.8 — 건전하게 백필 가능한 카드에만 `sourceRevisions` 를 채운다.

hook 이 아니다(그래서 `core/` 가 아니라 여기 있다). 판정·직렬화·쓰기는 전부 `core/` 의
기존 SSOT 를 부른다 — 이 저장소는 읽기/쓰기 규칙이 갈려 두 번 깨졌다(카드 731장 중
8장 title 오염).

대상: `status: verified` + `createdBy: qmd-auto-context` + `sourceRevisions` 없음
      + 단일 파일 소스 + `generated-manifest.jsonl` 에 컴파일 시각(`ts`) 이 있는 카드.
건전 백필 가능: **내용 대조**로 판정한다 — 컴파일 시점에 이미 존재했음이 증명되는
      mainline(`--first-parent`) 앵커 커밋에서의 원문 blob == HEAD 의 blob == 워킹 트리의 blob.

**이 판정의 범위 — 어느 연산에 대해 참인가.** 시각 추론은 "어느 트리와 비교할지"를 고르는
데만 쓰고 "바뀌지 않았다"는 blob OID 로 증명한다. 그래서 merge 는 더 이상 구멍이 아니다
(`--no-ff` merge 로 들어온 내용은 base blob ≠ HEAD blob 으로 잡힌다). 남는 구멍은 둘이고
**둘 다 git 메타데이터만으로는 증명 불가**다:
  - **컴파일 시점에 워킹 트리가 dirty 였고 그 변경이 나중에 discard 된 경우.** 카드는
    커밋되지 않은 본문을 읽었는데 지금은 그 흔적이 어디에도 없어 구분할 수 없다.
  - **author·committer 두 날짜가 모두 컴파일 이후로 백데이트된 커밋.** 앵커 자격은
    `max(author, committer) <= compiledAt` 인데 두 날짜를 함께 조작하면(import·
    `filter-branch`·양쪽 `GIT_*_DATE` 명시) 그 커밋이 자격을 얻는다. 한쪽만 되돌려진
    경우는 `mainline_anchors` 가 막지만 양쪽이면 git 이 가진 시각 증거가 전부 거짓이라
    남길 수 있는 것이 없다.
  - 버전 관리 밖의 원문(ignore·git 아닌 저장소)·컴파일 시점 mainline 에 없던 파일은
    대조할 트리가 없어 판정 대상이 아니다(`no_git_history`/`no_base_commit`/
    `source_absent_at_compile` → `undecidable`).
  - 찍는 sha256 은 **이 스크립트가 읽은 바이트**의 것이다: `snapshot_bytes` 가 낸 같은
    바이트로 워킹 트리 blob 을 계산하므로 클린 검사와 해시 사이에 창이 없다. 그 뒤
    원문이 바뀌면 recall 의 freshness 검사가 stale 로 잡는다 — 그것이 정상 경로다.
  - 카드 mtime 은 앵커가 아니다(verify 스탬프·dedup 병합으로도 갱신된다). 그 프록시는
    415장을 후보로 잡지만 git 검증은 그 1/10 규모다.
`status` 는 건드리지 않는다. 이미 `verified` 인 카드에 provenance 만 채운다.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "core"))

import recall as qmd_recall  # noqa: E402
import wiki_compile as wc  # noqa: E402
import wiki_freshness as wf  # noqa: E402
import wiki_source_missing as wsm  # noqa: E402

# 백필 대상 프로젝트 기본 목록.
DEFAULT_PROJECTS = [
    "service-engineering", "ktlo-check", "rccar", "ai-proxy",
    "qmd-auto-context", "novela", "axiom",
]
META_CARDS = {"index.md", "log.md", "SCHEMA.md"}

BACKFILLABLE = "backfillable"
SOURCE_NEWER = "source_newer"
SOURCE_MISSING = "source_missing"
UNDECIDABLE = "undecidable"
DIRTY = "dirty"
CLASSES = (BACKFILLABLE, SOURCE_NEWER, SOURCE_MISSING, UNDECIDABLE, DIRTY)


def git_out(root: Path, args: list, stdin: bytes = None, strip: str = "all"):
    """git 한 번. 실패는 None 이다(사유별 분기는 호출부가 한다).

    `strip="newline"` 은 **후행 개행만** 벗긴다. 경로를 돌려주는 조회
    (`rev-parse --show-prefix`)에 `.strip()` 을 쓰면 공백으로 끝나는 디렉터리 이름이
    조용히 다른 경로가 되고(POSIX 는 허용한다) 그 접두로 tree 를 조회하면 엉뚱한
    파일이나 미존재를 본다.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root)] + list(args),
            input=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    text = done.stdout.decode("utf-8", "replace")
    if strip == "newline":
        return text[:-1] if text.endswith("\n") else text
    return text.strip()


def parse_iso(value) -> datetime | None:
    """ISO 시각 → aware datetime. **문자열 비교로 대신하지 말 것.**

    매니페스트 `ts` 는 UTC(`Z`/`+00:00`)인 것도 로컬 오프셋(`+09:00`)인 것도 있다.
    문자열 순서와 시간 순서는 실제로 어긋난다 — `2026-01-01T00:30:00+09:00`
    (= 전날 15:30Z)은 `2026-01-01T00:00:00Z` 보다 **이전**이지만 문자열로는 뒤에 온다.
    그 자리를 문자열로 비교하면 낡은 카드에 신선 배지를 다는 쪽으로 틀린다.
    커밋 시각과의 비교는 여기서 얻은 aware datetime 을 `pick_anchor` 가 epoch 정수로
    바꿔서 한다(git 은 `%at`/`%ct` 로 epoch 를 직접 준다) — 두 표현이 만나는 자리를
    한 곳으로 좁혀 그 클래스를 구조적으로 없앤다.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # naive 시각은 어느 오프셋인지 모른다 — 추정하지 않고 판정 불가로 보낸다.
        return None
    return parsed


def repo_path_prefix(root: Path):
    """`<rev>:<경로>` 조회에 붙일 저장소 top-level 상대 접두. 실패·예상 밖 값은 None.

    **`git -C <root> rev-parse HEAD:<rel>` 의 `<rel>` 은 cwd 가 아니라 저장소
    top-level 기준이다.** 프로젝트 루트가 저장소 하위면 접두 없는 조회가 같은 이름의
    **상위 저장소 파일**을 낸다(재현: 상위 `docs/a.md`=X, 하위 `project/docs/a.md`=Y →
    `git -C project rev-parse HEAD:docs/a.md` 는 X 의 blob 을 돌려준다). 그 blob 으로
    대조하면 카드가 읽은 적 없는 파일이 신뢰의 근거가 되고, 두 파일이 우연히 같으면
    거짓 통과, 다르면 거짓 `source_newer` 다 — 어느 쪽도 판정이 아니다.

    접두를 못 얻거나 모양이 예상과 다르면 **찍지 않는다**(fail-closed, 호출부의
    `repo_prefix_unresolved`). 잘못된 접두는 조용히 다른 파일을 증거로 삼는 쪽으로
    틀리므로 "모르면 안 찍는다"가 유일하게 안전한 기본값이다.
    """
    prefix = git_out(root, ["rev-parse", "--show-prefix"], strip="newline")
    if prefix is None:
        return None
    if prefix == "":
        # 프로젝트 루트 == 저장소 top-level. 라이브 4프로젝트가 전부 이 경우다.
        return ""
    if prefix.startswith("/") or not prefix.endswith("/"):
        return None
    if ".." in prefix.split("/"):
        return None
    return prefix


def mainline_anchors(root: Path):
    """`HEAD` 의 first-parent 조상 `(sha, 앵커 시각 epoch)` 목록. 최신 → 과거. 실패는 None.

    **앵커 시각은 `max(author date, committer date)` 다. `rev-list --before` 를 쓰면
    안 된다** — 그것은 committer date 만 보므로, author 는 컴파일 이후인데 committer 가
    과거로 되돌려진 커밋(import·`filter-branch`·`GIT_COMMITTER_DATE` 명시)이 앵커로
    뽑힌다. 그 커밋의 blob 은 대개 HEAD blob 과 같아 대조를 **통과**하고, 결과는 그
    내용으로 컴파일된 적 없는 카드에 신뢰를 주는 것이다(재현: base X at=ct=01-01,
    later Y at=08-03·ct=01-01, 카드 컴파일 08-02 → `--before` 는 later 를 앵커로 잡아
    통과하고 `max` 는 base 를 잡아 blob 이 달라 거부한다).

    "컴파일 시점에 이미 존재했다"를 주장하려면 **두 날짜 모두** 그 이전이어야 한다.
    한쪽이라도 컴파일 이후면 그 커밋은 앵커 자격이 없다.
    """
    out = git_out(root, ["log", "--first-parent", "--format=%H %at %ct", "HEAD"])
    if out is None:
        return None
    anchors = []
    for line in out.splitlines():
        # 읽을 수 없는 줄은 **후보에서 뺀다**(앵커 자격을 주지 않는다). 후보가 줄면 더
        # 오래된 앵커가 뽑히고 그것은 원문이 더 긴 구간 동안 그대로였음을 요구하므로,
        # 이 fail 방향은 항상 보수적이다.
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            stamp = max(int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        anchors.append((parts[0], stamp))
    return anchors


def pick_anchor(anchors: list, compiled_at: datetime):
    """컴파일 시점에 이미 존재한 것이 증명되는 가장 최근 mainline 커밋. 없으면 None."""
    # **epoch 정수로 비교한다.** ISO 문자열 비교는 이 저장소에서 이미 한 번 크게 틀렸고
    # (git `+09:00` vs 매니페스트 `Z`) 편향이 한 방향이라 세 번 재현해도 세 번 틀렸다.
    # `int()` 는 양수 epoch 를 내림하므로 컷오프가 앞으로 가고, 그 역시 보수적이다.
    cutoff = int(compiled_at.timestamp())
    for sha, stamp in anchors:
        if stamp <= cutoff:
            return sha
    return None


def git_context(root: Path) -> dict:
    """프로젝트 루트당 **한 번만** 부르는 git 조회 묶음.

    두 값 모두 카드와 무관한데 예전엔 카드마다 `rev-list` 를 실행했다 —
    service-engineering 은 대상이 1000장 규모라 그것이 이 스크립트의 지배적 비용이다.
    """
    return {"prefix": repo_path_prefix(root), "anchors": mainline_anchors(root)}


def latest_manifest_records(root: Path) -> dict:
    """targetPath → 가장 최신 created/updated 레코드(컴파일 시각의 앵커)."""
    latest = {}
    manifest = root / ".auto-context/compile/generated-manifest.jsonl"
    if not manifest.is_file():
        return latest
    try:
        handle = manifest.open(encoding="utf-8", errors="replace")
    except OSError:
        return latest
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            target = record.get("targetPath")
            stamp = parse_iso(record.get("ts"))
            if not isinstance(target, str) or stamp is None:
                continue
            if record.get("action") not in ("created", "updated"):
                continue
            previous = latest.get(target)
            if previous is None or stamp > previous[0]:
                latest[target] = (stamp, record)
    return latest


def manifest_file_sources(record: dict) -> list:
    """레코드의 파일 소스만. `kind == "file"` 이 아닌 출처는 판정 대상이 아니다."""
    sources = record.get("sources")
    if not isinstance(sources, list):
        return []
    out = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != qmd_recall.SOURCE_KIND_FILE:
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            out.append(item)
    return out


def declares_revisions(block: str) -> bool:
    """frontmatter 에 `sourceRevisions` 키가 **구조적으로** 있는가.

    파싱 결과로 판정하면 안 된다 — `recall.frontmatter_source_revisions` 는 깨진 블록도
    "증거 없음"(`[]`)으로 내므로, 그것을 근거로 쓰면 이미 헤더가 있는 카드에 두 번째
    헤더를 붙인다. 같은 파서가 중복 선언을 거부하므로 카드만 망가지고 신뢰는 얻지
    못한다. 인구조사 스크립트는 쓰지 않으므로 그 구분이 필요 없었다.
    """
    sections = wc._frontmatter_sections(block)
    if sections is None:
        # 파싱 불가한 frontmatter — 헤더 유무를 단정할 수 없으므로 "있다"로 본다.
        return True
    return any(key == "sourceRevisions" for key, _ in sections)


def declared_source_paths(block: str):
    """카드 frontmatter 의 `sources` → (파일 경로 집합, 확정 가능한가).

    **두 번째 값이 요점이다.** "빈 집합"과 "읽을 수 없다"를 뭉치면 미지원 표기(block
    mapping·여러 줄 flow)를 쓴 카드에서 집합이 비고, `if declared and ...` 형태의 가드가
    통째로 우회된다 — 재현: 카드 `sources` 는 `docs/a.md`(block mapping), 매니페스트는
    `docs/b.md` → `applied=1`. 결과는 **자기모순 provenance 로 신뢰를 얻은 카드**다
    (freshness 는 b 만 보고, 주입 `↳` 는 검증되지 않은 a 를 모델에 준다).
    이 저장소의 `COLLECTION_ROLE_INVALID` 와 같은 규칙이다: "키 없음"과 "키 있음 + 값
    미지"를 구분하고 후자는 fail-closed.
    """
    # 표기 판정은 core SSOT(`wiki_source_missing.declared_source_records`) 하나다 —
    # SessionStart 수명 주기의 레거시 카드 백필도 같은 판정을 쓴다.
    records, state = wsm.declared_source_records(block)
    if state != wsm.SOURCES_OK:
        return set(), False
    return {record["path"] for record in records
            if record.get("kind") == qmd_recall.SOURCE_KIND_FILE}, True


def compare_source_blobs(root: Path, source_rel: str, data: bytes,
                         compiled_at: datetime, gitctx: dict):
    """앵커 blob == HEAD blob == 워킹 트리 blob 3자 대조 → `(class, reason)`.

    **이 판정의 SSOT 는 이 함수 하나다.** 진단(`--recheck` 류 드라이버)이 같은 판정을
    손으로 베끼면 두 벌이 갈리고, 그 클래스가 이 저장소에서 가장 자주 반복됐다
    (읽기/쓰기 규칙이 갈려 카드 731장 중 8장 title 오염, 판정 두 벌로 census 와
    백필이 갈림). 호출부는 여기 결과를 그대로 쓴다.

    **어떤 경로도 파일을 쓰지 않는다** — `hash-object` 는 `-w` 가 없어 해시만 내고
    나머지는 `rev-parse`/`log` 조회다. 그래서 라이브 저장소에 안전하게 돌 수 있다.
    """
    prefix = gitctx.get("prefix")
    if prefix is None:
        # 접두를 모르면 `<rev>:<경로>` 가 어느 저장소의 어느 파일인지 모른다
        # (`repo_path_prefix` 참조). "모르면 안 찍는다".
        return UNDECIDABLE, "repo_prefix_unresolved"
    tree_rel = prefix + source_rel

    head_blob = git_out(root, ["rev-parse", "--verify", "HEAD:" + tree_rel])
    if not head_blob:
        return UNDECIDABLE, "no_git_history"
    # 클린 판정은 porcelain 파싱이 아니라 **찍을 바로 그 바이트**의 blob 해시로 한다.
    # 한 번의 읽기에서 클린 검사와 sha256 이 함께 나오므로 그 둘 사이에 원문이 바뀌는
    # 창이 없다. `--path` 를 주어 저장소의 clean filter·해시 알고리즘을 git 이 적용한다.
    # (porcelain 은 rename·인용 경로에서 파싱이 깨지고, 검사와 스냅샷이 두 읽기로 갈린다.)
    # `--path` 에는 저장소 접두를 붙이지 **않는다** — git 이 `-C` 로 준 cwd 기준으로
    # 스스로 접두를 붙인다(실측: `project/docs/*` 와 `docs/*` 에 다른 gitattributes 를
    # 준 저장소에서 `-C project --path docs/a.md` 의 blob 이 top-level 기준
    # `--path project/docs/a.md` 와 정확히 일치했다). 우리가 또 붙이면 이중 접두다.
    # 접두는 tree 조회(`<rev>:<경로>`) 전용이다.
    working_blob = git_out(root, ["hash-object", "--stdin", "--path", source_rel],
                           stdin=data)
    if not working_blob:
        return UNDECIDABLE, "hash_object_failed"
    if working_blob != head_blob:
        return DIRTY, "working_tree_differs_from_head"

    # **"바뀌지 않았다"는 내용으로 증명한다. 시각 비교는 어느 트리를 볼지 고르는 데만 쓴다.**
    # `log -1 --format=%cI -- <path>` 를 쓰면 안 된다 — pathspec 이력 단순화가 **feat 브랜치
    # 커밋**을 보고하므로, `--no-ff` merge 로 새 내용이 트리에 들어와도 브랜치 커밋 날짜가
    # 컴파일보다 앞서면 "원문 그대로"로 읽힌다(재현: base 1/1=X → feat 1/15=Y → merge 8/1,
    # 카드는 5/1 에 X 로 컴파일 → plain 은 1/15 를 보고해 건전 판정, 즉 X 카드에 sha256(Y)
    # 를 찍는다. 라이브 102 target 중 2건이 이 경로에 걸렸고 blob 이 우연히 같아 무해했다).
    # `--first-parent` 만 더하는 것도 시각 추론에 판정을 맡기는 것이라 쓰지 않는다.
    # 앵커 자격 규칙(committer date 단독 금지)의 근거는 `mainline_anchors` 에 있다.
    anchors = gitctx.get("anchors")
    if anchors is None:
        # `HEAD:<경로>` 를 이미 얻었으므로 HEAD 는 존재한다 — 사실상 도달하지 않는 방어
        # 분기다. 그래도 `no_base_commit`("자격 커밋이 없다")과 뭉치지 않는다.
        return UNDECIDABLE, "mainline_unreadable"
    base = pick_anchor(anchors, compiled_at)
    if not base:
        # 컴파일 시점에 존재한 것이 증명되는 mainline 커밋이 없다 — 대조할 트리가 없다.
        return UNDECIDABLE, "no_base_commit"
    base_blob = git_out(root, ["rev-parse", "--verify", base + ":" + tree_rel])
    if not base_blob:
        # 컴파일 시점의 mainline 에 그 파일이 없었다 — 카드가 무엇을 읽었는지 알 수 없다.
        return UNDECIDABLE, "source_absent_at_compile"
    if base_blob != head_blob:
        return SOURCE_NEWER, "content_changed_since_compile"
    return BACKFILLABLE, ""


def classify_card(root: Path, card: Path, rel_card: str, latest: dict,
                  allow_roots: list, gitctx: dict) -> dict:
    """카드 하나 → 분류 결과. 어떤 경로도 파일을 쓰지 않는다."""
    try:
        text = card.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"class": UNDECIDABLE, "reason": "card_unreadable"}
    match = wc.FRONTMATTER_RE.match(text)
    if match is None:
        return {"class": None, "reason": "no_frontmatter"}
    block = match.group(1)
    meta = qmd_recall.parse_frontmatter_scalars(block)
    if meta.get("status") != "verified" or meta.get("createdBy") != "qmd-auto-context":
        return {"class": None, "reason": "not_target"}
    if declares_revisions(block):
        if qmd_recall.frontmatter_source_revisions(block):
            return {"class": None, "reason": "already_provenanced"}
        # 헤더는 있는데 파싱되지 않는다 — 두 번째 헤더를 붙이지 않기 위해 손대지 않는다.
        return {"class": UNDECIDABLE, "reason": "invalid_existing_revisions"}

    entry = latest.get(rel_card)
    if entry is None:
        return {"class": UNDECIDABLE, "reason": "no_manifest_ts"}
    compiled_at, record = entry
    sources = manifest_file_sources(record)
    if len(sources) != 1:
        return {"class": UNDECIDABLE,
                "reason": "multi_source" if len(sources) > 1 else "no_file_source"}
    source = sources[0]
    source_rel = source["path"]

    # 존재 판정은 recall 의 SSOT 를 쓴다(주입·검수·소실 스캔과 같은 판정). 신뢰를
    # 부여하는 경로가 그보다 약한 검사를 쓰면 안 된다.
    resolved, why = qmd_recall.resolve_existing_source(source_rel, root, allow_roots)
    if resolved is None:
        # `outside_root`/`too_long` 은 "이 카드로는 판정 못 한다"이므로 소실과 섞지 않는다.
        if why == "missing":
            return {"class": SOURCE_MISSING, "reason": why, "source": source_rel}
        return {"class": UNDECIDABLE, "reason": why, "source": source_rel}

    snapshot = wf.snapshot_bytes(resolved)
    if snapshot is None:
        return {"class": UNDECIDABLE, "reason": "snapshot_failed", "source": source_rel}
    revision, data = snapshot

    verdict, reason = compare_source_blobs(root, source_rel, data,
                                           compiled_at, gitctx)
    if verdict != BACKFILLABLE:
        return {"class": verdict, "reason": reason, "source": source_rel}

    collection = source.get("collection")
    declared_paths, declared_ok = declared_source_paths(block)
    return {
        "class": BACKFILLABLE, "reason": "", "source": source_rel,
        "declared": sorted(declared_paths),
        "declaredOk": declared_ok,
        # collection 은 ts·path 를 준 **같은 매니페스트 레코드**에서 가져온다(사실 하나에
        # 앵커 하나). freshness·recall 은 이 값을 읽지 않고 그대로 나른다.
        "revision": {
            "kind": qmd_recall.SOURCE_KIND_FILE,
            "path": source_rel,
            "collection": collection if isinstance(collection, str) else "",
            "sha256": revision["sha256"],
            "size": revision["size"],
            "mtimeNs": revision["mtimeNs"],
        },
    }


def process_project(root: Path, apply: bool) -> dict:
    settings = root / ".auto-context/settings.json"
    if not settings.is_file():
        return {"skipped": "no_settings"}
    try:
        config = json.loads(settings.read_text(encoding="utf-8"))
    except Exception:
        return {"skipped": "unreadable_settings"}
    if not isinstance(config, dict):
        return {"skipped": "unreadable_settings"}
    wiki_root = wsm.wiki_root_of(root, config)
    if wiki_root is None or not wiki_root.is_dir():
        return {"skipped": "no_wiki"}
    allow_roots = wsm.allow_roots_of(config)
    latest = latest_manifest_records(root)
    # 카드마다 git 을 부르지 않는다(`git_context` docstring).
    gitctx = git_context(root)

    counts = collections.Counter()
    reasons = collections.Counter()
    applied = 0
    write_failed_cards = []
    source_mismatch = 0
    mismatch_cards = []
    sources_undeterminable = 0
    undeterminable_cards = []
    targets = []
    for path in sorted(wiki_root.rglob("*.md")):
        if path.name in META_CARDS or not path.is_file():
            continue
        rel_card = os.path.relpath(path, root)
        info = classify_card(root, path, rel_card, latest, allow_roots, gitctx)
        if info["class"] is None:
            continue
        counts[info["class"]] += 1
        if info["reason"]:
            reasons[info["class"] + ":" + info["reason"]] += 1
        if info["class"] != BACKFILLABLE:
            continue
        # 카드가 스스로 적은 원문 집합과 매니페스트가 준 경로가 어긋나면 찍지 않는다:
        # `sources` 는 파일 A 를 가리키는데 `sourceRevisions` 는 파일 B 의 해시인 카드는
        # 그 자체로 provenance 가 틀린 상태다(freshness 는 B 만 보고, 주입 `↳` 는 검증되지
        # 않은 A 를 모델에 준다). 사유는 둘로 **나눈다** — `sourceMismatch` 는 "다르다",
        # `sourcesUndeterminable` 은 "읽을 수 없다"다. 뭉치면 진단이 오염되고 후자에
        # fail-open 이 숨는다: `if declared and ...` 형태였을 때 미지원 표기(block mapping)
        # 카드에서 집합이 비어 이 검사가 통째로 우회됐다.
        # 라이브 4프로젝트 dry-run 실측: 불일치 3건(service-engineering 2 / ai-proxy 1),
        # 판정 불가 0건 — 0이어도 가드는 남긴다(우회 경로가 실재했다).
        # **판정은 `--apply` 안이 아니라 여기서 한다** — apply 전용이면 dry-run 이 실제로
        # 찍힐 수를 예고하지 못하고(분류 N 인데 적용 N-k), 그 숫자를 라이브에서 확인할 수
        # 있는 유일한 모드가 dry-run 이라 실측 자체가 불가능해진다.
        if not info.get("declaredOk"):
            sources_undeterminable += 1
            undeterminable_cards.append(rel_card)
            continue
        if info["source"] not in set(info.get("declared") or []):
            source_mismatch += 1
            mismatch_cards.append(rel_card)
            continue
        targets.append({"card": rel_card, "source": info["source"]})
        if not apply:
            continue
        # **신뢰 부여 쓰기 — CLAUDE.md "쓰기 반환값" 기준 1(작업 유실·상태 오염).**
        # 실패를 삼키면 카드는 그대로인데 "백필됨"으로 보고돼, 다음 사람이 그 카드의
        # recall 침묵을 설명할 수 없다(재실행으로 복구되지만 그 사실을 모른다).
        if wc.insert_source_revisions(path, [info["revision"]]):
            applied += 1
        else:
            # 개수만으로는 **어느 카드가** 안 찍혔는지 알 수 없다 — 경로를 남긴다.
            write_failed_cards.append(rel_card)
    return {
        "counts": {name: counts.get(name, 0) for name in CLASSES},
        "reasons": dict(reasons), "targets": targets,
        "applied": applied, "writeFailed": len(write_failed_cards),
        "writeFailedCards": write_failed_cards,
        "sourceMismatch": source_mismatch, "sourceMismatchCards": mismatch_cards,
        "sourcesUndeterminable": sources_undeterminable,
        "sourcesUndeterminableCards": undeterminable_cards,
        "applyMode": apply,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="로드맵 3.8 sourceRevisions 백필")
    parser.add_argument("projects", nargs="*", help="프로젝트 경로 또는 ~/work 하위 이름")
    parser.add_argument("--apply", action="store_true",
                        help="분류만 하지 않고 건전 백필 가능 카드에 실제로 찍는다")
    parser.add_argument("--dry-run", action="store_true",
                        help="기본값(명시해도 같다). --apply 가 없으면 항상 dry-run 이다")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 와 --dry-run 을 함께 줄 수 없다")

    names = args.projects or DEFAULT_PROJECTS
    report = {"applyMode": args.apply, "projects": {}}
    for name in names:
        candidate = Path(name).expanduser()
        root = candidate if candidate.is_dir() else Path.home() / "work" / name
        report["projects"][name] = process_project(root.resolve(), args.apply)

    # **쓰기 실패가 하나라도 있으면 non-zero 다.** 보고 자체는 정직해도(거짓 성공이
    # 아니다) exit 0 이면 호출자·CI 는 성공으로 읽는다. 그 run 의 신뢰 부여가 전량
    # 실패해도 조용히 넘어가는 것이 이 스크립트에서 가장 놓치기 쉬운 실패다.
    failed = sorted(
        card
        for result in report["projects"].values()
        for card in result.get("writeFailedCards", [])
    )
    report["writeFailedTotal"] = len(failed)
    exit_code = 1 if failed else 0

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return exit_code
    print("# wiki-revision-backfill (%s)" % ("APPLY" if args.apply else "dry-run"))
    for name, result in report["projects"].items():
        if "skipped" in result:
            print("%s: skip (%s)" % (name, result["skipped"]))
            continue
        counts = result["counts"]
        print("%s: 건전 백필 가능 %d  원문이 새로움 %d  원문 소실 %d  판정 불가 %d  dirty %d"
              % (name, counts[BACKFILLABLE], counts[SOURCE_NEWER],
                 counts[SOURCE_MISSING], counts[UNDECIDABLE], counts[DIRTY]))
        # 소스 판정 보류는 두 모드에서 모두 센다(dry-run 이 apply 를 예고해야 한다).
        if args.apply or result["sourceMismatch"] or result["sourcesUndeterminable"]:
            print("    찍을 대상 %d  적용 %d  쓰기 실패 %d  소스 불일치 %d  소스 판정 불가 %d"
                  % (len(result["targets"]), result["applied"], result["writeFailed"],
                     result["sourceMismatch"], result["sourcesUndeterminable"]))
        for label, cards in (("쓰기 실패", result["writeFailedCards"]),
                             ("소스 불일치", result["sourceMismatchCards"]),
                             ("소스 판정 불가", result["sourcesUndeterminableCards"])):
            for card in cards:
                print("      [%s] %s" % (label, card))
        if result["reasons"]:
            print("    사유: %s" % result["reasons"])
    if failed:
        print("쓰기 실패 %d건 — exit 1" % len(failed))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
