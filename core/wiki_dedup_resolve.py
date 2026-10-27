#!/usr/bin/env python3
"""Resolve one entry in the retroactive wiki dedup queue.

Reads .auto-context/compile/dedup-needed.jsonl, applies one action to the
entry at --index, and rewrites the queue with that entry removed. Never
touches entries other than the one resolved this run.

This script's `merge` action DELETES a file. The caller (the
wiki-dedup-resolver subagent) has already folded any unique content into the
page it is keeping via its own Edit tool before invoking this CLI.
"""
from __future__ import annotations
import argparse
import json
import os
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import compile_paths as cp
import config as qmd_config
import wiki_mutation_lock
import wiki_compile as wc
import wiki_dedup_scan as dedup_scan
import wiki_selected_laya
from wiki_compile_worker import claim_queue, requeue_lines

ACTIONS = {"merge", "skip"}


def enqueue_project_collections(project_root, selected):
    """Queue a QMD reindex for a project that has no selected Laya route.

    The import stays inside this function so the selected path never loads
    the QMD queue modules.
    """
    from dirty_queue import enqueue_project_collections as enqueue
    enqueue(project_root, selected)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_entries(claimed: Path) -> list[tuple[str, dict | None]]:
    if not claimed.exists():
        return []
    rows = []
    try:
        content = claimed.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            rows.append((line, None))
            continue
        rows.append((line, parsed if isinstance(parsed, dict) else None))
    return rows


def record_skip(root: Path, wiki_root: Path, compile_dir: Path, entry: dict) -> bool:
    """Append the skip judgment to dedup-skipped.jsonl so the scanner can
    suppress re-queueing this pair while both bodies are unchanged.

    Returns False -- recording NOTHING -- for stale skips: either page
    missing, path-unsafe, or unreadable. A stale skip is not a content
    judgment, and recording one would create a bogus permanent suppression.
    Recording failure never fails the skip itself.

    The hashes are computed HERE, by the CLI, at skip time -- never supplied
    by the resolver agent (an agent-supplied hash would be nondeterministic).

    An unwritable ledger returns False instead of propagating OSError. Same class as
    the verify workers' ledgers: without the row the scanner re-queues this pair and
    the judge re-bills it, so the failure must be visible (`recorded: false` in the
    CLI's JSON) rather than a traceback that loses the rest of the resolve.
    """
    page_a = entry.get("pageA")
    page_b = entry.get("pageB")
    if not (isinstance(page_a, str) and isinstance(page_b, str)) or page_a == page_b:
        return False
    texts: dict[str, str] = {}
    for rel in (page_a, page_b):
        target = (wiki_root / rel).resolve()
        try:
            target.relative_to(wiki_root)
        except ValueError:
            return False
        if not target.is_file():
            return False  # stale skip: no content judgment happened
        try:
            texts[rel] = target.read_text(encoding="utf-8")
        except OSError:
            return False
    skipped_path = cp.ledger(root, cp.DEDUP_SKIPPED)
    if skipped_path is None:
        return False
    first, second = sorted((page_a, page_b))  # order-independent pair key
    try:
        wc.append_jsonl(skipped_path, {
            "pageA": first,
            "pageB": second,
            "pageAHash": dedup_scan.body_hash(texts[first]),
            "pageBHash": dedup_scan.body_hash(texts[second]),
            "skippedAt": now_iso(),
        })
    except OSError:
        return False
    return True


def record_recovery(compile_dir: Path, delete_rel: str, paired_with: str, content: str) -> Path:
    """Keep a non-trimmed, private copy before selected-Laya card deletion."""
    recovery_dir = compile_dir / 'dedup-recovery'
    recovery_dir.mkdir(mode=0o700, exist_ok=True)
    info = recovery_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError('unsafe_dedup_recovery_dir')
    path = recovery_dir / (uuid.uuid4().hex + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump({'deletedPath': delete_rel, 'pairedWith': paired_with,
                       'content': content, 'savedAt': now_iso()}, output,
                      ensure_ascii=False, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(recovery_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def pair_missing(wiki_root: Path, entry: dict) -> bool:
    pages = (entry.get('pageA'), entry.get('pageB'))
    if any(not isinstance(page, str) for page in pages):
        return False
    for page in pages:
        target = (wiki_root / page).resolve()
        try:
            target.relative_to(wiki_root)
        except ValueError:
            return False
        if not target.is_file():
            return True
    return False


def resolve_entry(root: Path, wiki_root: Path, compile_dir: Path, entry: dict, action: str, delete_rel: str | None, *, laya_bound=None) -> dict:
    # Match compiler/verifier/retirement lock order. Queueing a wake performs
    # journal proof checks, so do it only after both card locks are released.
    with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
        if laya_bound is not None:
            wiki_selected_laya.recheck(laya_bound)
        result = _resolve_entry_locked(root, wiki_root, compile_dir, entry, action,
                                       delete_rel, laya_bound=laya_bound)
    sync_needed = result.pop('_syncAfterMutation', False)
    if laya_bound is not None and sync_needed:
        wiki_selected_laya.request_sync(root, laya_bound)
    return result


def _resolve_entry_locked(root: Path, wiki_root: Path, compile_dir: Path, entry: dict, action: str, delete_rel: str | None, *, laya_bound=None) -> dict:
    page_a = entry.get("pageA")
    page_b = entry.get("pageB")
    valid_choices = {p for p in (page_a, page_b) if isinstance(p, str)}

    if action == "skip":
        recorded = record_skip(root, wiki_root, compile_dir, entry)
        # A prior merge may have deleted the loser before its wake could be
        # queued. Stale pairs are retried as skips by the resolver agent.
        stale = laya_bound is not None and not recorded and pair_missing(wiki_root, entry)
        return {"action": "skipped", "recorded": recorded,
                "_syncAfterMutation": stale}

    if action == "merge":
        if delete_rel not in valid_choices:
            return {"action": "rejected", "reason": "delete_not_in_entry"}
        target = (wiki_root / delete_rel).resolve()
        try:
            target.relative_to(wiki_root)
        except ValueError:
            return {"action": "rejected", "reason": "unsafe_delete_path"}
        if not target.is_file():
            return {"action": "skipped", "reason": "stale_target",
                    "_syncAfterMutation": laya_bound is not None}

        paired_with = page_b if delete_rel == page_a else page_a
        content = target.read_text(encoding="utf-8")
        if laya_bound is not None:
            record_recovery(compile_dir, delete_rel, paired_with, content)
        deleted_path = cp.ledger(root, cp.DEDUP_DELETED)
        if deleted_path is not None:
            wc.append_jsonl(deleted_path, {
                "deletedPath": delete_rel,
                "content": content,
                "pairedWith": paired_with,
                "score": entry.get("score"),
                "resolvedAt": now_iso(),
            })
            # write-only audit log storing full deleted-card bodies (KB each) --
            # cap it so it can't grow unbounded.
            wc.trim_jsonl(deleted_path)
        target.unlink()
        # 카드가 사라졌으니 인덱스 줄도 걷는다. 순서는 unlink 다음이다 — 먼저 지웠다가
        # unlink가 실패하면 살아 있는 카드가 인덱스에서만 사라진다(반대 방향의 어긋남).
        # 인덱스는 항법이라 실패에 fail-closed하지 않고 결과에 실어 보낸다.
        meta, _ = wc.parse_frontmatter(content)
        title = str(meta.get("title") or Path(delete_rel).stem)
        index_ok = wc.remove_from_index(wiki_root, target)
        wc.append_log(wiki_root, "deleted", target, title)
        # The caller requests sync after releasing the card locks but before
        # ACKing the dedup queue. A failure requeues this row for stale retry.
        return {"action": "deleted", "deletedPath": delete_rel,
                "indexOk": index_ok, "_syncAfterMutation": laya_bound is not None}

    return {"action": "rejected", "reason": "unknown_action"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--index", type=int)
    parser.add_argument("--action", choices=sorted(ACTIONS))
    parser.add_argument("--delete", default=None)
    parser.add_argument("--recover-queue", action="store_true")
    args = parser.parse_args()
    if not args.recover_queue and (args.index is None or args.action is None):
        parser.error('--index and --action are required for resolution')

    found = qmd_config.find_project_config(args.cwd)
    root = Path(found["projectRoot"]).resolve()
    config = found["config"]
    wiki_rel = config.get("wikiPath", ".auto-context/wiki")
    wiki_root = wc.safe_managed_dir(root, wiki_rel)
    compile_dir = cp.compile_dir(root, create=True)
    if wiki_root is None or compile_dir is None:
        print(json.dumps({"action": "rejected", "reason": "unsafe_managed_path"}, ensure_ascii=False))
        return 1
    queue_path = cp.ledger(root, cp.DEDUP_NEEDED)
    if queue_path is None:
        print(json.dumps({"action": "rejected", "reason": "unsafe_compile_path"}, ensure_ascii=False))
        return 1

    if args.recover_queue:
        # claim_queue reclaims dead-owner *.claimed.* files under the queue
        # lock. Give the currently claimed rows back unchanged before the
        # agent inspects the queue. No card or index is touched here.
        claimed = claim_queue(queue_path)
        if claimed is not None:
            rows = load_entries(claimed)
            requeue_lines(queue_path, [raw for raw, _ in rows])
            claimed.unlink(missing_ok=True)
        print(json.dumps({'action': 'recovered'}, ensure_ascii=False))
        return 0

    if args.action == "merge" and not args.delete:
        print(json.dumps({"action": "rejected", "reason": "missing_delete_arg"}, ensure_ascii=False))
        return 1

    # Resolve the journal-bound route before claiming or mutating a pair. A
    # blocked Laya route must never fall through to the legacy QMD queue.
    laya_bound = wiki_selected_laya.selected(root)
    if laya_bound is not None:
        selected_wiki = next(item['sourceRoot'] for item in laya_bound[0]['collections']
                             if item['role'] == 'wiki')
        if selected_wiki != str(wiki_root.resolve()):
            raise ValueError('selected_dedup_wiki_root_changed')

    claimed = claim_queue(queue_path)
    if claimed is None:
        print(json.dumps({"action": "rejected", "reason": "queue_empty"}, ensure_ascii=False))
        return 1

    rows = load_entries(claimed)
    if not (0 <= args.index < len(rows)):
        requeue_lines(queue_path, [raw for raw, _ in rows])
        claimed.unlink(missing_ok=True)
        print(json.dumps({"action": "rejected", "reason": "index_out_of_range"}, ensure_ascii=False))
        return 1

    raw, entry = rows[args.index]

    if entry is None:
        remaining_raw = [r for i, (r, _) in enumerate(rows) if i != args.index]
        requeue_lines(queue_path, remaining_raw)
        claimed.unlink(missing_ok=True)
        print(json.dumps({"action": "rejected", "reason": "malformed_entry"}, ensure_ascii=False))
        return 1

    # Ordering invariant: resolve_entry() must complete BEFORE this entry is
    # excluded from the requeue, so a crash never loses the entry.
    try:
        result = resolve_entry(root, wiki_root, compile_dir, entry, args.action, args.delete,
                               laya_bound=laya_bound)
    except Exception:
        requeue_lines(queue_path, [r for r, _ in rows])
        claimed.unlink(missing_ok=True)
        raise

    # A "rejected" outcome means the resolution did NOT apply (e.g. --delete
    # failed re-validation) -- the entry must stay in the queue for a retry,
    # unlike "deleted"/"skipped" which did apply and are done with.
    if result.get("action") == "rejected":
        requeue_lines(queue_path, [r for r, _ in rows])
    else:
        remaining_raw = [r for i, (r, _) in enumerate(rows) if i != args.index]
        requeue_lines(queue_path, remaining_raw)
    claimed.unlink(missing_ok=True)

    if result.get("action") == "deleted" and laya_bound is None:
        collection, collection_path = wc.find_wiki_collection(config)
        if collection and collection_path:
            enqueue_project_collections(root, {collection: str((root / collection_path).resolve())})

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("action") != "rejected" else 1


if __name__ == "__main__":
    sys.exit(main())
