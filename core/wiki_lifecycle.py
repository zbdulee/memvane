#!/usr/bin/env python3
"""SessionStart wiki lifecycle: judge every generated card by its recorded sources.

Runs in the `core/update.sh` worker (background fork, off the hook foreground).
A card is in scope only when the plugin owns its bytes
(`wiki_compile.auto_writable_text`: `createdBy: qmd-auto-context`, managed
auto block, not `superseded`). Human pages and anything outside the project's
wiki root are never read for action. The compiler-owned `sourceRevisions`
frontmatter (path, collection, sha256) is the only evidence:

| card state                              | action                                      |
|-----------------------------------------|---------------------------------------------|
| a recorded source file is missing       | retire (`wiki_source_retire.retire_deleted_source`) |
| a recorded source hash changed          | enqueue regeneration (compile source queue)  |
| no `sourceRevisions` block              | retire (`wiki_source_retire.retire_card`)    |
| every source present with the same hash | keep                                         |

Undecidable cards (malformed provenance, a source outside the allowed roots,
an unreadable source) are kept and logged. Retirement archives the old bytes
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
(`MAX_RETIRE_PER_RUN`) and enqueues by the compile per-wake cap. A capped run
stops its cursor at the last finished card, so the next session resumes there.
Mass-deletion guard: when more than `MASS_DELETE_FRACTION` of proved cards
(and more than `MASS_DELETE_MIN`) report a missing source in one run, nothing
is retired or enqueued and the run is logged as deferred (unmounted disk,
branch switch, moved checkout). A missing `sourceRevisions` block is a
property of the card itself, not of the filesystem, so it is left out of the
ratio and bounded by the per-run cap; a run that trips the guard defers it too.

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


def inspect_card(page: Path, root: Path, allow_roots: list[Path]) -> dict | None:
    """Ownership and provenance of one card, without hashing sources.

    Returns None for a card this lifecycle does not own. `missing` lists the
    recorded revisions whose file is gone (`recall.resolve_existing_source`
    is the existence SSOT); `present` maps the rest to their resolved path.
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
        info["state"] = UNPROVEN
        return info
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
              "kept": 0, "undecidable": 0, "ineligible": 0, "ineligibleMeasured": False,
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
    rels = [page.relative_to(wiki_root).as_posix() for page in pages]
    infos = {rel: inspect_card(wiki_root / rel, root, allow_roots) for rel in rels}
    proved = [info for info in infos.values()
              if info is not None and info["state"] in (KEEP, MISSING)]
    missing_cards = sum(1 for info in proved if info["state"] == MISSING)
    if missing_cards > MASS_DELETE_MIN and missing_cards > MASS_DELETE_FRACTION * len(proved):
        result["guard"] = {"missingCards": missing_cards, "provedCards": len(proved)}
        result["deferred"] = missing_cards
        log(f"DEFER: mass-deletion guard: {missing_cards}/{len(proved)} proved cards "
            f"report a missing source; nothing retired or enqueued")
        return result

    project_key = qmd_sync.project_key(str(root), found.get("configPath"))
    state_file = state_path(project_key)
    state = qmd_sync.read_state(state_file)
    cursor = state.get("cursor") if isinstance(state.get("cursor"), str) else ""
    refreshed = state.get("refreshed") if isinstance(state.get("refreshed"), dict) else {}
    window = rotate(rels, cursor, max_cards(scan_cfg))

    now = time.time()
    retire_budget = MAX_RETIRE_PER_RUN
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
        if kind in (MISSING, UNPROVEN) and retire_budget <= 0:
            log(f"DEFER: retire cap {MAX_RETIRE_PER_RUN} reached at {target}; next run resumes here")
            break
        if kind == UNPROVEN:
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
        f"undecidable={result['undecidable']} ineligible={result['ineligible']} cursor={last}")
    return result


def main() -> int:
    if os.environ.get("QMD_SANDBOX"):
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
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
