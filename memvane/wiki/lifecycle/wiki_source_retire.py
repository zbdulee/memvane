"""Recoverable selected-wiki retirement after a proved raw source deletion."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from memvane.wiki.card.api import archive_card
from memvane.wiki.workspace import api as cp
from memvane.settings import types as project_types
from memvane.wiki.card import api as wiki_card
from memvane.wiki.workspace import api as workspace_files
from memvane.wiki.workspace import api as wiki_selected_laya
from memvane.wiki.card import api as yaml_scalars
from memvane.settings.api import is_wiki_meta_page

from memvane.wiki.card.types import AUTO_START_RE, FRONTMATTER_RE, MAX_CARDS
MAX_CARD_BYTES = 5_000_000
MAX_TOTAL_BYTES = 24_000_000




def _is_auto_writable_page(path: Path):
    try:
        return wiki_card.auto_writable_text(path.read_text(encoding="utf-8"))
    except OSError:
        return False, ["unreadable_target"]


@workspace_files.guard_path
def _insert_source_revisions(path: Path, revisions: list) -> bool:
    return wiki_card.insert_source_revisions(path, revisions)


_LINEAGE_CACHE: dict = {}
LINEAGE_CACHE_SECONDS = 60.0


def _lineage_for(root: Path):
    """``(judge|None, hold_reason|None)``, reused per process while HEAD holds still.

    The compile worker retires one ``sourceDelete`` row per call; one judge
    keeps its HEAD tree and branch-tip lookups bounded across those rows.
    """
    import time
    from memvane.wiki.lifecycle import git_branch_safety
    cached = _LINEAGE_CACHE.get(str(root))
    if (cached is not None and time.monotonic() - cached[1] < LINEAGE_CACHE_SECONDS
            and cached[0].still_valid()):
        return cached[0], None
    judge, reason = git_branch_safety.deletion_gate(root)
    if judge is not None:
        _LINEAGE_CACHE[str(root)] = (judge, time.monotonic())
    else:
        _LINEAGE_CACHE.pop(str(root), None)
    return judge, reason


def lineage_hold(lineage, rel: str, cards) -> str | None:
    """First reason any of ``cards`` must keep ``rel``; None when all may retire."""
    if lineage is None:
        return None
    for card in cards:
        verdict, reason = lineage.judge(rel, card)
        if verdict != 'retire':
            return reason
    return None


def retire_deleted_source(root: Path, config: dict, selected, collection: str,
                          rel: str, prior_hash: str, *, lineage=None) -> dict:
    """Archive single-source machine cards; defer shared cards for regeneration.

    No source bytes or model call are needed. A shared card is never unlinked
    while it may still be the only completed account of another live source.
    The caller retains its queue row on ``shared_pending`` or ``blocked``.

    In a git checkout the absence must be a deletion in HEAD's lineage for
    every citing card (``git_branch_safety.LineageJudge``); a source that is
    only invisible on this branch, a linked worktree, an in-progress
    rebase/merge/cherry-pick/bisect or an unanswerable git returns
    ``held_branch`` and nothing is retired. ``lineage`` is the caller's
    per-run judge; without it one is opened here.
    """
    root = Path(root).resolve()
    source = root / rel
    if source.is_symlink() or source.exists():
        return {'state': 'source_reappeared', 'survivors': []}
    if lineage is None:
        lineage, hold = _lineage_for(root)
        if hold is not None:
            return {'state': 'held_branch', 'survivors': [], 'reason': hold}
    elif not lineage.still_valid():
        return {'state': 'held_branch', 'survivors': [], 'reason': 'git_state_changed'}
    wiki_root = (root / config.get('wikiPath', project_types.WIKI_REL)).resolve()
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return {'state': 'blocked', 'survivors': []}
    from memvane.settings import api as source_label
    label = source_label.resolver(root, config)

    def key(item):
        # A renamed raw collection's old label resolves by path (recall rule).
        return label(item['collection'], item['path']), item['path']
    deleted = key({'collection': collection, 'path': rel})
    with cp.card_write_lock(root), workspace_files.mutation_lock(root):
        wiki_selected_laya.recheck(selected)
        cards = sorted(wiki_root.rglob('*.md'))
        if len(cards) > MAX_CARDS:
            return {'state': 'blocked', 'survivors': []}
        total = 0
        retiring = []
        shared_cards = []
        observed = []
        for card in cards:
            subpath = card.relative_to(wiki_root)
            if (any(part.startswith('.') for part in subpath.parts)
                    or is_wiki_meta_page(subpath)):
                continue
            if card.is_symlink() or card.resolve() != card:
                return {'state': 'blocked', 'survivors': []}
            info = card.stat()
            total += info.st_size
            if info.st_size > MAX_CARD_BYTES or total > MAX_TOTAL_BYTES:
                return {'state': 'blocked', 'survivors': []}
            text = card.read_text(encoding='utf-8')
            match = FRONTMATTER_RE.match(text)
            if match is None:
                continue
            revisions = wiki_card.frontmatter_source_revisions(match.group(1))
            normalized = [yaml_scalars.normalize_source_revision(item) for item in revisions]
            observed.append((card, text, normalized))
            # A source may have several historical card revisions. The deleted
            # path, not only the last observed revision, is retired once absence
            # is rechecked under this mutation lock.
            if not any(item is not None and key(item) == deleted
                       for item in normalized):
                continue
            writable, findings = _is_auto_writable_page(card)
            if not writable and 'non_qmd_created_by' in findings:
                # A human-maintained page is not owned by this retirement flow.
                continue
            if not writable or any(item is None for item in normalized):
                return {'state': 'blocked', 'survivors': []}
            if len(normalized) > 1:
                shared_cards.append((card, text, normalized))
                continue
            retiring.append((card, text))

        hold = lineage_hold(lineage, rel, [card for card, _ in retiring] +
                            [card for card, _, _ in shared_cards])
        if hold is not None:
            return {'state': 'held_branch', 'survivors': [], 'reason': hold}
        shared = set()
        if shared_cards:
            from memvane.wiki.card.api import verified_wiki_row
            wiki_name = next((item['name'] for item in selected[0]['collections']
                              if item['role'] == 'wiki'
                              and Path(item['sourceRoot']).resolve() == wiki_root), None)
            if wiki_name is None:
                return {'state': 'blocked', 'survivors': []}
            for old_card, old_text, revisions in shared_cards:
                survivors = [item for item in revisions
                             if key(item) != deleted
                             and ((root / item['path']).exists()
                                  or (root / item['path']).is_symlink())]
                def covered(revision):
                    return any(
                        candidate != old_card and all(item is not None for item in proof)
                        and any(key(item) == key(revision) for item in proof)
                        and not any(key(item) == deleted for item in proof)
                        and verified_wiki_row({
                            'path': wiki_name + '/' + candidate.relative_to(wiki_root).as_posix(),
                            'body': body}, selected[0], current_source=True)
                        for candidate, body, proof in observed)
                if all(covered(item) for item in survivors):
                    retiring.append((old_card, old_text))
                else:
                    shared.update(key(item) for item in survivors)
        if shared:
            return {'state': 'shared_pending', 'survivors': sorted(shared)}
        if source.is_symlink() or source.exists():
            return {'state': 'source_reappeared', 'survivors': []}
        for card, text in retiring:
            before = card.lstat()
            if (not stat.S_ISREG(before.st_mode) or card.is_symlink()
                    or card.read_text(encoding='utf-8') != text):
                return {'state': 'blocked', 'survivors': []}
            if not archive_card(root, card, text, prior_hash):
                return {'state': 'blocked', 'survivors': []}
            after = card.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                return {'state': 'blocked', 'survivors': []}
            card.unlink()
            directory = os.open(card.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _record_retired(root, config, selected, card, text, 'source_deleted')
    # Also reconciles an old sidecar row whose canonical card disappeared.
    wiki_selected_laya.request_sync(root, selected)
    return {'state': 'retired', 'survivors': []}


def retire_card(root: Path, wiki_root: Path, selected, card: Path,
                expected: str, proof_hash: str) -> str:
    """Archive and unlink one machine card whose bytes are still ``expected``.

    The SessionStart lifecycle uses this for a card it already judged; the
    judgement is repeated here under the card and wiki locks so a concurrent
    compiler rewrite or a human edit is never retired. Symlinks, dot paths,
    wiki meta pages and paths outside ``wiki_root`` are refused. The caller
    requests one laya-sync wake for the whole run.
    """
    root = Path(root).resolve()
    wiki_root = Path(wiki_root)
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return 'blocked'
    with cp.card_write_lock(root), workspace_files.mutation_lock(root):
        wiki_selected_laya.recheck(selected)
        try:
            subpath = card.relative_to(wiki_root)
        except ValueError:
            return 'blocked'
        if (any(part.startswith('.') for part in subpath.parts)
                or is_wiki_meta_page(subpath)):
            return 'blocked'
        try:
            before = card.lstat()
        except FileNotFoundError:
            return 'gone'
        if (not stat.S_ISREG(before.st_mode) or card.is_symlink()
                or card.resolve() != card or before.st_size > MAX_CARD_BYTES):
            return 'blocked'
        try:
            text = card.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return 'blocked'
        if text != expected:
            return 'changed'
        if not wiki_card.auto_writable_text(text)[0]:
            return 'blocked'
        if not archive_card(root, card, text, proof_hash):
            return 'blocked'
        after = card.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return 'blocked'
        card.unlink()
        directory = os.open(card.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    _record_retired(root, None, selected, card, text, proof_hash, wiki_root=wiki_root)
    return 'retired'


def _record_retired(root, config, selected, card, text, reason, *, wiki_root=None) -> None:
    """One ``wiki`` retired event; never fails the retirement."""
    try:
        if wiki_root is None:
            wiki_root = (root / (config or {}).get('wikiPath',
                                                   project_types.WIKI_REL)).resolve()
        rel = Path(card).relative_to(wiki_root).as_posix()
        meta, _ = wiki_card.parse_frontmatter(text)
        title = str(meta.get('title') or Path(card).stem)
        workspace_files.append_activity(root, 'retired', rel, title, bound=selected, reason=reason)
    except Exception:
        pass


def record_source_revisions(root: Path, wiki_root: Path, selected, card: Path,
                            expected: str, revisions: list) -> str:
    """Record ``revisions`` as the source proof of one card whose bytes are still ``expected``.

    This is the single write seam for "card X was generated from these source
    revisions". Today the proof lives in the card's ``sourceRevisions``
    frontmatter block (``wiki_card.insert_source_revisions``, which refuses
    a card that already declares the key or has no ``sources``); moving the
    proof to another store replaces only this function. The same card and wiki
    locks, path checks, byte comparison and ownership recheck as
    ``retire_card`` guard the write, so a concurrent compiler rewrite or a
    human edit is never stamped.

    A schema3-selected install records nothing (``schema3_selected``): the
    rewritten card would be a new blob whose provenance no store holds.
    """
    root = Path(root).resolve()
    wiki_root = Path(wiki_root)
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return 'blocked'
    with cp.card_write_lock(root), workspace_files.mutation_lock(root):
        wiki_selected_laya.recheck(selected)
        if selected is not None and 'journalPath' in selected[0]:
            return 'schema3_selected'
        try:
            subpath = card.relative_to(wiki_root)
        except ValueError:
            return 'blocked'
        if (any(part.startswith('.') for part in subpath.parts)
                or is_wiki_meta_page(subpath)):
            return 'blocked'
        try:
            info = card.lstat()
        except FileNotFoundError:
            return 'gone'
        if (not stat.S_ISREG(info.st_mode) or card.is_symlink()
                or card.resolve() != card or info.st_size > MAX_CARD_BYTES):
            return 'blocked'
        try:
            text = card.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return 'blocked'
        if text != expected:
            return 'changed'
        if not wiki_card.auto_writable_text(text)[0]:
            return 'blocked'
        if not _insert_source_revisions(card, revisions):
            return 'blocked'
    return 'recorded'
