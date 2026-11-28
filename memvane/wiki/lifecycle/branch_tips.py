"""Which unmerged local branch tips own a deleted source (``LineageJudge`` 2c/3a).

A branch tip counts unless HEAD's lineage has a deletion commit for the path
and the tip holds the path exactly as every merge base of the tip and HEAD
does (same mode, type and blob): such a tip is a snapshot from before the
deletion, and merging it keeps the deletion. A tip that added the path (no
merge base has it) or changed it still holds, and so does a tip checked out
in another worktree, whose uncommitted edits git cannot see. One ``git log``
over all tips (``^HEAD``) proves the common case: when no commit reachable
only from the tips touches the path, every tip holds it as its merge bases
do. A touched path falls back to per-tip merge bases.
"""
from __future__ import annotations

from pathlib import Path
import unicodedata

MAX_PATHSPECS_PER_CALL = 200


def _nfc(path: str) -> str:
    # macOS git stores precomposed names; a card may record either form.
    return unicodedata.normalize('NFC', path)


def _z_paths(raw: bytes) -> set[str]:
    return {_nfc(item.decode('utf-8', 'surrogateescape')) for item in raw.split(b'\0') if item}


def _z_entries(raw: bytes) -> dict[str, str]:
    """``{path: '<mode> <type> <object>'}`` from ``git ls-tree -z`` (no ``--name-only``)."""
    entries: dict[str, str] = {}
    for item in raw.split(b'\0'):
        meta, sep, name = item.partition(b'\t')
        if sep and name:
            entries[_nfc(name.decode('utf-8', 'surrogateescape'))] = meta.decode('ascii', 'replace')
    return entries


class BranchTips:
    """Branch-tip lookups of one ``LineageJudge`` run, cached per run.

    The judge supplies ``_git`` (run deadline), ``_chunks``, ``_unmerged_tips``,
    ``_deletion_commit``, ``prefetch``, ``head``, ``top`` and the caches its
    ``__init__`` sets (``_paths``, ``_tip_entries``, ``_touched``, ``_busy``,
    ``_bases``, ``_base_entries``).
    """

    def _on_other_branch(self, path: str) -> bool:
        """An unmerged branch tip that may still own ``path`` (rules 2c/3a)."""
        tips = self._unmerged_tips()
        if tips is None:
            return True
        if path not in self._paths:
            self.prefetch([path])
        if self._deletion_commit(path) is None:
            return any(self._tip_entry(tip, path) is not None for tip in tips)
        busy = self._busy_tips()
        if any(self._tip_entry(tip, path) is not None for tip in tips if tip in busy):
            return True
        if path not in self._touched_on_tips():
            return False
        return any(self._tip_entry(tip, path) is not None
                   and not self._unchanged_since_base(tip, path)
                   for tip in self._touching_tips(path))

    def _tip_entry(self, tip: str, path: str) -> str | None:
        """``path``'s entry in ``tip``; one lookup covers every registered path."""
        entries = self._tip_entries.setdefault(tip, {})
        if path not in entries:
            wanted = (self._paths | {path}) - set(entries)
            for chunk in self._chunks(wanted):
                found = _z_entries(self._git(['ls-tree', '-r', '-z', tip, '--', *chunk]).stdout)
                entries.update({item: found.get(item) for item in chunk})
        return entries[path]

    def _touched_on_tips(self) -> set[str]:
        """Registered paths some commit reachable from a tip but not HEAD changes.

        ``--full-history -m`` lists a merge that differs from any parent, so an
        empty answer means every such commit keeps the path as its parents do.
        """
        if self._touched is None:
            touched: set[str] = set()
            for chunk in self._chunks(self._paths):
                touched |= _z_paths(self._git(
                    ['log', '--full-history', '--no-renames', '-m', '--format=', '--name-only',
                     '-z', *self._tips, '--not', self.head, '--', *chunk]).stdout)
            self._touched = touched
        return self._touched

    def _touching_tips(self, path: str) -> list[str]:
        """Tips that contain a commit (reachable only from tips) changing ``path``.

        A tip outside this list keeps ``path`` as its merge bases do (the
        ``_touched_on_tips`` argument restricted to that tip).
        """
        commits = self._git(['log', '--full-history', '--no-renames', '-m', '--format=%H',
                             *self._tips, '--not', self.head, '--', path]).stdout.decode().split()
        commits = sorted(set(commits))
        if not commits or len(commits) > MAX_PATHSPECS_PER_CALL:
            return list(self._tips)
        contains = [arg for commit in commits for arg in ('--contains', commit)]
        raw = self._git(['for-each-ref', '--format=%(objectname)', *contains,
                         'refs/heads']).stdout.decode().split()
        return [tip for tip in self._tips if tip in set(raw)]

    def _busy_tips(self) -> frozenset:
        """Commits checked out in another worktree of this repository."""
        if self._busy is None:
            # No ``-z`` (git 2.36+). A path git cannot print on one line parses
            # as some other worktree: its commit holds, never retires.
            out = self._git(['worktree', 'list', '--porcelain']).stdout
            busy, path, head = set(), None, None
            for line in [*out.decode('utf-8', 'surrogateescape').split('\n'), '']:
                if line.startswith('worktree '):
                    path = line[len('worktree '):]
                elif line.startswith('HEAD '):
                    head = line[len('HEAD '):]
                elif not line:
                    if path is not None and head is not None and not self._is_here(path):
                        busy.add(head)
                    path = head = None
            self._busy = frozenset(busy)
        return self._busy

    def _is_here(self, path: str) -> bool:
        try:
            return Path(path).resolve() == Path(self.top).resolve()
        except (OSError, RuntimeError):
            return False

    def _unchanged_since_base(self, tip: str, path: str) -> bool:
        """``tip`` holds ``path`` exactly as every merge base of ``tip`` and HEAD does."""
        bases = self._merge_bases(tip)
        entry = self._tip_entry(tip, path)
        return bool(bases) and all(self._base_entry(base, tip, path) == entry for base in bases)

    def _merge_bases(self, tip: str) -> tuple[str, ...]:
        if tip not in self._bases:
            out = self._git(['merge-base', '--all', tip, self.head], ok=(0, 1))
            self._bases[tip] = tuple(out.stdout.decode().split()) if out.returncode == 0 else ()
        return self._bases[tip]

    def _base_entry(self, base: str, tip: str, path: str) -> str | None:
        """``path``'s entry in ``base``; one lookup covers every path ``tip`` holds."""
        if (base, path) not in self._base_entries:
            wanted = {item for item, entry in self._tip_entries[tip].items()
                      if entry is not None and (base, item) not in self._base_entries}
            for chunk in self._chunks(wanted):
                found = _z_entries(self._git(['ls-tree', '-r', '-z', base, '--', *chunk]).stdout)
                self._base_entries.update({(base, item): found.get(item) for item in chunk})
        return self._base_entries[(base, path)]
