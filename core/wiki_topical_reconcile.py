#!/usr/bin/env python3
"""Sandbox-only, hook-independent source-to-topical-card reconciliation.

No extractor, model, index, or host hook is called. One atomic state file holds
the source snapshot, durable work queue, and fail-closed projection together.
"""
from __future__ import annotations

import fcntl
import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import posttool
import wiki_topical as topical
from wiki_topical_selection import claim_identity

STATE = "topical-reconcile-state.json"
HISTORY = "topical-reconcile-history"
DENIED = frozenset({".auto-context", ".agents", ".codex", ".claude", ".git", ".hg", ".svn",
                    ".qmd", "node_modules", "__pycache__", "skills", "plugins", "hooks",
                    "topical-generations", "topical-attestations", "projection", "index", "logs"})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sandbox(root):
    root = Path(root).resolve()
    if not topical.opted_in(root):
        raise ValueError("sandbox_marker_required")
    return root


def safe_rel(value, *, explicit_root=False, roots=()):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid_relative_path")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("invalid_relative_path")
    if any(part in DENIED for part in path.parts):
        raise ValueError("excluded_source_path")
    if not explicit_root:
        allowed_prefix = next((prefix for prefix in roots if value.startswith(prefix + "/")), None)
        inner = value[len(allowed_prefix) + 1:] if allowed_prefix is not None else value
        if any(part.startswith(".") for part in Path(inner).parts):
            raise ValueError("hidden_source_path")
    return path.as_posix()


def _within_roots(path, roots):
    return any(path.startswith(prefix + "/") for prefix in roots)


def _scan(root, source_roots, skip_paths):
    files = {}
    def fail_walk(error):
        raise error
    for prefix in source_roots:
        folder = root / prefix
        if folder.is_symlink() or not folder.is_dir() or folder.resolve() != folder:
            raise ValueError("source_root_unavailable_or_symlink")
        for dirpath, dirs, names in os.walk(folder, followlinks=False, onerror=fail_walk):
            dirs[:] = [name for name in dirs if not name.startswith(".") and name not in DENIED
                       and not (Path(dirpath) / name).is_symlink()]
            for name in names:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".md":
                    continue
                rel = path.relative_to(root).as_posix()
                try:
                    safe_rel(rel, roots=source_roots)
                except ValueError:
                    continue
                if any(skip in rel for skip in skip_paths):
                    continue
                # A missing/unreadable file aborts the entire transaction: never
                # convert a partial scan into a mass deletion.
                body = path.read_bytes()
                files[rel] = {"sha256": hashlib.sha256(body).hexdigest(),
                              "size": len(body)}
    return dict(sorted(files.items()))


def _cards(cards, roots):
    if not isinstance(cards, list):
        raise ValueError("invalid_cards")
    result = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("cardId"), str) or not card["cardId"]:
            raise ValueError("invalid_card")
        if card["cardId"] in result or not isinstance(card.get("claims"), list) or not card["claims"]:
            raise ValueError("duplicate_or_empty_card")
        refs = set()
        for claim in card["claims"]:
            claim_identity(claim)
            for evidence in claim["evidence"]:
                path = safe_rel(evidence["sourcePath"], roots=roots)
                if not _within_roots(path, roots):
                    raise ValueError("claim_outside_explicit_sources")
                refs.add((path, evidence["sourceRevisionSha256"]))
        revisions = card.get("sourceRevisions")
        if (not isinstance(revisions, list) or
                {(safe_rel(row["path"], roots=roots), row["sha256"]) for row in revisions} != refs):
            raise ValueError("source_revisions_do_not_match_claims")
        result[card["cardId"]] = card
    return result


def _read(root):
    path = root / STATE
    if path.is_symlink():
        raise ValueError("unsafe_state_symlink")
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != "topical-reconcile-v1"
            or not {"revision", "sourceRoots", "sources", "settledSources", "queue", "moves",
                    "projection", "inFlight", "awaitingVerification"} <= set(value)
            or type(value["revision"]) is not int or value["revision"] < 1
            or not all(isinstance(value[key], dict) for key in
                       ("sources", "settledSources", "queue", "projection"))):
        raise ValueError("unknown_state_schema")
    held = value.get("heldSources", {})
    if (not isinstance(held, dict) or any(
            not isinstance(path, str) or not isinstance(sha, str) or len(sha) != 64
            for path, sha in held.items())):
        raise ValueError("unknown_state_schema")
    return value


def _atomic(path, value):
    if path.is_symlink():
        raise ValueError("unsafe_output_symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent:
        raise ValueError("unsafe_output_parent_symlink")
    handle, temporary = tempfile.mkstemp(prefix=".reconcile-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _projection(cards, files, trusted_card_ids):
    result = {}
    for card_id, card in sorted(cards.items()):
        claims = []
        for claim in card["claims"]:
            sources = []
            for evidence in claim["evidence"]:
                path = evidence["sourcePath"]
                observed = files.get(path)
                expected = evidence["sourceRevisionSha256"]
                sources.append({"path": path, "expectedSha256": expected,
                                "currentSha256": observed["sha256"] if observed else None,
                                "state": ("missing" if observed is None else
                                          "unchanged" if observed["sha256"] == expected else "modified")})
            claims.append({"claimId": claim["claimId"], "sourceStates": sources,
                           "state": "prior_evidence_unchanged" if all(
                               item["state"] == "unchanged" for item in sources) else "stale"})
        current = all(claim["state"] == "prior_evidence_unchanged" for claim in claims)
        result[card_id] = {"state": ("eligible_existing_attestation" if card_id in trusted_card_ids
                                        else "source_current_attestation_required") if current else "excluded_stale",
                           "claims": claims}
    return result


def _pending(settled, current):
    result = {}
    for path in sorted(set(settled) | set(current)):
        before, after = settled.get(path), current.get(path)
        if before is None and after is None or before is not None and after is not None and before["sha256"] == after["sha256"]:
            continue
        result[path] = {"path": path, "kind": "created" if before is None else
                        "deleted" if after is None else "modified",
                        "baselineSha256": before["sha256"] if before else None,
                        "currentSha256": after["sha256"] if after else None}
    return result


def turn_key(payload):
    """Codex has a turn id; Claude Stop has session id plus final message."""
    if not isinstance(payload, dict):
        return "recovery"
    turn = payload.get("turn_id")
    if isinstance(turn, str) and turn:
        return "codex:" + turn
    session = payload.get("session_id")
    if isinstance(session, str) and session:
        message = payload.get("last_assistant_message")
        return "claude:" + sync_hash(session + "\n" + (message if isinstance(message, str) else ""))[:24]
    return "recovery"


def sync_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def reconcile(root, source_roots, cards, *, trusted_card_ids=(), hints=(), skip_paths=(),
              dry_run=False, bootstrap_unclaimed=False):
    root = sandbox(root)
    if not isinstance(source_roots, (list, tuple)) or not source_roots:
        raise ValueError("explicit_source_roots_required")
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    if not isinstance(skip_paths, (list, tuple)) or not all(isinstance(x, str) and x for x in skip_paths):
        raise ValueError("invalid_skip_paths")
    validated = _cards(cards, roots)
    trusted = set(trusted_card_ids)
    if not trusted <= set(validated) or not all(isinstance(x, str) for x in trusted):
        raise ValueError("invalid_trusted_card_ids")
    # Hints are intentionally non-authoritative. Missing, duplicated, reordered,
    # or unrelated plugin events cannot change the filesystem-derived result.
    if not isinstance(hints, (list, tuple)):
        raise ValueError("invalid_hints")
    lock = root / ".topical-reconcile.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_lock_symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        if previous is not None and previous["sourceRoots"] != roots:
            raise ValueError("source_roots_changed")
        files = _scan(root, roots, skip_paths)
        old = previous["sources"] if previous else {}
        removed = {path: value for path, value in old.items() if path not in files}
        added = {path: value for path, value in files.items() if path not in old}
        # Pair only unambiguous content-identical moves. Never rewrite evidence
        # to the new path or carry an old attestation over to it.
        moves = list(previous["moves"]) if previous else []
        for path, value in removed.items():
            matches = [new for new, candidate in added.items() if candidate["sha256"] == value["sha256"]]
            if len(matches) == 1 and sum(1 for old_value in removed.values()
                                         if old_value["sha256"] == value["sha256"]) == 1:
                move = {"from": path, "to": matches[0], "contentSha256": value["sha256"]}
                if move not in moves:
                    moves.append(move)
        events = []
        for path in sorted(set(old) | set(files)):
            a, b = old.get(path), files.get(path)
            if a == b:
                continue
            kind = "created" if a is None else "deleted" if b is None else "modified"
            events.append({"id": digest({"path": path, "old": a, "new": b}),
                           "kind": kind, "path": path, "before": a, "after": b})
        settled = previous["settledSources"] if previous else ({} if bootstrap_unclaimed and not validated else files)
        pending = _pending(settled, files)
        projection = _projection(validated, files, trusted)
        if previous is not None and pending:
            # A published page can be temporarily absent while a refresh
            # retires the old generation and installs the replacement. Keep
            # its batch owner in the durable projection until the backend
            # finishes; never expose the carried row to recall as current.
            for card_id, row in previous["projection"].items():
                if card_id not in projection:
                    carried = json.loads(canonical(row))
                    carried["state"] = "excluded_pending_refresh"
                    for claim in carried["claims"]:
                        claim["state"] = "pending_refresh"
                    projection[card_id] = carried
        state = {"schema": "topical-reconcile-v1", "sourceRoots": roots,
                 "sources": files, "settledSources": settled, "queue": pending,
                 "heldSources": {path: sha for path, sha in
                                 (previous.get("heldSources", {}) if previous else {}).items()
                                 if path in files and files[path]["sha256"] == sha},
                 "moves": moves, "projection": projection,
                 "inFlight": previous["inFlight"] if previous else None,
                 "awaitingVerification": previous["awaitingVerification"] if previous else [],
                 "activeGeneration": previous.get("activeGeneration") if previous else None,
                 "generatedProjection": previous.get("generatedProjection", {}) if previous else {},
                 "qmdIndexState": previous.get("qmdIndexState", {"status": "not_configured"}) if previous else {"status": "not_configured"},
                 "revision": (previous["revision"] + 1 if previous else 1)}
        changed = previous is None or any(state[key] != previous.get(key) for key in
                                          ("sources", "queue", "moves", "projection", "heldSources"))
        if not changed:
            return {"changed": False, "events": [], "state": previous}
        if dry_run:
            return {"changed": True, "events": events, "state": state, "dryRun": True}
        if previous is not None:
            archive = root / HISTORY / f"{previous['revision']}.json"
            if archive.exists():
                if json.loads(archive.read_text()) != previous:
                    raise ValueError("history_conflict")
            else:
                _atomic(archive, previous)
        _atomic(root / STATE, state)
        return {"changed": True, "events": events, "state": state}


def _hint_path(value, cwd, root, roots, skip_paths):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    absolute = path if path.is_absolute() else Path(cwd) / path
    try:
        rel = absolute.resolve().relative_to(root).as_posix()
        safe_rel(rel, roots=roots)
    except (OSError, ValueError):
        return None
    if not _within_roots(rel, roots) or not rel.lower().endswith(".md"):
        return None
    if any(skip in rel for skip in skip_paths):
        return None
    return rel


def _events_from_payload(payload, cwd):
    """Only supported editor payloads; never infer a shell delete or rename."""
    name = payload.get("tool_name")
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        return []
    if name == "apply_patch":
        patch = value.get("patch") if isinstance(value.get("patch"), str) else value.get("command")
        if not isinstance(patch, str):
            return []
        events = []
        for line in patch.splitlines():
            for prefix, kind in (("*** Add File: ", "write"), ("*** Update File: ", "modify"),
                                 ("*** Delete File: ", "delete"), ("*** Move to: ", "rename_target")):
                if line.startswith(prefix):
                    events.append((kind, line[len(prefix):].strip()))
        return events
    if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        kind = "write" if name == "Write" else "modify"
        return [(kind, path) for path in posttool.edited_paths(payload)]
    return []


def enqueue_hook_hint(root, source_roots, payload, *, skip_paths=()):
    """Fast optional PostToolUse adapter. No scan, model, or background worker.

    The caller may map this to the existing compile hook's CLI after testing.
    Unsupported tool events are ignored; a later reconcile catches missed edits.
    """
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (None, "PostToolUse", "AfterTool"):
        return {"queued": 0, "reason": "unsupported_hook_event"}
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return {"queued": 0, "reason": "missing_cwd"}
    events = []
    for kind, original in _events_from_payload(payload, cwd):
        path = _hint_path(original, cwd, root, roots, skip_paths)
        if path is not None:
            events.append((kind, path))
    if not events:
        return {"queued": 0, "reason": "no_supported_source_paths"}
    lock = root / ".topical-reconcile.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_lock_symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        if previous is None or previous["sourceRoots"] != roots:
            raise ValueError("reconcile_baseline_required")
        state = json.loads(canonical(previous))
        old_pending = set(state["queue"])
        for kind, path in events:
            # The previous snapshot is the durable comparison baseline. Hints
            # cannot proclaim a source fresh, including rename targets.
            observed = None
            candidate = root / path
            if candidate.is_file() and not candidate.is_symlink():
                observed = hashlib.sha256(candidate.read_bytes()).hexdigest()
            baseline = state["settledSources"].get(path)
            if (baseline["sha256"] if baseline else None) == observed:
                state["queue"].pop(path, None)
                continue
            state["queue"][path] = {"path": path, "kind": "created" if baseline is None else
                                    "deleted" if observed is None else "modified",
                                    "baselineSha256": baseline["sha256"] if baseline else None,
                                    "currentSha256": observed}
            for card in state["projection"].values():
                affected = False
                for claim in card["claims"]:
                    if any(source["path"] == path for source in claim["sourceStates"]):
                        claim["state"] = "pending_refresh"
                        affected = True
                if affected:
                    card["state"] = "excluded_pending_refresh"
        if state == previous:
            return {"queued": 0, "reason": "duplicate_hint"}
        state["revision"] += 1
        archive = root / HISTORY / f"{previous['revision']}.json"
        if archive.exists():
            if json.loads(archive.read_text()) != previous:
                raise ValueError("history_conflict")
        else:
            _atomic(archive, previous)
        _atomic(root / STATE, state)
        return {"queued": len(set(state["queue"]) - old_pending),
                "pending": len(state["queue"]), "reason": "hint_recorded", "revision": state["revision"]}


def _commit(root, previous, state):
    state["revision"] = previous["revision"] + 1
    archive = root / HISTORY / f"{previous['revision']}.json"
    if archive.exists():
        if json.loads(archive.read_text()) != previous:
            raise ValueError("history_conflict")
    else:
        _atomic(archive, previous)
    _atomic(root / STATE, state)


def start_batch(root, source_roots, cards, *, trusted_card_ids=(), skip_paths=(),
                turn_key_value="recovery", bootstrap_unclaimed=False):
    """Turn-boundary handoff: coalesce final on-disk state and claim one batch."""
    root = sandbox(root)
    reconcile(root, source_roots, cards, trusted_card_ids=trusted_card_ids,
              skip_paths=skip_paths, bootstrap_unclaimed=bootstrap_unclaimed)
    lock = root / ".topical-reconcile.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_lock_symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        if previous["inFlight"]:
            return {"started": False, "reason": "batch_running", "batch": previous["inFlight"]}
        held = previous.get("heldSources", {})
        selectable = {path: row for path, row in previous["queue"].items()
                      if path not in held or held[path] != row.get("currentSha256")}
        if not selectable:
            return {"started": False, "reason": "no_net_changes"}
        # The batch is a handoff record, not an invocation of a model. A future
        # worker must recheck these hashes before accepting generated results.
        batch = {"batchId": digest({"revision": previous["revision"], "turnKey": turn_key_value,
                                     "settled": previous["settledSources"],
                                     "current": previous["sources"]}),
                 "turnKey": turn_key_value,
                 "sources": selectable, "snapshot": dict(previous["sources"])}
        state = json.loads(canonical(previous))
        state["inFlight"] = batch
        _commit(root, previous, state)
        return {"started": True, "batch": batch}


def park_created_batch(root, source_roots, batch_id, source_path, source_sha,
                       *, skip_paths=()):
    """Release only a semantically held create source; retain its queue entry."""
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    source_path = safe_rel(source_path, roots=roots)
    lock = root / '.topical-reconcile.lock'
    if lock.is_symlink():
        raise ValueError('unsafe_lock_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        batch = previous.get('inFlight') if previous else None
        if not batch or batch['batchId'] != batch_id or source_path not in batch['sources']:
            raise ValueError('created_batch_not_in_flight')
        current = _scan(root, roots, skip_paths)
        if (current != batch['snapshot'] or current[source_path]['sha256'] != source_sha
                or previous['queue'].get(source_path, {}).get('currentSha256') != source_sha):
            raise ValueError('source_changed_during_backend')
        state = json.loads(canonical(previous))
        state['inFlight'] = None
        state.setdefault('heldSources', {})[source_path] = source_sha
        _commit(root, previous, state)
        return {'outcome': 'created_source_held', 'pending': len(state['queue'])}


def safe_projection(root, source_roots, cards, *, trusted_card_ids=(), skip_paths=()):
    """Next-read gate: recheck disk before exposing any prior-attested card."""
    result = reconcile(root, source_roots, cards, trusted_card_ids=trusted_card_ids,
                       skip_paths=skip_paths)
    return result["state"]["projection"]


def same_generation_projection(root, generation_id, card_id, observed, fresh):
    """Accept only a matching SessionStart view of this published generation."""
    pending = pending_projection(fresh)
    wiki = root / '.auto-context/wiki/topical-v2'
    published = list(wiki.glob('*/' + card_id + '.md'))
    expected = wiki / generation_id / (card_id + '.md')
    return observed in (fresh, pending) and published == [expected]


def pending_projection(row):
    pending = json.loads(canonical(row))
    pending['state'] = 'excluded_pending_refresh'
    for claim in pending['claims']:
        claim['state'] = 'pending_refresh'
    return pending


def finish_mock_batch(root, source_roots, batch_id, *, success=True, skip_paths=()):
    """Synthetic worker completion; never publishes a card or clears stale gate."""
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    lock = root / ".topical-reconcile.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_lock_symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        batch = previous["inFlight"] if previous else None
        if not batch or batch["batchId"] != batch_id:
            raise ValueError("batch_not_in_flight")
        current = _scan(root, roots, skip_paths)
        state = json.loads(canonical(previous))
        state["inFlight"] = None
        if current != batch["snapshot"]:
            # Edits during work invalidate the batch. Keep its old baseline;
            # next completion or SessionStart reconcile derives the new delta.
            state["sources"] = current
            state["queue"] = _pending(state["settledSources"], current)
            for card in state["projection"].values():
                card["state"] = "excluded_pending_refresh"
                for claim in card["claims"]:
                    claim["state"] = "pending_refresh"
            outcome = "superseded_source_changed"
        elif not success:
            outcome = "mock_failed_retry_pending"
        else:
            state["settledSources"] = current
            state["queue"] = {}
            state["awaitingVerification"].append({"batchId": batch_id,
                                                   "sources": batch["sources"]})
            outcome = "mock_completed_awaiting_real_generation_verification"
        _commit(root, previous, state)
        return {"outcome": outcome, "pending": len(state["queue"]),
                "awaitingVerification": len(state["awaitingVerification"])}


def supersede_changed_batch(root, source_roots, batch_id, *, skip_paths=()):
    """Release an unstarted handoff only after its source snapshot changed.

    Callers must hold their own backend-operation lock and prove that no
    generation journal owns this batch. A matching snapshot is never released:
    another worker may still be about to claim it.
    """
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    lock = root / '.topical-reconcile.lock'
    if lock.is_symlink():
        raise ValueError('unsafe_lock_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        batch = previous['inFlight'] if previous else None
        if not batch or batch['batchId'] != batch_id or previous['sourceRoots'] != roots:
            raise ValueError('batch_not_in_flight')
        current = _scan(root, roots, skip_paths)
        if current == batch['snapshot']:
            return False
        state = json.loads(canonical(previous))
        state['inFlight'] = None
        state['sources'] = current
        state['queue'] = _pending(state['settledSources'], current)
        for card in state['projection'].values():
            card['state'] = 'excluded_pending_refresh'
            for claim in card['claims']:
                claim['state'] = 'pending_refresh'
        _commit(root, previous, state)
        return True


def finish_backend_batch(root, source_roots, batch_id, generation_id, new_cards,
                         *, old_card_id=None, old_card_ids=None,
                         settled_paths=None, skip_paths=(),
                         replacement_lineage=None, old_cards=None,
                         primary_old_id=None, surviving_paths=None,
                         old_generation_ids=None):
    """Settle a batch only after backend attestations and isolated QMD sync.

    The caller must have published each replacement and retired stale pages.
    This method rechecks the complete source snapshot and every proof before
    changing the durable baseline; a fake completion never reaches this path.
    """
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    validated = _cards(new_cards, roots)
    import wiki_topical_publish as publisher
    import wiki_topical_experiment as experiment
    for card_id in validated:
        if experiment.load_staged_card(root, generation_id, card_id) != validated[card_id]:
            raise ValueError('replacement_card_identity_mismatch')
        publisher._attested(root, generation_id, card_id)
        publisher.publish(root, generation_id, card_id)
    publisher.sync(root, allow_empty=not validated, allow_stale=True)
    lock = root / '.topical-reconcile.lock'
    if lock.is_symlink():
        raise ValueError('unsafe_lock_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        batch = previous['inFlight'] if previous else None
        if not batch or batch['batchId'] != batch_id:
            raise ValueError('batch_not_in_flight')
        current = _scan(root, roots, skip_paths)
        if current != batch['snapshot']:
            raise ValueError('source_changed_during_backend')
        fresh = _projection(validated, current, set(validated))
        if any(row['state'] != 'eligible_existing_attestation'
               for row in fresh.values()):
            raise ValueError('replacement_source_stale')
        old_ids = (list(old_card_ids) if old_card_ids is not None else
                   [old_card_id or (next(iter(previous['projection']))
                                    if len(previous['projection']) == 1 else None)])
        if not old_ids or len(old_ids) != len(set(old_ids)):
            raise ValueError('unscoped_backend_batch')
        if replacement_lineage is not None:
            import wiki_topical_replacement as replacement
            if (not isinstance(old_cards, list) or
                    not isinstance(primary_old_id, str) or
                    not isinstance(surviving_paths, list)):
                raise ValueError('replacement_lineage_missing')
            try:
                baseline = _cards(old_cards, roots)
                actual_lineage = replacement.derive(root, old_cards,
                    new_cards, _projection(baseline, current, set(baseline)),
                    surviving_paths,
                    batch['snapshot'], primary_old_id)
            except (topical.TopicalError, OSError, KeyError, TypeError) as exc:
                raise ValueError('replacement_lineage_changed') from exc
            if (actual_lineage != replacement_lineage or old_ids !=
                    [link['oldCardId'] for link in actual_lineage['links']]):
                raise ValueError('replacement_lineage_changed')
            if (not isinstance(old_generation_ids, dict) or
                    set(old_generation_ids) != set(old_ids)):
                raise ValueError('replacement_retirement_missing')
            wiki = root / '.auto-context/wiki/topical-v2'
            for card_id in old_ids:
                old_generation = old_generation_ids[card_id]
                if (not isinstance(old_generation, str) or
                        not experiment.GENERATION_ID.fullmatch(old_generation)):
                    raise ValueError('replacement_retirement_missing')
                archive = root / 'topical-retired' / old_generation / (card_id + '.md')
                old_page = wiki / old_generation / (card_id + '.md')
                if archive.is_symlink() or not archive.is_file() or old_page.exists():
                    raise ValueError('replacement_retirement_missing')
                observed = previous['projection'].get(card_id)
                if observed is None:
                    if list(wiki.glob('*/' + card_id + '.md')):
                        raise ValueError('replacement_card_id_conflict')
                    continue
                old_stale = _projection({card_id: baseline[card_id]}, current,
                                        set())[card_id]
                if (old_stale['state'] == 'excluded_stale' and
                        observed in (old_stale, pending_projection(old_stale))):
                    if card_id not in validated and list(wiki.glob('*/' + card_id + '.md')):
                        raise ValueError('replacement_card_id_conflict')
                    continue
                if (card_id not in validated or
                        not same_generation_projection(root, generation_id,
                            card_id, observed, fresh[card_id])):
                    raise ValueError('replacement_card_id_conflict')
        elif validated and not set(old_ids) <= set(validated):
            raise ValueError('replacement_card_id_mismatch')
        elif any(card_id not in previous['projection'] or
                 previous['projection'][card_id]['state'] != 'excluded_stale'
                 for card_id in old_ids):
            raise ValueError('unscoped_backend_batch')
        overlap = set(validated) & (set(previous['projection']) - set(old_ids))
        if overlap:
            # SessionStart may read the newly published pages while this
            # refresh still owns the source batch. With a pending queue it
            # can downgrade an otherwise identical new row to pending. Only
            # that exact projection from this generation may be adopted.
            for card_id in overlap:
                if not same_generation_projection(root, generation_id, card_id,
                        previous['projection'][card_id], fresh[card_id]):
                    raise ValueError('replacement_card_id_conflict')
        paths = set(settled_paths if settled_paths is not None else batch['sources'])
        if not paths <= set(batch['snapshot']) | set(previous['settledSources']):
            raise ValueError('invalid_settled_paths')
        # A shared changed source remains queued until every dependent stale
        # card has been refreshed. Do not erase another card's projection.
        remaining = {key: value for key, value in previous['projection'].items()
                     if key not in old_ids and key not in overlap}
        referenced_elsewhere = {source['path'] for row in remaining.values()
            if row['state'] != 'eligible_existing_attestation'
            for claim in row['claims'] for source in claim['sourceStates']}
        settled = dict(previous['settledSources'])
        for path in paths - referenced_elsewhere:
            if path in current:
                settled[path] = current[path]
            else:
                settled.pop(path, None)
        projection = {**remaining, **fresh}
        state = json.loads(canonical(previous))
        state.update(inFlight=None, settledSources=settled, sources=current,
                     queue=_pending(settled, current), projection=projection,
                     activeGeneration=generation_id, generatedProjection=projection,
                     lastCompletedBatchId=batch_id)
        _commit(root, previous, state)
        return {'outcome': 'backend_verified_synced', 'cards': len(validated),
                'generationId': generation_id, 'pending': len(state['queue'])}


def finish_created_batch(root, source_roots, batch_id, generation_id, cards, source_path,
                         *, skip_paths=()):
    """Settle one newly claimed source after every card is published and embedded."""
    root = sandbox(root)
    roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
    source_path = safe_rel(source_path, roots=roots)
    if not _within_roots(source_path, roots):
        raise ValueError('created_source_outside_roots')
    validated = _cards(cards, roots)
    if not validated or any({row['path'] for row in card['sourceRevisions']} != {source_path}
                            for card in validated.values()):
        raise ValueError('created_card_source_mismatch')
    import wiki_topical_publish as publisher
    import wiki_topical_experiment as experiment
    for card_id, card in validated.items():
        if experiment.load_staged_card(root, generation_id, card_id) != card:
            raise ValueError('created_card_identity_mismatch')
        publisher._attested(root, generation_id, card_id)
        if not (root / '.auto-context/wiki/topical-v2' / generation_id /
                (card_id + '.md')).is_file():
            raise ValueError('created_card_not_published')
    lock = root / '.topical-reconcile.lock'
    if lock.is_symlink(): raise ValueError('unsafe_lock_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _read(root)
        batch = previous['inFlight'] if previous else None
        if not batch or batch['batchId'] != batch_id or source_path not in batch['sources']:
            raise ValueError('created_batch_not_in_flight')
        current = _scan(root, roots, skip_paths)
        if current != batch['snapshot'] or source_path not in current:
            raise ValueError('source_changed_during_backend')
        fresh = _projection(validated, current, set(validated))
        if any(row['state'] != 'eligible_existing_attestation' for row in fresh.values()):
            raise ValueError('created_source_stale')
        overlap = set(validated) & set(previous['projection'])
        if overlap:
            # A SessionStart reconciliation may observe these published pages
            # before this in-flight create settles. Accept only the identical
            # projection from this generation; any other ID reuse stays fatal.
            wiki = root / '.auto-context/wiki/topical-v2'
            for card_id in overlap:
                published = list(wiki.glob('*/' + card_id + '.md'))
                expected = wiki / generation_id / (card_id + '.md')
                if (previous['projection'][card_id] != fresh[card_id]
                        or published != [expected]):
                    raise ValueError('created_card_id_conflict')
        settled = dict(previous['settledSources'])
        settled[source_path] = current[source_path]
        state = json.loads(canonical(previous))
        state.update(inFlight=None, settledSources=settled, sources=current,
                     queue=_pending(settled, current),
                     projection={**previous['projection'], **fresh},
                     activeGeneration=generation_id,
                     lastCompletedBatchId=batch_id)
        _commit(root, previous, state)
        return {'outcome': 'backend_created_synced', 'cards': len(validated),
                'generationId': generation_id, 'pending': len(state['queue'])}


def rollback(root, revision, source_roots, cards, *, trusted_card_ids=(), skip_paths=()):
    """Restore a previous snapshot only when sources still match it exactly."""
    root = sandbox(root)
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid_revision")
    lock = root / ".topical-reconcile.lock"
    if lock.is_symlink():
        raise ValueError("unsafe_lock_symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        candidate = root / HISTORY / f"{revision}.json"
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("history_unavailable")
        old = json.loads(candidate.read_text())
        roots = sorted(set(safe_rel(p, explicit_root=True) for p in source_roots))
        validated = _cards(cards, roots)
        if old.get("sourceRoots") != roots or _scan(root, roots, skip_paths) != old.get("sources"):
            raise ValueError("rollback_source_mismatch")
        current = _read(root)
        if current is None or current["revision"] <= revision or current["inFlight"]:
            raise ValueError("invalid_rollback_target")
        if old["projection"] != _projection(validated, old["sources"], set(trusted_card_ids)):
            raise ValueError("rollback_card_mismatch")
        _atomic(root / STATE, old)
        return old


def main(argv=None):
    parser = argparse.ArgumentParser(description="Isolated topical source queue; no model calls")
    parser.add_argument("action", choices=("event", "reconcile", "boundary", "finish-mock", "rollback"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--source-root", action="append")
    parser.add_argument("--settings-json")
    parser.add_argument("--cards-json")
    parser.add_argument("--trusted-card", action="append", default=[])
    parser.add_argument("--skip-path", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-id")
    parser.add_argument("--revision", type=int)
    args = parser.parse_args(argv)
    root = sandbox(args.root)
    if args.settings_json:
        source = Path(args.settings_json)
        settings_file = source.resolve()
        if not settings_file.is_relative_to(root) or source.is_symlink():
            raise ValueError("settings_file_outside_sandbox")
        settings = json.loads(settings_file.read_text())
        if (not isinstance(settings, dict) or
                set(settings) != {"sourceRoots", "cardsFile", "trustedCardIds", "skipPaths"}
                or not isinstance(settings["sourceRoots"], list)
                or not isinstance(settings["cardsFile"], str)
                or not isinstance(settings["trustedCardIds"], list)
                or not isinstance(settings["skipPaths"], list)):
            raise ValueError("invalid_sandbox_settings")
        args.source_root = settings["sourceRoots"]
        args.cards_json = str(root / settings["cardsFile"])
        args.trusted_card = settings["trustedCardIds"]
        args.skip_path = settings["skipPaths"]
    if not args.source_root:
        raise ValueError("explicit_source_roots_required")
    cards = []
    if args.cards_json:
        original_file = Path(args.cards_json)
        card_file = original_file.resolve()
        if not card_file.is_relative_to(root) or original_file.is_symlink():
            raise ValueError("cards_file_outside_sandbox")
        cards = json.loads(card_file.read_text())
    if args.action == "event":
        outcome = enqueue_hook_hint(root, args.source_root, json.load(sys.stdin),
                                    skip_paths=args.skip_path)
    elif args.action == "reconcile":
        outcome = reconcile(root, args.source_root, cards,
                            trusted_card_ids=args.trusted_card, skip_paths=args.skip_path,
                            dry_run=args.dry_run)
        outcome = {"changed": outcome["changed"], "revision": outcome["state"]["revision"],
                   "pending": len(outcome["state"]["queue"]),
                   "excluded": sum(row["state"].startswith("excluded") for row in
                                   outcome["state"]["projection"].values())}
    elif args.action == "boundary":
        payload = json.load(sys.stdin)
        if payload.get("hook_event_name") != "Stop":
            raise ValueError("supported_stop_event_required")
        outcome = start_batch(root, args.source_root, cards,
                              trusted_card_ids=args.trusted_card, skip_paths=args.skip_path,
                              turn_key_value=turn_key(payload))
    elif args.action == "finish-mock":
        outcome = finish_mock_batch(root, args.source_root, args.batch_id,
                                    skip_paths=args.skip_path)
    else:
        outcome = rollback(root, args.revision, args.source_root, cards,
                           trusted_card_ids=args.trusted_card, skip_paths=args.skip_path)
        outcome = {"restoredRevision": outcome["revision"]}
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
