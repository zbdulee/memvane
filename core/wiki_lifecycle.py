#!/usr/bin/env python3
"""SessionStart wiki lifecycle: judge every generated card by its recorded sources.

Runs in the `core/update.sh` worker (background fork, off the hook foreground).
A card is in scope only when the plugin owns its bytes
(`wiki_compile.auto_writable_text`: `createdBy: qmd-auto-context`, managed
auto block, not `superseded`). Human pages and anything outside the project's
wiki root are never read for action. The compiler-owned `sourceRevisions`
frontmatter (path, collection, sha256) is the evidence; a card written before
that format existed is judged by its `sources:` file list instead:

| card state                                              | action |
|---------------------------------------------------------|--------|
| a recorded source file is missing                       | retire (`wiki_source_retire.retire_deleted_source`) |
| a recorded source hash changed                          | enqueue regeneration (compile source queue) |
| every source present with the same hash                 | keep |
| no `sourceRevisions`, every `sources:` file exists      | backfill: record the current bytes (`wiki_source_retire.record_source_revisions`), keep |
| no `sourceRevisions`, every `sources:` file is missing  | retire (`wiki_source_retire.retire_card`) |
| no `sourceRevisions` and no `sources:` file entry       | retire (`wiki_source_retire.retire_card`) |

A backfilled card is kept on that run (no regeneration); the next run judges
it like any recorded card. A legacy source is "missing" only when its
collection is one this project defines: a name the project does not know may
point at another checkout, so it is undecidable instead. Undecidable cards
(malformed provenance or `sources:`, a source outside the allowed roots, an
unreadable source, a legacy card with some sources present and some missing)
are kept and logged. Retirement archives the old bytes
under `.auto-context/compile/retired-wiki/` before unlinking, removes the
index line, and requests one `laya-sync` wake; the selected laya-db full scan
drops absent wiki files from its next generation. A missing source on a card
that still has live sources is retired only once those survivors are covered;
otherwise the deleted path goes to the compile queue as a `sourceDelete` row,
whose worker refreshes the survivors first.

The source-edit path needs no second hook: PostToolUse and raw discovery
(`laya_wiki_discovery`) already enqueue changed sources and `sourceDelete`
rows for deleted ones.

Bounds: hashing runs on a rotating window of cards (`compile.sourceScan.
maxCardsPerScan`, cursor in the sync state dir); retirements are capped per run
(`MAX_RETIRE_PER_RUN`), backfills by `MAX_BACKFILL_PER_RUN`, and enqueues by
the compile per-wake cap. A capped run stops its cursor at the last finished
card, so the next session resumes there and the corpus converges.
Mass-deletion guard: every would-be deletion counts — a missing recorded
source, a missing legacy source, and a card with no source at all. When more
than `MASS_DELETE_FRACTION` of the decided cards (and more than
`MASS_DELETE_MIN`) would be deleted in one run, nothing is retired, backfilled
or enqueued and the run is logged as deferred (unmounted disk, branch switch,
moved checkout, a corpus older than its provenance format).

`--dry-run` classifies every card (not the window) and prints the counts and
the guard verdict as JSON. It writes nothing: no card, state, log, lock or
queue, and it does not consult the selected route.

The run is idempotent: a retired card is gone on the next run, the archive
reuses same-content bytes, live-queue keys collapse a repeated enqueue, and a
refresh already enqueued for the same source hash is not enqueued again until
`REFRESH_RETRY_SECONDS` pass. Regeneration follows the legacy `session_start_source`
trigger consent; minimal settings (`settingsModel: "minimal"`) need none. Only the selected laya-db route acts; the legacy
QMD route only measures `ineligible`.

Fail-open: always exit 0, no stdout without `--json`, exceptions go to the log.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config as qmd_config
import sync as qmd_sync
import wiki_compile as wc
import wiki_source_missing as wsm
import wiki_source_retire

META_PAGES = ("index.md", "log.md")
DEFAULT_MAX_CARDS = 300
# frontmatter만 보는 ineligible 집계의 읽기 창(실코퍼스 frontmatter는 수백 바이트).
FRONTMATTER_READ_LIMIT = 16384
# 한 run이 보는 카드 상한. 넘으면 측정만 하고 조치하지 않는다(로그로 남긴다).
# 퇴역 helper와 같은 값이어야 한다 — 다르면 그 사이 구간에서 모든 퇴역이 `blocked`가 된다.
MAX_CARDS = wiki_source_retire.MAX_CARDS
MAX_CARD_BYTES = wiki_source_retire.MAX_CARD_BYTES
MAX_TOTAL_BYTES = wiki_source_retire.MAX_TOTAL_BYTES
MAX_RETIRE_PER_RUN = 25
# 레거시 카드 백필은 카드당 원문 해시 + 카드 원자 쓰기 1회다. 창(maxCardsPerScan)보다
# 작게 잡아 한 SessionStart worker의 쓰기 폭을 묶고, 커서가 다음 회차로 넘긴다.
MAX_BACKFILL_PER_RUN = 200
MASS_DELETE_FRACTION = 0.25
MASS_DELETE_MIN = 3
REFRESH_RETRY_SECONDS = 7 * 86400
MAX_REFRESH_STATE = 4096
STATE_VERSION = 1

KEEP = "keep"
MISSING = "missing"
CHANGED = "changed"
UNPROVEN = "unproven"
UNDECIDABLE = "undecidable"
BACKFILL = "backfill"
LEGACY_MISSING = "legacy_missing"
# 가드 분모(판정이 끝난 카드)와 분자(이번 run이 지울 카드).
DECIDED = (KEEP, MISSING, BACKFILL, LEGACY_MISSING, UNPROVEN)
DELETING = (MISSING, LEGACY_MISSING, UNPROVEN)


def log_path() -> Path:
    return Path(os.environ.get(
        "QMD_WIKI_LIFECYCLE_LOG", str(Path.home() / ".cache" / "qmd" / "wiki-lifecycle.log")))


def log(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    except OSError:
        pass


def state_path(project_key: str) -> Path:
    return qmd_sync.state_dir() / f"{project_key}-wiki-lifecycle.json"


def max_cards(scan_cfg: dict) -> int:
    env = os.environ.get("QMD_SOURCE_SCAN_MAX")
    if env:
        try:
            value = int(env)
            if value > 0:
                return value
        except ValueError:
            pass
    # 상한 클램프는 `config.compile_config`(MAX_SOURCE_SCAN_CARDS) 한 곳이다.
    try:
        value = int(scan_cfg.get("maxCardsPerScan", DEFAULT_MAX_CARDS) or DEFAULT_MAX_CARDS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CARDS
    return value if value > 0 else DEFAULT_MAX_CARDS


def card_paths(wiki_root: Path) -> list[Path]:
    """Regular `.md` cards under `wiki_root`; never follows symlinks or dot paths."""
    found = []
    for directory, dirnames, filenames in os.walk(wiki_root, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(name for name in dirnames
                             if not name.startswith(".") and not (base / name).is_symlink())
        for name in filenames:
            if (not name.endswith(".md") or name.startswith(".")
                    or (base == wiki_root and name in META_PAGES)):
                continue
            path = base / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append(path)
    return sorted(found)


def count_recall_ineligible(pages: list[Path]) -> int:
    """Cards `recall.is_auto_trusted_card` rejects, over **all** cards (not the window).

    This is the user-facing total behind the `wiki-ineligible` notice, so a
    partial count would report a wrong number. The judgement is recall's own.
    """
    import recall

    ineligible = 0
    for page in pages:
        try:
            with open(page, "r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(FRONTMATTER_READ_LIMIT)
        except OSError:
            ineligible += 1
            continue
        if not head.startswith("---"):
            ineligible += 1
            continue
        end = head.find("\n---", 3)
        meta = recall.parse_frontmatter_scalars(head[4:end] if end > 0 else head)
        meta["sourceRevisions"] = ["present"] if "\nsourceRevisions:\n" in head else []
        if not recall.is_auto_trusted_card(meta):
            ineligible += 1
    return ineligible


def rotate(rels: list[str], cursor: str, limit: int) -> list[str]:
    """`limit` entries after `cursor`, wrapping, so successive runs cover all cards."""
    if not rels:
        return []
    if limit >= len(rels):
        return rels
    start = 0
    if cursor:
        start = next((index for index, rel in enumerate(rels) if rel > cursor), 0)
    end = start + limit
    if end <= len(rels):
        return rels[start:end]
    return rels[start:] + rels[:end - len(rels)]


def has_revisions_header(block: str) -> bool:
    return any(line.startswith("sourceRevisions:") for line in block.split("\n"))


def read_card(page: Path) -> str | None:
    try:
        info = page.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CARD_BYTES:
            return None
        return page.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def known_collections(config: dict) -> frozenset[str]:
    """Collection names this project defines (collections, collectionPaths, roles)."""
    names: set[str] = set()
    listed = config.get("collections")
    if isinstance(listed, list):
        names.update(name for name in listed if isinstance(name, str) and name)
    for key in ("collectionPaths", "collectionRoles"):
        mapping = config.get(key)
        if isinstance(mapping, dict):
            names.update(name for name in mapping if isinstance(name, str) and name)
    return frozenset(names)


def inspect_legacy(info: dict, block: str, root: Path, allow_roots: list[Path],
                   known: frozenset[str]) -> dict:
    """Judge a card without `sourceRevisions` by its declared `sources:` files.

    Resolution is the same SSOT as recorded revisions
    (`recall.resolve_existing_source`: project root, then `allowRoots`); the
    declared `collection` is carried verbatim and decides only whether a
    missing path is provably gone (a collection this project defines) or may
    belong to another checkout (undecidable).
    """
    import recall

    records, parsed = wsm.declared_source_records(block)
    if parsed == wsm.SOURCES_INVALID:
        info["state"] = UNDECIDABLE
        info["reason"] = "invalid_sources"
        return info
    seen = set()
    files = []
    for record in records:
        if record.get("kind") != recall.SOURCE_KIND_FILE:
            continue
        collection = record.get("collection")
        collection = collection if isinstance(collection, str) else ""
        key = (record["path"], collection)
        if key not in seen:
            seen.add(key)
            files.append({"kind": recall.SOURCE_KIND_FILE, "path": record["path"],
                          "collection": collection})
    if not files:
        info["state"] = UNPROVEN
        return info
    info["legacy"] = True
    for source in files:
        resolved, reason = recall.resolve_existing_source(source["path"], root, allow_roots)
        if resolved is not None:
            info["present"].append((source, resolved))
        elif reason == "missing" and source["collection"] in known:
            info["missing"].append(source)
        else:
            info["state"] = UNDECIDABLE
            info["reason"] = ("source_foreign_collection" if reason == "missing"
                              else "source_" + (reason or "unavailable"))
            return info
    if info["missing"] and info["present"]:
        # 살아 있는 원문만 찍으면 소실된 원문에서 온 내용이 "전부 증명됨"으로 보인다.
        # 공유 카드 핸드오프(`sourceDelete`)는 기록된 revision으로 카드를 찾으므로 여기엔
        # 쓸 수 없다 — 사람이 정리하도록 남긴다.
        info["state"] = UNDECIDABLE
        info["reason"] = "legacy_partial_missing"
    elif info["missing"]:
        info["state"] = LEGACY_MISSING
    else:
        info["state"] = BACKFILL
    return info


def inspect_card(page: Path, root: Path, allow_roots: list[Path],
                 known: frozenset[str] = frozenset()) -> dict | None:
    """Ownership and provenance of one card, without hashing sources.

    Returns None for a card this lifecycle does not own. `missing` lists the
    recorded revisions whose file is gone (`recall.resolve_existing_source`
    is the existence SSOT); `present` maps the rest to their resolved path.
    A card with no `sourceRevisions` block goes to `inspect_legacy`.
    """
    import recall

    text = read_card(page)
    if text is None:
        return None
    owned, _ = wc.auto_writable_text(text)
    if not owned:
        return None
    match = wc.FRONTMATTER_RE.match(text)
    if match is None:
        return None
    block = match.group(1)
    info = {"text": text, "state": KEEP, "revisions": [], "missing": [], "present": []}
    if not has_revisions_header(block):
        return inspect_legacy(info, block, root, allow_roots, known)
    revisions = recall.frontmatter_source_revisions(block)
    if not revisions:
        info["state"] = UNDECIDABLE
        info["reason"] = "invalid_source_revisions"
        return info
    info["revisions"] = revisions
    for revision in revisions:
        resolved, reason = recall.resolve_existing_source(revision["path"], root, allow_roots)
        if resolved is not None:
            info["present"].append((revision, resolved))
        elif reason == "missing":
            info["missing"].append(revision)
        else:
            info["state"] = UNDECIDABLE
            info["reason"] = "source_" + (reason or "unavailable")
            return info
    if info["missing"]:
        info["state"] = MISSING
    return info


def changed_sources(info: dict) -> list[tuple[dict, str]] | None:
    """`[(revision, current_sha)]` whose bytes differ; None when unreadable."""
    import wiki_freshness

    changed = []
    for revision, resolved in info["present"]:
        state, _reason = wiki_freshness.compare_revision(resolved, revision)
        if state == wiki_freshness.FRESH:
            continue
        if state != wiki_freshness.STALE:
            return None
        snapshot = wiki_freshness.snapshot_file(resolved)
        if snapshot is None:
            return None
        changed.append((revision, snapshot["sha256"]))
    return changed


def backfill_revisions(info: dict) -> list[dict] | None:
    """Closed-schema revisions of a BACKFILL card's current source bytes; None if unreadable."""
    import wiki_freshness

    revisions = []
    for source, resolved in info["present"]:
        snapshot = wiki_freshness.snapshot_file(resolved)
        if snapshot is None:
            return None
        revisions.append({**source, "sha256": snapshot["sha256"], "size": snapshot["size"],
                          "mtimeNs": snapshot["mtimeNs"]})
    return revisions


def legacy_still_missing(info: dict, root: Path, allow_roots: list[Path]) -> bool:
    """Recheck right before retirement: every judged-missing legacy source is still gone."""
    import recall

    return all(recall.resolve_existing_source(source["path"], root, allow_roots)[1] == "missing"
               for source in info["missing"])


def guard_verdict(infos: dict) -> dict:
    """Mass-deletion guard over every decided card; `tripped` means defer the whole run."""
    states = [info["state"] for info in infos.values() if info is not None]
    decided = sum(1 for state in states if state in DECIDED)
    verdict = {
        "wouldDelete": sum(1 for state in states if state in DELETING),
        "decidedCards": decided,
        "missingCards": sum(1 for state in states if state == MISSING),
        "legacyMissingCards": sum(1 for state in states if state == LEGACY_MISSING),
        "noSourceCards": sum(1 for state in states if state == UNPROVEN),
    }
    verdict["tripped"] = (verdict["wouldDelete"] > MASS_DELETE_MIN
                          and verdict["wouldDelete"] > MASS_DELETE_FRACTION * decided)
    return verdict


def _add_change(changed_files: dict, root: Path, revision: dict, sha: str, kind: str) -> None:
    entry = changed_files.setdefault(revision["collection"], {
        "root": str(root), "created": [], "updated": [], "deleted": [], "revisions": {}})
    if revision["path"] not in entry[kind]:
        entry[kind].append(revision["path"])
    entry["revisions"][revision["path"]] = sha


def _recently_refreshed(refreshed: dict, path: str, sha: str, now: float) -> bool:
    row = refreshed.get(path)
    return (isinstance(row, dict) and row.get("sha256") == sha
            and isinstance(row.get("ts"), (int, float)) and now - row["ts"] < REFRESH_RETRY_SECONDS)


def regeneration_config(config: dict) -> dict:
    """Config the regeneration enqueue is gated by.

    Minimal settings make wiki generation plugin-owned (always on), so the
    lifecycle refresh needs no separate `session_start_source` grant there.
    Legacy settings keep their explicit trigger consent.
    """
    if not qmd_config.is_minimal_config(config):
        return config
    import wiki_source_intent
    compile_cfg = dict(config.get("compile") or {})
    triggers = list(compile_cfg.get("triggers") or [])
    for trigger in wiki_source_intent.SESSION_START_ACCEPTED_TRIGGERS:
        if trigger not in triggers:
            triggers.append(trigger)
    compile_cfg["triggers"] = triggers
    return {**config, "compile": compile_cfg}


def run(cwd: str) -> dict:
    # `ineligibleMeasured` separates a measured 0 from "not counted": update.sh
    # clears the wiki-ineligible notice state only on a measured run.
    result = {"cards": 0, "examined": 0, "retired": 0, "enqueued": 0, "deferred": 0,
              "kept": 0, "backfilled": 0, "undecidable": 0, "ineligible": 0,
              "ineligibleMeasured": False,
              "route": None, "guard": None}
    found = qmd_config.find_project_config(cwd)
    root = Path(found["projectRoot"]).resolve()
    config = found["config"]
    if config.get("indexing") is not True:
        log("SKIP: indexing is not enabled")
        return result
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    scan_cfg = compile_cfg.get("sourceScan") if isinstance(compile_cfg.get("sourceScan"), dict) else {}
    wiki_root = wsm.wiki_root_of(root, config)
    if wiki_root is None or wiki_root.is_symlink():
        log("ABORT: unsafe wikiPath")
        return result
    if not wiki_root.is_dir():
        result["ineligibleMeasured"] = True
        return result

    pages = card_paths(wiki_root)
    result["cards"] = len(pages)
    result["ineligible"] = count_recall_ineligible(pages)
    result["ineligibleMeasured"] = True

    if not scan_cfg.get("enabled", True):
        log("SKIP: compile.sourceScan.enabled is false")
        return result
    if len(pages) > MAX_CARDS:
        log(f"SKIP: {len(pages)} cards exceed MAX_CARDS={MAX_CARDS}")
        result["deferred"] = len(pages)
        return result

    import wiki_selected_laya
    try:
        selected = wiki_selected_laya.selected(root)
    except ValueError as exc:
        log(f"DEFER: selected route unavailable ({exc})")
        result["route"] = "unavailable"
        return result
    if selected is None:
        result["route"] = "legacy"
        log("SKIP: legacy QMD route (lifecycle acts on the selected laya-db route only)")
        return result
    result["route"] = "selected"

    total = 0
    for page in pages:
        try:
            total += page.lstat().st_size
        except OSError:
            continue
    if total > MAX_TOTAL_BYTES:
        log(f"SKIP: wiki is {total} bytes, over MAX_TOTAL_BYTES={MAX_TOTAL_BYTES}")
        return result
    allow_roots = wsm.allow_roots_of(config)
    known = known_collections(config)
    rels = [page.relative_to(wiki_root).as_posix() for page in pages]
    infos = {rel: inspect_card(wiki_root / rel, root, allow_roots, known) for rel in rels}
    verdict = guard_verdict(infos)
    if verdict["tripped"]:
        result["guard"] = {key: value for key, value in verdict.items() if key != "tripped"}
        result["deferred"] = verdict["wouldDelete"]
        log(f"DEFER: mass-deletion guard: {verdict['wouldDelete']}/{verdict['decidedCards']} "
            f"decided cards would be deleted (missing={verdict['missingCards']} "
            f"legacyMissing={verdict['legacyMissingCards']} noSource={verdict['noSourceCards']}); "
            f"nothing retired, backfilled or enqueued")
        return result

    project_key = qmd_sync.project_key(str(root), found.get("configPath"))
    state_file = state_path(project_key)
    state = qmd_sync.read_state(state_file)
    cursor = state.get("cursor") if isinstance(state.get("cursor"), str) else ""
    refreshed = state.get("refreshed") if isinstance(state.get("refreshed"), dict) else {}
    window = rotate(rels, cursor, max_cards(scan_cfg))

    now = time.time()
    retire_budget = MAX_RETIRE_PER_RUN
    backfill_budget = MAX_BACKFILL_PER_RUN
    changed_files: dict = {}
    pending_refresh: dict[str, str] = {}
    last = cursor
    for rel in window:
        info = infos.get(rel)
        card = wiki_root / rel
        target = card.relative_to(root).as_posix()
        # A retirement earlier in this run may already have removed this card.
        if info is None or not os.path.lexists(card):
            last = rel
            continue
        result["examined"] += 1
        kind = info["state"]
        if kind in DELETING and retire_budget <= 0:
            log(f"DEFER: retire cap {MAX_RETIRE_PER_RUN} reached at {target}; next run resumes here")
            break
        if kind == BACKFILL and backfill_budget <= 0:
            log(f"DEFER: backfill cap {MAX_BACKFILL_PER_RUN} reached at {target}; "
                f"next run resumes here")
            break
        if kind == BACKFILL:
            backfill_budget -= 1
            revisions = backfill_revisions(info)
            if revisions is None:
                result["undecidable"] += 1
                log(f"KEEP undecidable {target}: source_unreadable")
            else:
                outcome = wiki_source_retire.record_source_revisions(
                    root, wiki_root, selected, card, info["text"], revisions)
                log(f"BACKFILL source_revisions {target}: {outcome}")
                if outcome == "recorded":
                    result["backfilled"] += 1
                elif outcome != "gone":
                    result["undecidable"] += 1
        elif kind == LEGACY_MISSING:
            retire_budget -= 1
            if not legacy_still_missing(info, root, allow_roots):
                result["undecidable"] += 1
                log(f"KEEP legacy source reappeared {target}")
            else:
                outcome = wiki_source_retire.retire_card(
                    root, wiki_root, selected, card, info["text"],
                    "lifecycle:legacy_source_missing")
                log(f"RETIRE legacy_source_missing {target} "
                    f"({', '.join(source['path'] for source in info['missing'])}): {outcome}")
        elif kind == UNPROVEN:
            retire_budget -= 1
            outcome = wiki_source_retire.retire_card(
                root, wiki_root, selected, card, info["text"], "lifecycle:no_source_revisions")
            log(f"RETIRE no_source_revisions {target}: {outcome}")
        elif kind == MISSING:
            retire_budget -= 1
            for revision in info["missing"]:
                outcome = wiki_source_retire.retire_deleted_source(
                    root, config, selected, revision["collection"], revision["path"],
                    revision["sha256"])["state"]
                log(f"RETIRE source_missing {target} ({revision['path']}): {outcome}")
                if outcome == "shared_pending":
                    # Live sources remain on this card. The compile worker refreshes
                    # them first for a `sourceDelete` row, then retires the card.
                    key = "delete:" + revision["path"]
                    if not _recently_refreshed(refreshed, key, revision["sha256"], now):
                        _add_change(changed_files, root, revision, revision["sha256"], "deleted")
                        pending_refresh[key] = revision["sha256"]
                elif outcome != "retired":
                    result["undecidable"] += 1
        elif kind == UNDECIDABLE:
            result["undecidable"] += 1
            log(f"KEEP undecidable {target}: {info.get('reason')}")
        else:
            changed = changed_sources(info)
            if changed is None:
                result["undecidable"] += 1
                log(f"KEEP undecidable {target}: source_unreadable")
            elif not changed:
                # The refresh state stays: another card citing the same source may
                # still be stale, and dropping the entry would re-enqueue it every run.
                result["kept"] += 1
            else:
                for revision, sha in changed:
                    if not _recently_refreshed(refreshed, revision["path"], sha, now):
                        _add_change(changed_files, root, revision, sha, "updated")
                        pending_refresh[revision["path"]] = sha
        last = rel

    if changed_files:
        import wiki_compile_enqueue
        outcome = wiki_compile_enqueue.enqueue_changed_sources(
            str(root), regeneration_config(config), changed_files, session_start=True)
        reason = outcome.get("reason")
        deferred = {tuple(item)[1] for item in outcome.get("deferredFiles", [])}
        log(f"ENQUEUE {reason}: queued={outcome.get('queued', 0)} deferred={len(deferred)} "
            f"paths={sorted(pending_refresh)}")
        result["enqueued"] = int(outcome.get("queued") or 0)
        result["deferred"] += len(deferred)
        if reason in ("queued", "capped"):
            for key, sha in pending_refresh.items():
                if key.removeprefix("delete:") not in deferred:
                    refreshed[key] = {"sha256": sha, "ts": now}

    result["retired"] = sum(1 for page in pages if not os.path.lexists(page))
    if result["retired"]:
        # retire_deleted_source wakes laya-sync itself; retire_card does not. One
        # coalesced wake covers both, and the full scan drops absent wiki files.
        try:
            wiki_selected_laya.request_sync(root, selected)
        except ValueError as exc:
            log(f"WAKE laya-sync failed: {exc}")

    if len(refreshed) > MAX_REFRESH_STATE:
        keep = sorted(refreshed.items(), key=lambda item: -float(item[1].get("ts") or 0))
        refreshed = dict(keep[:MAX_REFRESH_STATE])
    qmd_sync.write_state_atomic(state_file, {
        "version": STATE_VERSION, "projectRoot": str(root), "cursor": last,
        "refreshed": refreshed,
    })
    log(f"cards={result['cards']} examined={result['examined']} retired={result['retired']} "
        f"enqueued={result['enqueued']} deferred={result['deferred']} kept={result['kept']} "
        f"backfilled={result['backfilled']} undecidable={result['undecidable']} ineligible={result['ineligible']} cursor={last}")
    return result


def dry_run(cwd: str) -> dict:
    """Read-only census of every card with the same judgement and guard as `run`.

    Writes nothing (no card, lifecycle state, log, lock, queue or archive) and
    does not consult the selected route, so it can be pointed at a project
    before the lifecycle is installed there. `changed` cards are those whose
    recorded source hash differs (they would be regenerated, not deleted).
    """
    found = qmd_config.find_project_config(cwd)
    root = Path(found["projectRoot"]).resolve()
    config = found["config"]
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    scan_cfg = compile_cfg.get("sourceScan") if isinstance(compile_cfg.get("sourceScan"), dict) else {}
    report = {"dryRun": True, "projectRoot": str(root), "indexing": config.get("indexing") is True,
              "sourceScanEnabled": bool(scan_cfg.get("enabled", True)), "cards": 0,
              "counts": {"keep": 0, "changed": 0, "backfill": 0, "missing": 0,
                         "legacyMissing": 0, "retire": 0, "undecidable": 0, "manual": 0},
              "undecidableReasons": {}, "skip": None, "guard": None}
    wiki_root = wsm.wiki_root_of(root, config)
    if wiki_root is None or wiki_root.is_symlink():
        report["skip"] = "unsafe_wiki_path"
        return report
    if not wiki_root.is_dir():
        report["skip"] = "no_wiki"
        return report
    pages = card_paths(wiki_root)
    report["cards"] = len(pages)
    total = sum(page.lstat().st_size for page in pages if page.exists())
    if len(pages) > MAX_CARDS:
        report["skip"] = f"cards_over_MAX_CARDS_{MAX_CARDS}"
    elif total > MAX_TOTAL_BYTES:
        report["skip"] = f"bytes_over_MAX_TOTAL_BYTES_{MAX_TOTAL_BYTES}"
    allow_roots = wsm.allow_roots_of(config)
    known = known_collections(config)
    infos = {page: inspect_card(page, root, allow_roots, known) for page in pages}
    counts = report["counts"]
    reasons = report["undecidableReasons"]
    names = {KEEP: "keep", BACKFILL: "backfill", MISSING: "missing",
             LEGACY_MISSING: "legacyMissing", UNPROVEN: "retire", UNDECIDABLE: "undecidable"}
    for info in infos.values():
        if info is None:
            counts["manual"] += 1
            continue
        state = info["state"]
        reason = info.get("reason")
        if state == KEEP:
            changed = changed_sources(info)
            if changed is None:
                state, reason = UNDECIDABLE, "source_unreadable"
            elif changed:
                counts["changed"] += 1
                continue
        counts[names[state]] += 1
        if state == UNDECIDABLE:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
    report["guard"] = guard_verdict(infos)
    return report


def main() -> int:
    if os.environ.get("QMD_SANDBOX"):
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print per-state counts and the guard verdict; write nothing")
    args = parser.parse_args()
    if args.dry_run:
        # 운영자가 직접 부르는 진단 경로다 — fail-open이 아니라 오류를 그대로 보여 준다.
        print(json.dumps(dry_run(args.cwd), ensure_ascii=False, indent=2))
        return 0
    try:
        result = run(args.cwd)
    except Exception as exc:  # fail-open: never break the update worker
        log(f"EXCEPTION: {exc!r}")
        return 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
