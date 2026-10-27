"""Recoverable selected-wiki retirement after a proved raw source deletion."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import compile_paths as cp
import recall
import wiki_compile as wc
import wiki_mutation_lock
import wiki_selected_laya
import yaml_scalars

MAX_CARDS = 2_048
MAX_CARD_BYTES = 5_000_000
MAX_TOTAL_BYTES = 24_000_000


def archive_card(root: Path, card: Path, text: str, proof_hash: str) -> bool:
    """Durably retain old canonical bytes before retirement or replacement.

    The caller holds the card and wiki mutation locks and has compared the
    canonical bytes. A same-content replay reuses the archived hard link.
    """
    archive = cp.compile_dir(root, create=True)
    if archive is None:
        return False
    archive = archive / 'retired-wiki'
    archive.mkdir(mode=0o700, exist_ok=True)
    info = archive.lstat()
    if (archive.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        return False
    digest = hashlib.sha256((str(card.relative_to(root)) + '\0' +
                             proof_hash + '\0' + text).encode()).hexdigest()
    saved = archive / (digest + '.md')
    try:
        if saved.exists():
            if saved.is_symlink() or saved.read_text(encoding='utf-8') != text:
                return False
        else:
            os.link(card, saved, follow_symlinks=False)
        # The archive is useful only when an operator can locate the original
        # card. Keep that identity beside the immutable bytes on every replay.
        sidecar = archive / (digest + '.json')
        metadata = {'path': card.relative_to(root).as_posix(), 'proofHash': proof_hash}
        if sidecar.exists():
            if sidecar.is_symlink() or json.loads(sidecar.read_text(encoding='utf-8')) != metadata:
                return False
        else:
            fd, temp = tempfile.mkstemp(prefix='.' + digest + '-', dir=archive)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(metadata, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, sidecar)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        directory = os.open(archive, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def retire_deleted_source(root: Path, config: dict, selected, collection: str,
                          rel: str, prior_hash: str) -> dict:
    """Archive single-source machine cards; defer shared cards for regeneration.

    No source bytes or model call are needed. A shared card is never unlinked
    while it may still be the only completed account of another live source.
    The caller retains its queue row on ``shared_pending`` or ``blocked``.
    """
    root = Path(root).resolve()
    source = root / rel
    if source.is_symlink() or source.exists():
        return {'state': 'source_reappeared', 'survivors': []}
    wiki_root = (root / config.get('wikiPath', '.auto-context/wiki')).resolve()
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return {'state': 'blocked', 'survivors': []}
    import source_label
    label = source_label.resolver(root, config)

    def key(item):
        # A renamed raw collection's old label resolves by path (recall rule).
        return label(item['collection'], item['path']), item['path']
    deleted = key({'collection': collection, 'path': rel})
    with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
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
                    or card.name in ('index.md', 'log.md')):
                continue
            if card.is_symlink() or card.resolve() != card:
                return {'state': 'blocked', 'survivors': []}
            info = card.stat()
            total += info.st_size
            if info.st_size > MAX_CARD_BYTES or total > MAX_TOTAL_BYTES:
                return {'state': 'blocked', 'survivors': []}
            text = card.read_text(encoding='utf-8')
            match = wc.FRONTMATTER_RE.match(text)
            if match is None:
                continue
            revisions = recall.frontmatter_source_revisions(match.group(1))
            normalized = [yaml_scalars.normalize_source_revision(item) for item in revisions]
            observed.append((card, text, normalized))
            # A source may have several historical card revisions. The deleted
            # path, not only the last observed revision, is retired once absence
            # is rechecked under this mutation lock.
            if not any(item is not None and key(item) == deleted
                       for item in normalized):
                continue
            writable, findings = wc.is_auto_writable_page(card)
            if not writable and 'non_qmd_created_by' in findings:
                # A human-maintained page is not owned by this retirement flow.
                continue
            if not writable or any(item is None for item in normalized):
                return {'state': 'blocked', 'survivors': []}
            if len(normalized) > 1:
                shared_cards.append((card, text, normalized))
                continue
            retiring.append((card, text))

        shared = set()
        if shared_cards:
            from laya_host_response import _verified_wiki_row
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
                        and _verified_wiki_row({
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
            wc.remove_from_index(wiki_root, card)
            directory = os.open(card.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    # Also reconciles an old sidecar row whose canonical card disappeared.
    wiki_selected_laya.request_sync(root, selected)
    return {'state': 'retired', 'survivors': []}


def retire_card(root: Path, wiki_root: Path, selected, card: Path,
                expected: str, proof_hash: str) -> str:
    """Archive and unlink one machine card whose bytes are still ``expected``.

    The SessionStart lifecycle uses this for a card it already judged; the
    judgement is repeated here under the card and wiki locks so a concurrent
    compiler rewrite or a human edit is never retired. Symlinks, dot paths,
    index/log pages and paths outside ``wiki_root`` are refused. The caller
    requests one laya-sync wake for the whole run.
    """
    root = Path(root).resolve()
    wiki_root = Path(wiki_root)
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return 'blocked'
    with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
        wiki_selected_laya.recheck(selected)
        try:
            subpath = card.relative_to(wiki_root)
        except ValueError:
            return 'blocked'
        if (any(part.startswith('.') for part in subpath.parts)
                or card.name in ('index.md', 'log.md')):
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
        if not wc.auto_writable_text(text)[0]:
            return 'blocked'
        if not archive_card(root, card, text, proof_hash):
            return 'blocked'
        after = card.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return 'blocked'
        card.unlink()
        wc.remove_from_index(wiki_root, card)
        directory = os.open(card.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return 'retired'


def record_source_revisions(root: Path, wiki_root: Path, selected, card: Path,
                            expected: str, revisions: list) -> str:
    """Record ``revisions`` as the source proof of one card whose bytes are still ``expected``.

    This is the single write seam for "card X was generated from these source
    revisions". Today the proof lives in the card's ``sourceRevisions``
    frontmatter block (``wiki_compile.insert_source_revisions``, which refuses
    a card that already declares the key or has no ``sources``); moving the
    proof to another store replaces only this function. The same card and wiki
    locks, path checks, byte comparison and ownership recheck as
    ``retire_card`` guard the write, so a concurrent compiler rewrite or a
    human edit is never stamped.
    """
    root = Path(root).resolve()
    wiki_root = Path(wiki_root)
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        return 'blocked'
    with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
        wiki_selected_laya.recheck(selected)
        try:
            subpath = card.relative_to(wiki_root)
        except ValueError:
            return 'blocked'
        if (any(part.startswith('.') for part in subpath.parts)
                or card.name in ('index.md', 'log.md')):
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
        if not wc.auto_writable_text(text)[0]:
            return 'blocked'
        if not wc.insert_source_revisions(card, revisions):
            return 'blocked'
    return 'recorded'
