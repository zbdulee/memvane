#!/usr/bin/env python3
"""Opt-in, synthetic-only topical wiki schema and deterministic staging prototype.

No extractor, verifier, QMD daemon, hook, or model is called here. Outputs remain
``generated`` and are never installed into a project's live wiki collection.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

import wiki_compile
import wiki_freshness
import yaml_scalars

SCHEMA = "qmd-topical-sandbox-v1"
CARD_SCHEMA_VERSION = 2
MARKER = ".qmd-topical-sandbox"
PROJECT_MARKER = ".qmd-topical-v2-project"


def opted_in(root: Path) -> bool:
    sandbox = root / MARKER
    if sandbox.is_file() and not sandbox.is_symlink():
        return True
    project = root / PROJECT_MARKER
    if project.is_symlink() or not project.is_file():
        return False
    info = project.stat()
    return info.st_uid == os.getuid() and info.st_mode & 0o077 == 0


CARD_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
STATES = frozenset({"plan", "actual", "observation", "unknown", "rule"})
MAX_SOURCE_BYTES = 1024 * 1024
MAX_SPAN_LINES = 200
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class TopicalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _private_dir_fd(fd: int, code: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise TopicalError(code)


@contextmanager
def generation_base_fd(root: Path, *, create: bool = False):
    """Pin the sandbox generation parent; never traverse its symlink."""
    root = Path(root).resolve()
    try:
        root_fd = os.open(root, DIR_FLAGS)
    except OSError as exc:
        raise TopicalError("unsafe_generation_parent") from exc
    try:
        _private_dir_fd(root_fd, "unsafe_generation_parent")
        if create:
            try: os.mkdir("topical-generations", 0o700, dir_fd=root_fd)
            except FileExistsError: pass
        try: base_fd = os.open("topical-generations", DIR_FLAGS, dir_fd=root_fd)
        except OSError as exc: raise TopicalError("unsafe_generation_parent") from exc
        try:
            _private_dir_fd(base_fd, "unsafe_generation_parent")
            yield base_fd
        finally: os.close(base_fd)
    finally: os.close(root_fd)


@contextmanager
def generation_dir_fd(root: Path, generation_id: str):
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        raise TopicalError("invalid_generation_id")
    with generation_base_fd(root) as base_fd:
        try: fd = os.open(generation_id, DIR_FLAGS, dir_fd=base_fd)
        except OSError as exc: raise TopicalError("generation_missing") from exc
        try:
            _private_dir_fd(fd, "unsafe_generation_directory")
            yield fd
        finally: os.close(fd)


def read_generation_bytes(root: Path, generation_id: str, relative: str) -> bytes:
    """Read a generation member without following any parent or leaf symlink."""
    parts = Path(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts) or Path(relative).is_absolute():
        raise TopicalError("unsafe_generation_file")
    with generation_dir_fd(root, generation_id) as generation_fd:
        parent_fd = os.dup(generation_fd)
        try:
            for part in parts[:-1]:
                child_fd = os.open(part, DIR_FLAGS, dir_fd=parent_fd)
                os.close(parent_fd); parent_fd = child_fd
                _private_dir_fd(parent_fd, "unsafe_generation_file")
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 2 * 1024 * 1024):
                    raise TopicalError("unsafe_generation_file")
                with os.fdopen(os.dup(fd), "rb") as source: data = source.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024: raise TopicalError("unsafe_generation_file")
                return data
            finally: os.close(fd)
        except OSError as exc:
            raise TopicalError("unsafe_generation_file") from exc
        finally: os.close(parent_fd)


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=directory_fd)
    with os.fdopen(fd, "wb") as output:
        output.write(content); output.flush(); os.fsync(output.fileno())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def short_text(value, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise TopicalError(code)
    return value.strip()


def exact_quote(value) -> str:
    """Keep evidence bytes verbatim, including source line breaks."""
    if (not isinstance(value, str) or not value.strip() or len(value) < 6
            or len(value) > 1000 or any(ord(char) < 32 and char not in "\t\r\n" for char in value)):
        raise TopicalError("invalid_quote_anchor")
    return value


def safe_source(root: Path, raw) -> tuple[str, Path]:
    if (not isinstance(raw, str) or not raw or "\\" in raw or "#" in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise TopicalError("unsafe_source_path")
    relative = Path(raw)
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        topical_story_scope.read(root)
        allowed_roots = topical_story_scope.SOURCES
    else:
        allowed_roots = ('sources',)
    if (relative.is_absolute() or not relative.parts or relative.parts[0] not in allowed_roots
            or any(part in (".", "..") or part.startswith(".") for part in relative.parts)):
        raise TopicalError("unsafe_source_path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TopicalError("unsafe_source_path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TopicalError("unsafe_source_path") from exc
    if not path.is_file():
        raise TopicalError("source_missing_or_symlink")
    return relative.as_posix(), path


def source_snapshot(root: Path, raw, cache: dict) -> tuple[dict, str, list[str]]:
    rel, path = safe_source(root, raw)
    if rel not in cache:
        snapped = wiki_freshness.snapshot_bytes(path)
        if snapped is None or len(snapped[1]) > MAX_SOURCE_BYTES:
            raise TopicalError("source_unstable_or_too_large")
        rev, data = snapped
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TopicalError("source_not_utf8") from exc
        import topical_story_scope
        collection = (topical_story_scope.SOURCE_COLLECTIONS[rel.split('/', 1)[0]]
                      if topical_story_scope.exact_story(root) else 'sandbox-source')
        revision = {"kind": "file", "path": rel, "collection": collection, **rev}
        assert yaml_scalars.normalize_source_revision(revision) == revision
        cache[rel] = revision, body, body.splitlines(keepends=True)
    return cache[rel]


def valid_span(root: Path, evidence: dict, cache: dict) -> dict:
    if not isinstance(evidence, dict) or set(evidence) != {
        "sourcePath", "sourceRevisionSha256", "startLine", "endLine", "quoteAnchor", "quoteSha256"
    }:
        raise TopicalError("invalid_evidence_schema")
    revision, whole, lines = source_snapshot(root, evidence["sourcePath"], cache)
    if evidence["sourceRevisionSha256"] != revision["sha256"]:
        raise TopicalError("source_revision_mismatch")
    start, end = evidence["startLine"], evidence["endLine"]
    if (type(start) is not int or type(end) is not int or start < 1 or end < start
            or end > len(lines) or end - start + 1 > MAX_SPAN_LINES):
        raise TopicalError("invalid_line_span")
    anchor = exact_quote(evidence["quoteAnchor"])
    if evidence["quoteSha256"] != digest(anchor.encode("utf-8")):
        raise TopicalError("quote_hash_mismatch")
    if anchor not in "".join(lines[start - 1:end]):
        raise TopicalError("quote_outside_span")
    if whole.count(anchor) != 1:
        raise TopicalError("ambiguous_quote_anchor")
    return {**evidence, "sourcePath": revision["path"], "quoteAnchor": anchor}


def claim_line(claim: dict) -> str:
    condition = f"; 조건:{claim['condition']}" if claim["condition"] else ""
    return f"[{claim['state']}|{claim['timeScope']}{condition}] {claim['statement']}"


def valid_claim(root: Path, claim: dict, cache: dict) -> dict:
    if not isinstance(claim, dict) or set(claim) != {
        "claimId", "statement", "state", "timeScope", "condition", "evidence"
    }:
        raise TopicalError("invalid_claim_schema")
    claim_id = short_text(claim["claimId"], "invalid_claim_id")
    if not CARD_ID.fullmatch(claim_id):
        raise TopicalError("invalid_claim_id")
    statement = short_text(claim["statement"], "invalid_statement")
    state = claim["state"]
    if not isinstance(state, str) or state not in STATES:
        raise TopicalError("invalid_claim_state")
    time_scope = short_text(claim["timeScope"], "missing_time_scope")
    condition = claim["condition"]
    if not isinstance(condition, str) or "\n" in condition or "\r" in condition:
        raise TopicalError("invalid_condition")
    condition = condition.strip()
    if not isinstance(claim["evidence"], list) or not claim["evidence"]:
        raise TopicalError("missing_claim_evidence")
    evidence = [valid_span(root, item, cache) for item in claim["evidence"]]
    if len({(e["sourcePath"], e["startLine"], e["endLine"], e["quoteSha256"]) for e in evidence}) != len(evidence):
        raise TopicalError("duplicate_evidence")
    return {"claimId": claim_id, "statement": statement, "state": state,
            "timeScope": time_scope, "condition": condition, "evidence": evidence}


def normalize(root: Path, payload: dict, lead_budget: int = 600) -> tuple[list[dict], dict]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "cards"} or payload["schema"] != SCHEMA:
        raise TopicalError("invalid_bundle_schema")
    if type(lead_budget) is not int or lead_budget < 1 or lead_budget > 4000:
        raise TopicalError("invalid_lead_budget")
    entries = payload["cards"]
    if not isinstance(entries, list) or not entries or len(entries) > 50:
        raise TopicalError("invalid_card_count")
    cache: dict = {}
    cards: dict[str, dict] = {}
    for entry in entries:
        if (not isinstance(entry, dict) or
                set(entry) not in ({"cardId", "title", "category", "claims"},
                                   {"cardId", "title", "category", "details", "claims"})):
            raise TopicalError("invalid_card_schema")
        card_id = short_text(entry["cardId"], "invalid_card_id")
        if not CARD_ID.fullmatch(card_id):
            raise TopicalError("invalid_card_id")
        title = short_text(entry["title"], "invalid_card_title")
        category = entry["category"]
        if not isinstance(category, str) or category not in wiki_compile.ALLOWED_TYPES:
            raise TopicalError("invalid_card_category")
        details = entry.get("details", "")
        if not isinstance(details, str):
            raise TopicalError("invalid_card_details")
        if wiki_compile.has_secret_like(details):
            raise TopicalError("secret_like_details")
        if not isinstance(entry["claims"], list) or not entry["claims"]:
            raise TopicalError("missing_card_claims")
        card = cards.setdefault(card_id, {"cardId": card_id, "title": title,
                                          "category": category, "details": [], "claims": {}})
        if (card["title"], card["category"]) != (title, category):
            raise TopicalError("conflicting_canonical_card")
        if details.strip():
            card["details"].append(details.strip())
        for raw_claim in entry["claims"]:
            claim = valid_claim(root, raw_claim, cache)
            old = card["claims"].get(claim["claimId"])
            if old is None:
                card["claims"][claim["claimId"]] = claim
            else:
                if any(old[key] != claim[key] for key in ("statement", "state", "timeScope", "condition")):
                    raise TopicalError("conflicting_claim_identity")
                existing = {(e["sourcePath"], e["startLine"], e["endLine"], e["quoteSha256"])
                            for e in old["evidence"]}
                for evidence in claim["evidence"]:
                    key = (evidence["sourcePath"], evidence["startLine"], evidence["endLine"], evidence["quoteSha256"])
                    if key not in existing:
                        old["evidence"].append(evidence)
                        existing.add(key)
    result = []
    for card in cards.values():
        claims = sorted(card["claims"].values(), key=lambda item: item["claimId"])
        fingerprints = [(c["statement"], c["state"], c["timeScope"], c["condition"]) for c in claims]
        if len(set(fingerprints)) != len(fingerprints):
            raise TopicalError("duplicate_claim_identity")
        for claim in claims:
            claim["evidence"].sort(key=lambda e: (e["sourcePath"], e["startLine"], e["endLine"], e["quoteSha256"]))
        lead = "\n".join(claim_line(claim) for claim in claims)
        if len(lead) > lead_budget:
            raise TopicalError("semantic_split_required")
        revisions = {e["sourcePath"]: cache[e["sourcePath"]][0]
                     for claim in claims for e in claim["evidence"]}
        source_revisions = [revisions[path] for path in sorted(revisions)]
        candidate = {"title": card["title"], "summary": lead, "canonicalKey": card["cardId"],
                     "suggestedType": card["category"],
                     "sources": [{"kind": "file", "path": r["path"], "collection": r["collection"]}
                                 for r in source_revisions],
                     "sourceRevisions": source_revisions}
        lint = wiki_compile.lint_candidate(candidate, root / "topical-generations" / (card["cardId"] + ".md"), 120)
        if lint["verdict"] != "clean":
            raise TopicalError("legacy_lint_" + lint["findings"][0])
        result.append({"cardId": card["cardId"], "title": card["title"],
                       "category": card["category"], "lead": lead,
                       "details": "\n\n".join(card["details"]), "claims": claims,
                       "sourceRevisions": source_revisions, "candidate": candidate})
    return sorted(result, key=lambda card: card["cardId"]), cache


def compile_sandbox(root: Path, payload: dict, lead_budget: int = 600) -> dict:
    root = Path(root).resolve()
    if not opted_in(root):
        raise TopicalError("sandbox_marker_required")
    cards, cache = normalize(root, payload, lead_budget)
    digest_input = [{key: value for key, value in card.items() if key != "candidate"} for card in cards]
    generation_id = digest(encoded({"cards": digest_input, "leadBudget": lead_budget}))[:24]
    base = root / "topical-generations"
    final = base / generation_id
    with generation_base_fd(root, create=True) as base_fd:
        stage_name = ".stage-" + secrets.token_hex(16)
        os.mkdir(stage_name, 0o700, dir_fd=base_fd)
        stage_fd = os.open(stage_name, DIR_FLAGS, dir_fd=base_fd)
        cards_fd = None
        committed = False
        try:
            _private_dir_fd(stage_fd, "unsafe_generation_directory")
            os.mkdir("cards", 0o700, dir_fd=stage_fd)
            cards_fd = os.open("cards", DIR_FLAGS, dir_fd=stage_fd)
            _private_dir_fd(cards_fd, "unsafe_generation_directory")
            manifest = {"schema": SCHEMA, "cardSchemaVersion": CARD_SCHEMA_VERSION,
                        "status": "generated_unverified", "leadBudget": lead_budget,
                        "generationId": generation_id, "cards": []}
            for card in cards:
                candidate = card["candidate"]
                markdown = wiki_compile.markdown_page(
                    candidate, card["lead"], "generated", [], wiki_compile.source_hash(candidate))
                markdown = markdown.replace("---\n", f"---\nschemaVersion: {CARD_SCHEMA_VERSION}\n", 1)
                meta, parsed = wiki_compile.parse_frontmatter(markdown)
                if (not parsed or meta.get("status") != "generated"
                        or meta.get("schemaVersion") != str(CARD_SCHEMA_VERSION)
                        or meta.get("canonicalKey") != card["cardId"]
                        or yaml_scalars.load_source_revisions(meta.get("sourceRevisions")) != card["sourceRevisions"]
                        or f"\n## Summary\n{card['lead']}\n" not in markdown):
                    raise TopicalError("staged_card_readback_failed")
                name = card["cardId"]
                page_bytes = markdown.encode("utf-8")
                evidence_bytes = (json.dumps({key: value for key, value in card.items() if key != "candidate"},
                                             ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
                _write_at(cards_fd, name + ".md", page_bytes)
                _write_at(cards_fd, name + ".evidence.json", evidence_bytes)
                manifest["cards"].append({"cardId": name, "markdownSha256": digest(page_bytes),
                                          "evidenceSha256": digest(evidence_bytes),
                                          "sources": len(card["sourceRevisions"]), "claims": len(card["claims"]),
                                          "leadChars": len(card["lead"])})
            _write_at(stage_fd, "manifest.json",
                      (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
            os.fsync(cards_fd); os.fsync(stage_fd)
            for rel, (revision, _body, _lines) in cache.items():
                _, path = safe_source(root, rel)
                if wiki_freshness.compare_revision(path, revision)[0] != wiki_freshness.FRESH:
                    raise TopicalError("source_changed_before_commit")
            lock_fd = os.open(".commit.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                              dir_fd=base_fd)
            try:
                lock_info = os.fstat(lock_fd)
                if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                        or lock_info.st_mode & 0o022):
                    raise TopicalError("unsafe_generation_lock")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    os.stat(generation_id, dir_fd=base_fd, follow_symlinks=False)
                except FileNotFoundError: pass
                else: raise TopicalError("generation_already_exists")
                os.rename(stage_name, generation_id, src_dir_fd=base_fd, dst_dir_fd=base_fd)
                committed = True
                os.fsync(base_fd)
                current_parent = os.stat(base, follow_symlinks=False)
                opened_parent = os.fstat(base_fd)
                if (not stat.S_ISDIR(current_parent.st_mode) or
                        (current_parent.st_dev, current_parent.st_ino) !=
                        (opened_parent.st_dev, opened_parent.st_ino)):
                    raise TopicalError("generation_parent_changed")
            finally: os.close(lock_fd)
            return {"status": "generated_unverified", "generationId": generation_id,
                    "path": str(final), "cardCount": len(cards), "cards": manifest["cards"]}
        finally:
            if not committed:
                if cards_fd is not None:
                    for name in os.listdir(cards_fd): os.unlink(name, dir_fd=cards_fd)
                    os.rmdir("cards", dir_fd=stage_fd)
                try: os.unlink("manifest.json", dir_fd=stage_fd)
                except FileNotFoundError: pass
                os.rmdir(stage_name, dir_fd=base_fd)
            if cards_fd is not None: os.close(cards_fd)
            os.close(stage_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-root", type=Path, required=True)
    parser.add_argument("--lead-budget", type=int, default=600)
    args = parser.parse_args()
    try:
        result = compile_sandbox(args.sandbox_root, json.load(os.fdopen(0)), args.lead_budget)
    except (TopicalError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "rejected", "reason": getattr(exc, "code", "invalid_json")}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
