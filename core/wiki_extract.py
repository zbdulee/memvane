#!/usr/bin/env python3
"""Compact-context extractor for qmd wiki compile.

This command is the safe bridge between a host/manual compact summary and
core/wiki_compile.py. It accepts already-bounded durable conclusions, converts
them to deterministic candidate JSON, and delegates all write/lint/governance to
wiki_compile.py. It intentionally does not accept or persist raw transcripts.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import wiki_card
import resolve_paths as qmd_resolve_paths
import wiki_compile as wc
import wiki_freshness

TYPE_DIRS = {
    "concept": "concepts",
    "entity": "entities",
    "decision": "decisions",
    "session": "sessions",
    "comparison": "comparisons",
    "query": "queries",
    "character": "characters",
    "world-rule": "world",
    "timeline": "timeline",
    "plot-decision": "plot",
    "style": "style",
}
TRANSCRIPT_RE = re.compile(r"(?im)^\s*(user|assistant|system|human|ai)\s*:")

# 검증에 쓰이는 파일 소스 수는 `wiki_verify_worker.MAX_SOURCES`가 상한이고
# `authoritativeSources`는 그 상한으로 **잘리지 않는다**(verifier가 반드시 읽어야 하는
# 근거이기 때문이다). 즉 여기서 내는 목록 길이가 곧 유료 검수 호출의 읽기 폭이 되므로 수동
# 경로에서도 같은 폭으로 유계여야 한다. 두 값이 갈리면 안 되고 test/wiki-extract가
# 코드에서 유도해 단정한다. **상한이 걸리는 대상은 중복 제거 뒤 서로 다른 파일 수**다 —
# 항목 수에 걸면 같은 파일의 별칭이 폭을 소진한다.
MAX_PROVENANCE_FILE_SOURCES = 3
# 조사할 **항목** 수는 상한의 2배로 따로 유계다(`wiki_card.collect_source_paths`가 카드당 조사
# 예산을 같은 방식으로 두는 것과 같은 이유 — 목록은 caller 가 주므로 개수 보장이 없고,
# 중복 제거는 항목을 다 열어 본 뒤에야 확정된다). 별칭 중복은 이 창 안에서 접히고, 창을
# 넘기면 기존 ">상한" 과 같이 전무다.
MAX_PROVENANCE_SOURCE_SCAN = MAX_PROVENANCE_FILE_SOURCES * 2


def compiler_provenance(cwd: str, sources: Any) -> tuple[list[dict], list[dict]]:
    """수동 경로에서 컴파일러가 직접 읽은 파일 소스의 `(authoritative, revisions)`.

    **사실은 컴파일러가 정하고 caller 는 의견만 낸다.** 자동 훅 경로는
    `wiki_compile_worker`가 자기가 읽은 파일의 스냅샷을 candidate 에 대입해 provenance 를
    소유한다(모델 값 미채택). 수동 경로에는 큐도 worker 도 없어 provenance 가 아예 비고,
    그러면 그 카드는 `wiki_card.is_auto_trusted_card`(status + createdBy + **비어 있지 않은**
    `sourceRevisions`)를 영구히 통과하지 못해 **recall 에 절대 주입되지 않는다**. 그래서
    같은 규칙을 여기서도 적용한다 — 파일 소스를 **컴파일러가 직접 읽어** 스냅샷한다.

    수동 경로에서 caller 가 정하는 것은 **어느 파일인가**뿐이다(큐가 없으므로 불가피하다).
    그 선택이 틀리면 기계 검수가 카드 주장 vs 그 원문을 대조해 걸러낸다 — 여기서 만드는
    사실은 "컴파일러가 그 파일을 열어 이 내용을 봤다"이며 그 이상을 주장하지 않는다.

    규칙 4개를 지킨다:
    - **두 필드는 한 번의 읽기에서 함께 나온다**(`snapshot_bytes`는 fstat 를 같은 fd 에서
      두 번 떠 안정 이미지만 돌려준다). 소스 집합을 두 벌로 만들면 "검증은 A 를 보는데
      provenance 는 B 를 가리킨다"가 된다.
    - **`kind: file` 만**(`wiki_card.SOURCE_KIND_FILE`). url/slack 등 비파일 출처는 스냅샷
      대상이 아니고, 그런 항목만 있는 카드는 기존대로 provenance 없이 나간다.
    - **전부 아니면 전무.** 나열된 파일 소스 중 하나라도 스냅샷하지 못하면 아무것도 내지
      않는다 — `yaml_scalars.dump_source_revisions`가 형제 하나가 깨지면 목록 전체를
      버리는 것과 같은 이유로, 부분 목록이 완결된 근거처럼 보이면 안 된다.
    - **경로는 project root 상대 POSIX.** `recall.card_freshness`(경유
      `resolve_existing_source`)와 `wiki_verify_worker.load_sources`(`root / rel`)가 그
      base 로 되읽는다. root 밖(allowRoots) 파일은 후자가 어차피 거부하므로 스냅샷하지
      않는다 — 여기서 통과시키면 provenance 는 있는데 검수는 못 읽는 카드가 된다.
    - **중복 제거가 상한보다 먼저다.** 상한은 항목 수가 아니라 **서로 다른 파일 수**에
      걸린다. 항목 수에 먼저 걸면 같은 파일의 별칭 4개가 provenance 를 통째로 없애고
      (= 그 카드는 영구히 recall 에 나오지 않는다) 별칭 2개는 같은 파일을 두 번 세어
      검수 읽기 폭을 헛되게 소진한다.
    """
    if not isinstance(sources, list):
        return [], []
    # root 해석은 실제로 카드를 쓰는 쪽(`wiki_compile.project_paths`)과 **같은 함수**여야
    # 한다 — 갈리면 여기서 만든 root 상대 경로가 카드 안에서 다른 파일을 가리킨다.
    try:
        root, project_config = wc.project_paths(cwd)
    except (OSError, ValueError, KeyError):
        return [], []
    allow_roots = qmd_resolve_paths.allowed_roots(project_config or {})
    file_sources = [
        item for item in sources
        if isinstance(item, dict) and item.get("kind") == wiki_card.SOURCE_KIND_FILE
    ]
    if not file_sources or len(file_sources) > MAX_PROVENANCE_SOURCE_SCAN:
        return [], []
    authoritative: list[dict] = []
    revisions: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for item in file_sources:
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return [], []
        # 존재·격리 판정 SSOT. base 는 cwd 가 아니라 project root 다.
        resolved, _reason = wiki_card.resolve_existing_source(raw_path, root, allow_roots)
        if resolved is None:
            return [], []
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            # allowRoots 로 root 밖을 가리킨 소스 — 검수가 읽지 못하므로 근거가 못 된다.
            return [], []
        try:
            info = resolved.stat()
        except OSError:
            return [], []
        # **같은 물리 파일의 별칭을 접는다.** 판정은 `(st_dev, st_ino)` 동치이고 그것은
        # `os.path.samestat`(= `os.path.samefile`)이 보는 값과 같다 — `core/update.sh`의
        # 등록 경로 대조가 같은 근거를 쓴다. 문자열 정규화로 흉내내지 말 것: 대소문자 무시
        # 여부는 마운트마다 다르고 한글은 NFC↔NFD 로 갈리며, 하드링크는 어느 정규화로도
        # 같아지지 않는다. 양쪽 존재는 `resolve_existing_source`가 이미 확인했으므로
        # `samefile`이 성립하고 realpath 문자열 폴백이 필요없다.
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            # 별칭이 겹치면 **먼저 나온 rel** 을 쓴다(frontmatter 중복 키의 first-wins 와
            # 같은 규약). 뒤 항목은 같은 파일이므로 스냅샷도 다시 뜨지 않는다.
            continue
        if len(seen) >= MAX_PROVENANCE_FILE_SOURCES:
            # 상한은 중복 제거 **뒤** 서로 다른 파일 수에 걸린다. `snapshot_bytes` 앞에서
            # 끊는 이유는 그것이 파일 전체를 읽기 때문이다 — 전무로 끝낼 판에 4번째 파일을
            # 읽지 않는다.
            return [], []
        # `snapshot_bytes`는 같은 fd 에서 fstat 를 두 번 떠 안정 이미지만 돌려준다. 본문
        # 바이트는 쓰지 않고 버린다 — 수동 경로는 원문을 요약하지 않으므로(요약은 caller
        # 가 준다) "읽은 뒤 스냅샷" 사이의 창 자체가 없고, 읽기는 이 한 번뿐이다.
        snapshot = wiki_freshness.snapshot_bytes(resolved)
        if snapshot is None:
            return [], []
        seen.add(identity)
        collection = item.get("collection")
        collection = collection if isinstance(collection, str) else ""
        authoritative.append({"kind": "file", "path": rel, "collection": collection})
        revisions.append({
            "kind": "file", "path": rel, "collection": collection, **snapshot[0],
        })
    return authoritative, revisions


def attach_compiler_provenance(cwd: str, candidate: dict[str, Any]) -> None:
    """compile 직전에 provenance 를 **대입**한다(caller 값은 채택하지 않는다).

    `setdefault`가 아니라 pop + 대입인 이유는 `wiki_compile_worker`가 `engine`/`trigger`
    를 대입으로 덮는 것과 같다 — caller 가 낸 `sourceRevisions`가 채택되면 recall 신뢰의
    근거를 caller 가 스스로 발급하게 된다. 스냅샷할 파일 소스가 없으면 두 키 모두 만들지
    않아 기존 잡 형태가 그대로 유지된다.
    """
    candidate.pop("sourceRevisions", None)
    candidate.pop("authoritativeSources", None)
    authoritative, revisions = compiler_provenance(cwd, candidate.get("sources"))
    if revisions:
        candidate["authoritativeSources"] = authoritative
        candidate["sourceRevisions"] = revisions



def load_payload() -> dict[str, Any]:
    try:
        parsed = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9가-힣]+", "-", value.lower()).strip("-")
    return slug or "wiki-page"


def compact_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("candidates"), list):
        return [item for item in payload["candidates"] if isinstance(item, dict)]
    durable = payload.get("durable")
    if isinstance(durable, dict):
        return [durable]
    return []


def to_candidate(payload: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    title = item.get("title")
    summary = item.get("summary")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None

    suggested_type = item.get("suggestedType") or item.get("type") or "concept"
    if suggested_type not in TYPE_DIRS:
        suggested_type = "concept"
    target_path = item.get("targetPath")
    if not isinstance(target_path, str) or not target_path.strip():
        target_path = ""

    trigger = payload.get("trigger") if isinstance(payload.get("trigger"), str) else "manual"
    source_ref = payload.get("sourceRef") if isinstance(payload.get("sourceRef"), str) else "compact-context"
    source_kind = payload.get("sourceKind") if isinstance(payload.get("sourceKind"), str) else "session"
    sources = item.get("sources") if isinstance(item.get("sources"), list) else [{"kind": source_kind, "ref": source_ref}]

    # If a host accidentally passes transcript-shaped compact input, do not pass
    # the raw text through. wiki_compile will record a rejected candidate without
    # writing a wiki page.
    if TRANSCRIPT_RE.search(summary):
        summary = "[REDACTED_TRANSCRIPT] transcript-like compact input rejected before wiki compile."
        title = title.strip() or "Rejected transcript"
        candidate = {
            "trigger": trigger,
            "title": title,
            "summary": "User: [REDACTED_TRANSCRIPT]",
            "suggestedType": suggested_type,
            "confidence": item.get("confidence", "low"),
            "sources": sources,
            "targetPath": f".memvane/wiki/{TYPE_DIRS[suggested_type]}/{slugify(title)}.md",
        }
        if isinstance(item.get("canonicalKey"), str) and item.get("canonicalKey").strip():
            candidate["canonicalKey"] = item.get("canonicalKey").strip()
        if isinstance(item.get("aliases"), list):
            aliases = [alias.strip() for alias in item.get("aliases") if isinstance(alias, str) and alias.strip()]
            if aliases:
                candidate["aliases"] = aliases
        return candidate

    candidate = {
        "trigger": trigger,
        "title": title.strip(),
        "summary": summary.strip(),
        "suggestedType": suggested_type,
        "confidence": item.get("confidence", "medium"),
        "sources": sources,
    }
    if target_path:
        candidate["targetPath"] = target_path
    if isinstance(item.get("canonicalKey"), str) and item.get("canonicalKey").strip():
        candidate["canonicalKey"] = item.get("canonicalKey").strip()
    if isinstance(item.get("aliases"), list):
        aliases = [alias.strip() for alias in item.get("aliases") if isinstance(alias, str) and alias.strip()]
        if aliases:
            candidate["aliases"] = aliases
    if isinstance(item.get("related"), list):
        related = [key.strip() for key in item.get("related") if isinstance(key, str) and key.strip()]
        if related:
            candidate["related"] = related
    return candidate


def run_compile(cwd: str, candidate: dict[str, Any], regenerate: bool = False,
                budget_final: bool = False) -> str:
    script = Path(__file__).resolve().with_name("wiki_compile.py")
    argv = [sys.executable, str(script), "--cwd", cwd]
    if regenerate:
        argv.append("--regenerate")
    if budget_final:
        argv += ["--budget-final", "manual_final_pass"]
    proc = subprocess.run(
        argv,
        input=json.dumps(candidate, ensure_ascii=False),
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    return (proc.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract compact durable wiki candidates and compile them.")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--regenerate", action="store_true")
    # The skill's second run after its one split: an over-budget card is written
    # (wiki_card_budget best effort) instead of returning split-required again.
    parser.add_argument("--budget-final", action="store_true")
    args = parser.parse_args()

    if os.environ.get("QMD_SANDBOX"):
        return 0

    payload = load_payload()
    outputs = []
    for item in compact_items(payload):
        candidate = to_candidate(payload, item)
        if candidate is None:
            continue
        # 모델/caller 가 후보를 만든 **뒤에** 컴파일러가 provenance 를 소유한다(순서가 곧
        # 보장이다 — 자동 경로의 `wiki_compile_worker` 대입과 같은 자리).
        attach_compiler_provenance(args.cwd, candidate)
        out = run_compile(args.cwd, candidate, regenerate=args.regenerate,
                          budget_final=args.budget_final)
        if out:
            outputs.append(out)
    if outputs:
        print("\n".join(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
