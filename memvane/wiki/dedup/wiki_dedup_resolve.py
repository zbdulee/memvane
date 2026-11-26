#!/usr/bin/env python3
"""Resolve one entry in the retroactive wiki dedup queue.

Reads .memvane/compile/dedup-needed.jsonl, applies one action to the
entry at --index, and rewrites the queue with that entry removed. Never
touches entries other than the one resolved this run.

This script's `merge` action DELETES a file. The caller (the
wiki-dedup-resolver subagent) has already folded any unique content into the
page it is keeping via its own Edit tool before invoking this CLI. Before the
delete, the CLI moves the deleted card's canonicalKey, title and aliases into
the keeper's aliases and its sources into the keeper's sources, so a later
compile of the deleted card's source resolves to the keeper. When that move
fails or a moved label already names a third card, nothing is deleted.
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
from memvane.wiki.workspace import api as cp, types as ct
from memvane.settings import api as project_dir, types as project_types
from memvane.settings import api as qmd_config
from memvane.wiki.workspace import api as workspace_files
from memvane.kernel.paths import safe_managed_dir
from contextlib import nullcontext
import hashlib
from memvane.wiki.card import api as cards
from memvane.settings import api as source_text
from memvane.wiki.card.api import parse_frontmatter
from memvane.wiki.card import types as card_types
from memvane.wiki.card.types import FRONTMATTER_RE
from memvane.wiki.dedup import wiki_dedup_scan as dedup_scan
from memvane.wiki.workspace import api as wiki_selected_laya
from memvane.wiki.workspace.api import claim_queue, requeue_lines

ACTIONS = {"merge", "skip"}


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
    skipped_path = qmd_config.ledger(root, ct.DEDUP_SKIPPED)
    if skipped_path is None:
        return False
    first, second = sorted((page_a, page_b))  # order-independent pair key
    try:
        cp.append_jsonl(skipped_path, {
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


def keeper_over_budget(root: Path, wiki_root: Path, keeper_rel) -> dict | None:
    """The keeper's budget measurement when its body is over the card budget, else None."""
    from memvane.learning.api import measure_card, selector_tokenizers
    if not isinstance(keeper_rel, str):
        return None
    keeper = (wiki_root / keeper_rel).resolve()
    try:
        keeper.relative_to(wiki_root)
        text = keeper.read_text(encoding="utf-8")
    except (ValueError, OSError, UnicodeError):
        return None
    tokenizer = None
    try:
        found = qmd_config.find_project_config(str(root))
        tokenizer = selector_tokenizers(found["config"], root)
    except Exception:  # noqa: BLE001 - no tokenizer: measure_card reports unavailable
        tokenizer = None
    measured = measure_card(text, tokenizer)
    # A page the selector does not read at all (no frontmatter) or one no runtime
    # could count is not refused: only a measured overflow keeps the pair apart.
    return measured if measured["reason"] == "over_budget" else None


UNKNOWN_SOURCE = '{kind: unknown}'


def _block_entries(text: str, key: str) -> list[str] | None:
    """Raw ``  - <entry>`` values of one top-level frontmatter block, or None when unreadable."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return []
    sections = cards.frontmatter_sections(match.group(1))
    if sections is None:
        return None
    for name, lines in sections:
        if name != key:
            continue
        if lines[0].rstrip() in (f'{key}:', f'{key}: []') and all(line.startswith('  - ') for line in lines[1:]):
            return [line[4:] for line in lines[1:]]
        return None
    return []


def _label_owners(wiki_root: Path, keys: set[str]) -> set[Path]:
    """Cards whose canonicalKey, title or an alias has one of ``keys`` (compile's identity lookup)."""
    owners = set()
    for page in wiki_root.rglob('*.md'):
        if source_text.is_wiki_meta_page(page, wiki_root):
            continue
        try:
            meta, ok = parse_frontmatter(page.read_text(encoding='utf-8'))
        except (OSError, UnicodeError):
            continue
        if ok and any(card_types.identity_keys(value) & keys
                      for value in card_types.identity_values_from_meta(meta)):
            owners.add(page.resolve())
    return owners


def transfer_labels(root: Path, wiki_root: Path, keeper_rel: str, loser: Path, loser_text: str) -> str | None:
    """Move the loser's identity labels and sources into the keeper; None on success.

    Runs under the card locks the caller holds. The keeper is rewritten through
    ``publish_card`` against the bytes read here, so an edit made meanwhile keeps
    the keeper as it is and refuses the merge.
    """
    keeper = (wiki_root / keeper_rel).resolve()
    try:
        keeper.relative_to(wiki_root)
        raw = keeper.read_bytes()
        text = raw.decode('utf-8')
    except (ValueError, OSError, UnicodeError):
        return 'keeper_unreadable'
    # A page without frontmatter has no labels or sources; one whose frontmatter
    # does not parse cannot be read safely and refuses the merge.
    meta_a, ok_a = parse_frontmatter(text) if FRONTMATTER_RE.match(text) else ({}, True)
    meta_b, ok_b = parse_frontmatter(loser_text) if FRONTMATTER_RE.match(loser_text) else ({}, True)
    if not ok_a:
        return 'keeper_frontmatter_invalid'
    if not ok_b:
        return 'loser_frontmatter_invalid'
    known = set()
    for value in card_types.identity_values_from_meta(meta_a):
        known.add(card_types.normalize_identity(value))
    moved = []
    for value in card_types.identity_values_from_meta(meta_b):
        norm = card_types.normalize_identity(value)
        if norm and norm not in known:
            known.add(norm)
            moved.append(value)
    moved_keys = set()
    for value in moved:
        moved_keys |= card_types.identity_keys(value)
    if moved_keys and _label_owners(wiki_root, moved_keys) - {keeper, loser.resolve()}:
        return 'alias_conflict'
    blocks = {}
    if moved:
        aliases = meta_a.get('aliases')
        current = [a.strip() for a in aliases if isinstance(a, str) and a.strip()] if isinstance(aliases, list) else []
        blocks['aliases'] = [cards.dump(alias) for alias in current + moved]
    sources_a, sources_b = _block_entries(text, 'sources'), _block_entries(loser_text, 'sources')
    if sources_a is None or sources_b is None:
        return 'sources_unreadable'
    added = [entry for entry in sources_b if entry != UNKNOWN_SOURCE and entry not in sources_a]
    if added:
        kept = [entry for entry in sources_a if entry != UNKNOWN_SOURCE]
        blocks['sources'] = kept + added
    if not blocks:
        return None
    if not FRONTMATTER_RE.match(text):
        return 'keeper_frontmatter_missing'
    rewritten = cards.render_frontmatter_fields(text, {}, blocks)
    if rewritten is None or not parse_frontmatter(rewritten)[1]:
        return 'label_transfer_failed'
    from memvane.wiki.pipeline import api as pipeline
    try:
        outcome = pipeline.publish_card(root, wiki_root, keeper.relative_to(wiki_root), rewritten,
                                        expected_target_sha=hashlib.sha256(raw).hexdigest(),
                                        lock_context=nullcontext)
    except (OSError, ValueError):
        return 'label_transfer_failed'
    return None if outcome.state == 'published' else 'label_transfer_failed'


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
    with cp.card_write_lock(root), workspace_files.mutation_lock(root):
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
        refused = keeper_over_budget(root, wiki_root, paired_with)
        if refused is not None:
            # A keeper the local selector cannot score keeps the pair as two
            # cards: the loser stays, and the pair is recorded as skipped.
            recorded = record_skip(root, wiki_root, compile_dir, entry)
            from memvane.learning.api import emit_card_budget
            emit_card_budget(root, "merge_kept_separate", bound=laya_bound,
                             path=paired_with, reason=refused["reason"],
                             size=refused["size"], budget=refused["budget"],
                             method=refused["method"], kept=delete_rel)
            return {"action": "kept_separate", "reason": "keeper_over_budget",
                    "keeper": paired_with, "kept": delete_rel, "recorded": recorded,
                    "size": refused["size"], "budget": refused["budget"],
                    "method": refused["method"]}
        content = target.read_text(encoding="utf-8")
        refusal = transfer_labels(root, wiki_root, paired_with, target, content)
        if refusal is not None:
            # 1번 기준: 이름표가 옮겨지지 않으면 지운 카드의 원문이 새 카드를 다시 만든다.
            return {"action": "rejected", "reason": refusal, "kept": delete_rel}
        if laya_bound is not None:
            record_recovery(compile_dir, delete_rel, paired_with, content)
        # Log row only (working-rules 3): no decision reads it.
        from memvane.wiki.pipeline import api as pipeline
        pipeline.audit_log(root, 'dedup_deleted').append({
            "deletedPath": delete_rel,
            "targetPath": delete_rel,
            "content": content,
            "pairedWith": paired_with,
            "score": entry.get("score"),
            "resolvedAt": now_iso(),
        })
        target.unlink()
        meta, _ = parse_frontmatter(content)
        title = str(meta.get("title") or Path(delete_rel).stem)
        workspace_files.append_activity(root, "deleted", target.relative_to(wiki_root).as_posix(),
                                 title, bound=laya_bound, reason='dedup')
        # The caller requests sync after releasing the card locks but before
        # ACKing the dedup queue. A failure requeues this row for stale retry.
        return {"action": "deleted", "deletedPath": delete_rel,
                "_syncAfterMutation": laya_bound is not None}

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
    wiki_rel = config.get("wikiPath", project_types.WIKI_REL)
    wiki_root = safe_managed_dir(root, wiki_rel)
    compile_dir = project_dir.compile_dir(root, create=True)
    if wiki_root is None or compile_dir is None:
        print(json.dumps({"action": "rejected", "reason": "unsafe_managed_path"}, ensure_ascii=False))
        return 1
    queue_path = qmd_config.ledger(root, ct.DEDUP_NEEDED)
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

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("action") != "rejected" else 1


if __name__ == "__main__":
    sys.exit(main())
