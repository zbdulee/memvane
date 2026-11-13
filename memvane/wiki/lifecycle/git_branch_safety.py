#!/usr/bin/env python3
"""Read-only, bounded git facts for branch and worktree safety.

Two consumers share these facts:

- ``laya_index_host_route`` proves that a cwd is a linked git worktree of a
  main checkout (same common git dir) before serving that checkout's published
  index read-only.
- ``wiki_lifecycle`` / ``wiki_source_retire`` decide whether a wiki card's
  missing source was really deleted in HEAD's lineage or is only invisible on
  this branch. A card is never retired because its source exists on another
  branch only.

The facts are the ones a later memvane-db (worktree, branch) view records per
source (worktree path, HEAD commit, tracked paths); this module only answers
them per run. Every git call has a timeout and a run deadline. A ``.git``
entry that git cannot answer raises ``GitUnavailable``: the caller holds.

``DeletionProof`` is the schema3 consumer: it judges a recorded source
fingerprint (not a card commit) and returns a ``delete_receipts`` row spec.
The v6 path keeps ``LineageJudge.judge``.
"""
from __future__ import annotations
from memvane.wiki.lifecycle.types import UNTRACKED

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import unicodedata

from memvane.kernel.paths import (GIT_TIMEOUT_SECONDS, GitUnavailable, _env,
                                  find_dotgit, linked_worktree, repo_facts, run_git)

RUN_BUDGET_SECONDS = 20.0
MAX_BRANCH_TIPS = 64
MAX_PATHSPECS_PER_CALL = 200
# Per-worktree markers of a history operation that rewrites the checkout in
# several steps. A source can be absent only for the middle of one.
OPERATION_MARKERS = ('rebase-merge', 'rebase-apply', 'MERGE_HEAD',
                     'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG')


RETIRE = 'retire'
HOLD = 'hold'














def operation_in_progress(facts: dict) -> str | None:
    """Name of the first in-progress rebase/merge/cherry-pick/revert/bisect."""
    for base in (facts['gitDir'], facts['commonDir']):
        for marker in OPERATION_MARKERS:
            if os.path.lexists(Path(base) / marker):
                return marker
    return None


def _nfc(path: str) -> str:
    # macOS git stores precomposed names; a card may record either form.
    return unicodedata.normalize('NFC', path)


def _z_paths(raw: bytes) -> set[str]:
    return {_nfc(item.decode('utf-8', 'surrogateescape')) for item in raw.split(b'\0') if item}


def _chunks(paths):
    ordered = sorted(paths)
    for start in range(0, len(ordered), MAX_PATHSPECS_PER_CALL):
        yield ordered[start:start + MAX_PATHSPECS_PER_CALL]


def _git_relative(root: Path, top: Path, path) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return _nfc(candidate.resolve().relative_to(top).as_posix())
    except (OSError, RuntimeError, ValueError):
        return None


def _index_paths(top: Path, paths, deadline: float | None) -> set[str]:
    """Paths of ``paths`` present in the index, ``MAX_PATHSPECS_PER_CALL`` per git call."""
    found: set[str] = set()
    for chunk in _chunks(paths):
        found |= _z_paths(run_git(['ls-files', '-z', '--cached', '--', *chunk], top,
                                  deadline=deadline).stdout)
    return found


TRACKED = 'tracked'
UNKNOWN = 'unknown'


def tracked_states(root, paths, head_oid: str | None, *,
                   budget: float = GIT_TIMEOUT_SECONDS) -> dict:
    """``{path: 'tracked'|'untracked'|'unknown'}`` for project-relative ``paths``.

    The same index/HEAD facts ``LineageJudge`` rule 1 reads: a path in the
    index (a staged new file included) or in ``head_oid``'s tree is tracked;
    in neither it is untracked. ``head_oid`` is the commit the caller already
    observed, so the tree read here is that commit's even if HEAD moves; the
    caller re-checks the index and HEAD it captured after this returns.
    ``None`` (unborn branch) has no tree. A path outside the repository, a
    non-git project and any git failure or timeout answer ``unknown``. Git
    calls are 1 + 2 per ``MAX_PATHSPECS_PER_CALL`` paths.
    """
    paths = list(paths)
    unknown = {path: UNKNOWN for path in paths}
    deadline = time.monotonic() + budget
    root = Path(root).resolve()
    try:
        facts = repo_facts(root, deadline=deadline)
        if facts is None:
            return unknown
        top = facts['toplevel']
        names = {path: _git_relative(root, top, path) for path in paths}
        wanted = {name for name in names.values() if name is not None}
        tracked = _index_paths(top, wanted, deadline)
        if head_oid is not None:
            for chunk in _chunks(wanted - tracked):
                tracked |= _z_paths(run_git(
                    ['ls-tree', '-r', '--name-only', '-z', head_oid, '--', *chunk], top,
                    deadline=deadline).stdout)
    except (GitUnavailable, OSError, RuntimeError, ValueError):
        return unknown
    return {path: UNKNOWN if name is None else TRACKED if name in tracked else UNTRACKED
            for path, name in names.items()}


class LineageJudge:
    """Per-run judgement of missing wiki sources against HEAD's lineage.

    Rules (``judge`` returns ``(RETIRE|HOLD, reason)``):

    1. A source tracked in HEAD or in the index is not deleted; the file is
       only absent from the working tree. Hold.
    2. Card tracked in HEAD (C = last HEAD commit touching the card):
       a. source in C's tree, absent from HEAD: deleted in this lineage. Retire.
       b. a source deletion reachable from HEAD at or after C. Retire.
       c. source on a local branch tip not merged into HEAD. Hold.
       d. otherwise the source was never tracked and the committed card lost
          it in this checkout. Retire.
    3. Card not tracked in HEAD (uncommitted or from another branch):
       a. source on a local branch tip not merged into HEAD, or in the card's
          last commit on a local branch outside HEAD's lineage. Hold.
       b. a source deletion reachable from HEAD. Retire.
       c. otherwise neither the source nor the card was ever committed. Hold:
          nothing proves this checkout, not another one, owned that source.

    Paths outside the repository, more than ``MAX_BRANCH_TIPS`` unmerged
    branch tips (for 2c/3a) and an exhausted git budget all hold.
    """

    def __init__(self, root: Path, facts: dict, *, budget: float = RUN_BUDGET_SECONDS):
        self.root = Path(root).resolve()
        self.facts = facts
        self.top = facts['toplevel']
        self.deadline = time.monotonic() + budget
        self.head = self._head()
        self.head_paths: set[str] = set()
        if self.head is not None:
            self.head_paths = _z_paths(self._git(
                ['ls-tree', '-r', '--name-only', '-z', self.head]).stdout)
        self._index: dict[str, bool] = {}
        self._tips: list[str] | None = None
        self._on_tips: dict[str, bool] = {}
        self._card_commit: dict[tuple[str, bool], str | None] = {}
        self._deletion: dict[str, str | None] = {}
        self._in_commit: dict[tuple[str, str], bool] = {}
        self._ancestor: dict[tuple[str, str], bool] = {}

    @classmethod
    def open(cls, root, *, budget: float = RUN_BUDGET_SECONDS):
        """None for a non-git project; raises ``GitUnavailable`` otherwise."""
        facts = repo_facts(root)
        if facts is None:
            return None
        return cls(root, facts, budget=budget)

    def _git(self, args, ok=(0,)):
        return run_git(args, self.top, ok=ok, deadline=self.deadline)

    def _head(self) -> str | None:
        out = self._git(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], ok=(0, 1))
        if out.returncode != 0:
            return None  # unborn branch: no commit, nothing is tracked in HEAD
        return out.stdout.decode().strip() or None

    def operation(self) -> str | None:
        return operation_in_progress(self.facts)

    def still_valid(self) -> bool:
        """No history operation started and HEAD did not move since this run began."""
        try:
            return self.operation() is None and self._head() == self.head
        except GitUnavailable:
            return False

    def git_path(self, path) -> str | None:
        return _git_relative(self.root, self.top, path)

    def _chunks(self, paths):
        return _chunks(paths)

    def prefetch(self, paths) -> None:
        """Batch the index and branch-tip lookups for every path of a run."""
        pending = {path for path in paths if path is not None and path not in self._index}
        for chunk in self._chunks(pending):
            found = self._index_lookup(chunk)
            self._index.update({path: path in found for path in chunk})
        tips = self._unmerged_tips()
        pending = {path for path in paths if path is not None and path not in self._on_tips}
        if tips is None or not pending:
            return
        seen: set[str] = set()
        for tip in tips:
            for chunk in self._chunks(pending):
                seen |= _z_paths(self._git(
                    ['ls-tree', '-r', '--name-only', '-z', tip, '--', *chunk]).stdout)
        self._on_tips.update({path: path in seen for path in pending})

    def _index_lookup(self, paths) -> set[str]:
        """Paths of ``paths`` the index lists now; one call per chunk, no cache."""
        found: set[str] = set()
        for chunk in self._chunks(paths):
            found |= _z_paths(self._git(['ls-files', '-z', '--cached', '--', *chunk]).stdout)
        return found

    def _in_index(self, path: str) -> bool:
        if path not in self._index:
            self.prefetch([path])
        return self._index[path]

    def _unmerged_tips(self) -> list[str] | None:
        """Local branch tips not merged into HEAD; None when over the bound."""
        if self._tips is None:
            args = ['for-each-ref', '--format=%(objectname)']
            if self.head is not None:
                args.append('--no-merged=' + self.head)
            raw = self._git([*args, 'refs/heads']).stdout.decode().split()
            self._tips = sorted(set(raw))
        return None if len(self._tips) > MAX_BRANCH_TIPS else self._tips

    def _on_other_branch(self, path: str) -> bool:
        if self._unmerged_tips() is None:
            return True
        if path not in self._on_tips:
            self.prefetch([path])
        return self._on_tips.get(path, True)

    def _last_card_commit(self, card: str, in_head: bool) -> str | None:
        key = (card, in_head)
        if key not in self._card_commit:
            scope = [self.head] if in_head else ['--branches']
            out = self._git(['log', '-1', '--format=%H', *scope, '--', card])
            self._card_commit[key] = out.stdout.decode().strip() or None
        return self._card_commit[key]

    def _deletion_commit(self, path: str) -> str | None:
        if self.head is None:
            return None
        if path not in self._deletion:
            out = self._git(['log', '-1', '--format=%H', '--diff-filter=D',
                             '--no-renames', self.head, '--', path])
            self._deletion[path] = out.stdout.decode().strip() or None
        return self._deletion[path]

    def _path_in_commit(self, commit: str, path: str) -> bool:
        key = (commit, path)
        if key not in self._in_commit:
            out = self._git(['ls-tree', '--name-only', '-z', commit, '--', path])
            self._in_commit[key] = path in _z_paths(out.stdout)
        return self._in_commit[key]

    def _is_ancestor(self, older: str, newer: str) -> bool:
        key = (older, newer)
        if key not in self._ancestor:
            out = self._git(['merge-base', '--is-ancestor', older, newer], ok=(0, 1))
            self._ancestor[key] = out.returncode == 0
        return self._ancestor[key]

    def judge(self, source, card) -> tuple[str, str]:
        try:
            return self._judge(source, card)
        except GitUnavailable as exc:
            return HOLD, str(exc)

    def _judge(self, source, card) -> tuple[str, str]:
        src, card_path = self.git_path(source), self.git_path(card)
        if src is None:
            return HOLD, 'source_outside_repository'
        if card_path is None:
            return HOLD, 'card_outside_repository'
        if src in self.head_paths or self._in_index(src):
            return HOLD, 'source_tracked_in_head'
        if card_path in self.head_paths:
            commit = self._last_card_commit(card_path, True)
            if commit is None:
                return HOLD, 'card_commit_unknown'
            if self._path_in_commit(commit, src):
                return RETIRE, 'deleted_in_lineage'
            deletion = self._deletion_commit(src)
            if deletion is not None and (deletion == commit
                                         or self._is_ancestor(commit, deletion)):
                return RETIRE, 'deleted_in_lineage'
            if self._on_other_branch(src):
                return HOLD, 'source_on_other_branch'
            return RETIRE, 'untracked_source_gone'
        if self._on_other_branch(src):
            return HOLD, 'source_on_other_branch'
        commit = self._last_card_commit(card_path, False)
        if (commit is not None and self._path_in_commit(commit, src)
                and (self.head is None or not self._is_ancestor(commit, self.head))):
            return HOLD, 'source_in_card_commit'
        if self._deletion_commit(src) is not None:
            return RETIRE, 'deleted_in_lineage'
        return HOLD, 'untracked_source_untracked_card'


def deletion_gate(root):
    """``(judge|None, hold_reason|None)`` for one retirement entry point.

    A non-git project gets ``(None, None)``: the existing retirement applies.
    A linked worktree, an in-progress history operation or an unanswerable
    checkout gets a hold reason; nothing may be retired there.
    """
    try:
        judge = LineageJudge.open(root)
    except GitUnavailable as exc:
        return None, str(exc)
    if judge is None:
        return None, None
    if judge.facts['linked']:
        return None, 'linked_worktree'
    operation = judge.operation()
    if operation is not None:
        return None, 'git_operation_in_progress:' + operation
    return judge, None


# --- schema3 fingerprint deletion proof (design §7.1) -----------------------

PROOF_SCHEMA = 'memvane-delete-proof-v1'
_RECEIPT_DOMAIN = b'memvane-delete-receipt-v1\0'
# History rewrites in several steps plus writers of the index or HEAD. The
# extra three are only consulted by the proof; ``judge`` keeps its own set.
PROOF_OPERATION_MARKERS = OPERATION_MARKERS + ('sequencer', 'index.lock', 'HEAD.lock')
MAX_CARD_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 4096
_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_GIT_REASON = re.compile(r'git_[a-z0-9_]{1,40}\Z')
_TRACKED_STATES = ('tracked', 'untracked', 'unknown')
_BINDING_STATES = ('observed', 'legacy_unbound')
# Replacement refs could graft the recorded head under HEAD; hooks and
# fsmonitor are processes the proof never runs.
_PROOF_ARGS = ('--no-replace-objects', '-c', 'core.fsmonitor=false',
               '-c', 'core.hooksPath=' + os.devnull)


def _proof_env() -> dict:
    """Every ``GIT_*`` variable dropped, no global/system config, no lazy fetch."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM='1', GIT_LITERAL_PATHSPECS='1', GIT_OPTIONAL_LOCKS='0',
               GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1', GIT_NO_REPLACE_OBJECTS='1',
               LC_ALL='C')
    return env


def _git_reason(exc: Exception) -> str:
    text = str(exc)
    return text if _GIT_REASON.fullmatch(text) else 'git_unavailable'


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _is_identity(value) -> bool:
    return (isinstance(value, dict) and set(value) == {'path', 'dev', 'ino'}
            and isinstance(value['path'], str) and Path(value['path']).is_absolute()
            and type(value['dev']) is int and type(value['ino']) is int)


def _dir_identity(path) -> dict | None:
    """``{path, dev, ino}`` of a real directory (no symlink), or None."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    return {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino}


def _project_path(value) -> str | None:
    """A project-relative POSIX path with no empty, ``.`` or ``..`` segment."""
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS
            or value.startswith('/') or any(ch in value for ch in '\x00\n\r')):
        return None
    if any(part in ('', '.', '..') for part in value.split('/')):
        return None
    return value


def _fingerprint_ok(value) -> bool:
    return (isinstance(value, dict)
            and all(isinstance(value.get(key), str) and value.get(key)
                    for key in ('fingerprint_id', 'worktree_id', 'branch_key', 'scan_id'))
            and _project_path(value.get('path')) is not None
            and (value.get('head_oid') is None or isinstance(value.get('head_oid'), str))
            and value.get('tracked_state') in _TRACKED_STATES
            and value.get('binding_state') in _BINDING_STATES)


def _blank_proof(fingerprint, scan) -> dict:
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    sc = scan if isinstance(scan, dict) else {}
    scan_id = sc.get('scan_id')
    return {
        'schema': PROOF_SCHEMA,
        'repo': {'toplevel': None, 'commonDir': None},
        'worktree': {'worktreeId': None, 'recordedWorktreeId': fp.get('worktree_id'),
                     'checkout': None, 'gitDir': None, 'verdict': 'not_checked'},
        'branch': {'recorded': fp.get('branch_key'), 'current': None},
        'bindingState': fp.get('binding_state'), 'trackedState': fp.get('tracked_state'),
        'recordedCommit': fp.get('head_oid'), 'currentCommit': None,
        'history': 'not_checked', 'ancestry': 'not_checked', 'recordedPath': 'not_checked',
        'headPath': 'not_checked', 'indexPath': 'not_checked', 'diskPath': 'not_checked',
        'operation': 'not_checked', 'recheck': 'not_checked',
        'scan': {'scanId': scan_id if isinstance(scan_id, str) else None,
                 'complete': sc.get('complete') is True, 'root': 'not_checked'},
    }


def _receipt(*, view_id, base_gen, fingerprint, proof, decision, reason, observed_head,
             now_ns) -> dict:
    """A ``delete_receipts`` row spec; the caller owns the insert.

    ``fingerprint_id`` is None when no fingerprint exists and ``observed_head``
    is None when the checkout has no HEAD: both are holds the caller records
    without a receipt row (the columns are NOT NULL).
    """
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    fingerprint_id = fp.get('fingerprint_id') if isinstance(fp.get('fingerprint_id'), str) else None
    path = _project_path(fp.get('path'))
    proof_json = _canonical_json(proof)
    material = _canonical_json([view_id, base_gen, path, fingerprint_id, observed_head,
                                decision, reason, proof_json])
    receipt_id = hashlib.sha256(_RECEIPT_DOMAIN + material.encode('utf-8')).hexdigest()
    return {'receipt_id': receipt_id, 'view_id': view_id, 'base_gen': base_gen, 'path': path,
            'fingerprint_id': fingerprint_id, 'observed_head': observed_head,
            'proof_json': proof_json, 'decision': decision, 'reason': reason,
            'created_ns': time.time_ns() if now_ns is None else now_ns}


class DeletionProof(LineageJudge):
    """schema3 deletion proof of one worktree's recorded source fingerprints.

    ``prove`` retires only when every condition of design §7.1 holds:

    1. the fingerprint is ``observed`` by this worktree registration and the
       registration still names this checkout (root, checkout, admin dir by
       dev/ino); the branch key is the recorded one;
    2. ``head_oid`` is a commit Git knows and an ancestor of HEAD, in a
       repository without shallow or grafted history (replace refs ignored);
       the path was committed at ``head_oid`` (an untracked file has no
       committed deletion);
    3. a complete scan of the same project root misses the path and the disk
       has no entry for it (symlinked or unreadable parents hold);
    4. neither the HEAD tree nor the index lists the path;
    5. no history operation or index/HEAD writer is in progress;
    6. a final re-check sees the same HEAD, branch, worktree, scan root, disk,
       index and, when given, the expected card bytes.

    Anything else, including ``legacy_unbound`` and a missing fingerprint, holds.
    Git env pollution is dropped (``_proof_env``). Git calls: four to open, at
    most five per retiring proof, ancestry cached per recorded head.
    """

    def __init__(self, root, facts, worktree, *, budget: float = RUN_BUDGET_SECONDS):
        super().__init__(root, facts, budget=budget)
        self.worktree = worktree
        out = self._git(['symbolic-ref', '-q', 'HEAD'], ok=(0, 1))
        ref = out.stdout.decode('utf-8', 'surrogateescape').strip()
        if out.returncode == 0 and ref:
            self.branch_key: str | None = ref
        elif self.head is not None:
            self.branch_key = 'detached:' + self.head
        else:
            self.branch_key = None

    @classmethod
    def open(cls, root, worktree, *, budget: float = RUN_BUDGET_SECONDS):
        """None for a non-git project; raises ``GitUnavailable`` otherwise."""
        deadline = time.monotonic() + budget
        facts = repo_facts(root, deadline=deadline, env=_proof_env(), global_args=_PROOF_ARGS)
        if facts is None:
            return None
        return cls(root, facts, worktree, budget=deadline - time.monotonic())

    def _git(self, args, ok=(0,)):
        return run_git([*_PROOF_ARGS, *args], self.top, ok=ok, deadline=self.deadline,
                       env=_proof_env())

    # -- facts --------------------------------------------------------------

    def _operation_marker(self) -> str | None:
        marker = self.operation()
        if marker is not None:
            return marker
        for base in (self.facts['gitDir'], self.facts['commonDir']):
            for name in PROOF_OPERATION_MARKERS[len(OPERATION_MARKERS):]:
                if os.path.lexists(Path(base) / name):
                    return name
        return None

    def _history(self) -> str:
        common = Path(self.facts['commonDir'])
        if os.path.lexists(common / 'shallow'):
            return 'shallow'
        if os.path.lexists(common / 'info' / 'grafts'):
            return 'grafted'
        return 'complete'

    def _registration_ok(self) -> bool:
        worktree = self.worktree
        return (isinstance(worktree, dict) and isinstance(worktree.get('worktreeId'), str)
                and bool(worktree.get('worktreeId'))
                and all(_is_identity(worktree.get(key)) for key in ('project', 'checkout', 'gitDir')))

    def _worktree_same(self) -> bool:
        worktree = self.worktree
        return (_dir_identity(self.root) == worktree['project']
                and _dir_identity(self.top) == worktree['checkout']
                and _dir_identity(self.facts['gitDir']) == worktree['gitDir'])

    def _repo_path(self, path: str) -> str | None:
        try:
            prefix = PurePosixPath(self.root.relative_to(self.top).as_posix())
        except ValueError:
            return None
        return _nfc((prefix / path).as_posix())

    def _disk_state(self, path: str) -> str:
        """``absent`` only when lstat says so with real directories above it."""
        current = self.root
        parts = path.split('/')
        for part in parts[:-1]:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return 'absent'
            except OSError:
                return 'unverifiable'
            if not stat.S_ISDIR(info.st_mode):
                return 'unverifiable'
        try:
            os.lstat(current / parts[-1])
        except FileNotFoundError:
            return 'absent'
        except OSError:
            return 'unverifiable'
        return 'present'

    def _scan_root_same(self, scan) -> bool:
        root = scan['root']
        return root == self.worktree['project'] and _dir_identity(self.root) == root

    def _card_matches(self, expected) -> bool:
        if not isinstance(expected, dict):
            return False
        path, sha = _project_path(expected.get('path')), expected.get('sha256')
        if path is None or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            return False
        if self._disk_state(path) != 'present':
            return False
        try:
            fd = os.open(str(self.root / path), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CARD_BYTES:
                return False
            digest, total = hashlib.sha256(), 0
            while True:
                block = os.read(fd, 1 << 20)
                if not block:
                    break
                total += len(block)
                if total > MAX_CARD_BYTES:
                    return False
                digest.update(block)
        except OSError:
            return False
        finally:
            os.close(fd)
        return digest.hexdigest() == sha

    def _current_checkout(self) -> tuple[str | None, str | None]:
        """``(HEAD, branch key)`` now, in one git call."""
        out = self._git(['rev-parse', 'HEAD', '--symbolic-full-name', 'HEAD'], ok=(0, 128))
        lines = out.stdout.decode('utf-8', 'surrogateescape').splitlines()
        if out.returncode != 0 or len(lines) != 2 or not _OID.fullmatch(lines[0]):
            return None, None
        branch = lines[1] if lines[1].startswith('refs/') else 'detached:' + lines[0]
        return lines[0], branch

    def _recheck(self, scan, path, repo_path, expected_card) -> bool:
        if self._operation_marker() is not None:
            return False
        if self._current_checkout() != (self.head, self.branch_key):
            return False
        if not self._worktree_same() or not self._scan_root_same(scan):
            return False
        if self._disk_state(path) != 'absent':
            return False
        if repo_path in self._index_lookup([repo_path]):
            return False
        return expected_card is None or self._card_matches(expected_card)

    def recheck(self, fingerprint, scan, expected_card) -> bool:
        """Recheck a proved deletion immediately before removing its card."""
        if not _fingerprint_ok(fingerprint):
            return False
        path = fingerprint['path']
        repo_path = self._repo_path(path)
        if repo_path is None:
            return False
        try:
            return self._recheck(scan, path, repo_path, expected_card)
        except (GitUnavailable, OSError, KeyError, TypeError, ValueError):
            return False

    # -- proof --------------------------------------------------------------

    def prove(self, fingerprint, scan, *, view_id, base_gen, expected_card=None,
              now_ns: int | None = None) -> dict:
        proof = _blank_proof(fingerprint, scan)
        proof['repo'] = {'toplevel': str(self.top),
                         'commonDir': _dir_identity(self.facts['commonDir'])}
        proof['branch']['current'] = self.branch_key
        proof['currentCommit'] = self.head
        try:
            decision, reason = self._prove(fingerprint, scan, expected_card, proof)
        except GitUnavailable as exc:
            decision, reason = HOLD, _git_reason(exc)
        return _receipt(view_id=view_id, base_gen=base_gen, fingerprint=fingerprint,
                        proof=proof, decision=decision, reason=reason,
                        observed_head=self.head, now_ns=now_ns)

    def _prove(self, fp, scan, expected_card, proof) -> tuple[str, str]:
        if fp is None:
            return HOLD, 'fingerprint_missing'
        if not _fingerprint_ok(fp):
            return HOLD, 'fingerprint_invalid'
        if fp['binding_state'] != 'observed':
            return HOLD, 'legacy_unbound'
        # 1. same worktree, same branch key
        if not self._registration_ok():
            return HOLD, 'worktree_registration_invalid'
        worktree = proof['worktree']
        worktree.update(worktreeId=self.worktree['worktreeId'],
                        checkout=self.worktree['checkout'], gitDir=self.worktree['gitDir'])
        if fp['worktree_id'] != self.worktree['worktreeId']:
            worktree['verdict'] = 'other'
            return HOLD, 'other_worktree'
        if not self._worktree_same():
            worktree['verdict'] = 'changed'
            return HOLD, 'worktree_identity_changed'
        worktree['verdict'] = 'same'
        if fp['branch_key'] != self.branch_key:
            return HOLD, 'branch_changed'
        # 5. no history operation
        marker = self._operation_marker()
        proof['operation'] = marker or 'absent'
        if marker is not None:
            return HOLD, 'git_operation_in_progress'
        # 2. preconditions of the ancestry proof
        recorded = fp['head_oid']
        if recorded is None:
            return HOLD, 'recorded_commit_null'
        if self.head is None:
            return HOLD, 'current_commit_unborn'
        if not _OID.fullmatch(recorded) or len(recorded) != len(self.head):
            return HOLD, 'recorded_commit_invalid'
        proof['history'] = self._history()
        if proof['history'] == 'shallow':
            return HOLD, 'shallow_repository'
        if proof['history'] == 'grafted':
            return HOLD, 'grafted_history'
        if fp['tracked_state'] != 'tracked':
            return HOLD, 'not_tracked_at_observation'
        # 3. complete scan of the same root, and the disk, miss the path
        if (not isinstance(scan, dict) or not isinstance(scan.get('scan_id'), str)
                or not scan.get('scan_id') or not isinstance(scan.get('complete'), bool)
                or not _is_identity(scan.get('root')) or 'present_paths' not in scan):
            return HOLD, 'scan_invalid'
        if not scan['complete']:
            return HOLD, 'scan_incomplete'
        if not self._scan_root_same(scan):
            proof['scan']['root'] = 'changed'
            return HOLD, 'scan_root_changed'
        proof['scan']['root'] = 'same'
        path = fp['path']
        if path in scan['present_paths']:
            return HOLD, 'present_in_scan'
        proof['diskPath'] = self._disk_state(path)
        if proof['diskPath'] == 'present':
            return HOLD, 'present_on_disk'
        if proof['diskPath'] != 'absent':
            return HOLD, 'path_unverifiable'
        # 4. HEAD tree and index
        repo_path = self._repo_path(path)
        if repo_path is None:
            return HOLD, 'path_outside_repository'
        proof['headPath'] = 'present' if repo_path in self.head_paths else 'absent'
        if proof['headPath'] == 'present':
            return HOLD, 'path_in_head'
        proof['indexPath'] = 'present' if repo_path in self._index_lookup([repo_path]) else 'absent'
        if proof['indexPath'] == 'present':
            return HOLD, 'path_in_index'
        # 2. recorded head is a known ancestor and held the path
        try:
            ancestor = self._is_ancestor(recorded, self.head)
        except GitUnavailable as exc:
            proof['ancestry'] = 'unknown'
            if str(exc).startswith('git_exit_'):
                return HOLD, 'recorded_commit_unknown'
            raise
        proof['ancestry'] = 'ancestor' if ancestor else 'not_ancestor'
        if not ancestor:
            return HOLD, 'recorded_commit_not_ancestor'
        committed = self._path_in_commit(recorded, repo_path)
        proof['recordedPath'] = 'present' if committed else 'absent'
        if not committed:
            return HOLD, 'not_committed_at_observed_head'
        # 6. nothing moved since the facts above
        ok = self._recheck(scan, path, repo_path, expected_card)
        proof['recheck'] = 'passed' if ok else 'failed'
        if not ok:
            return HOLD, 'recheck_failed'
        return RETIRE, 'committed_deletion_in_lineage'


def prove_source_deletion(root, worktree, fingerprint, scan, *, view_id, base_gen,
                          expected_card=None, budget: float = RUN_BUDGET_SECONDS,
                          now_ns: int | None = None) -> dict:
    """One proof with its own session; every failure is a hold row spec."""
    try:
        prover = DeletionProof.open(root, worktree, budget=budget)
    except GitUnavailable as exc:
        reason = _git_reason(exc)
        prover = None
    else:
        reason = 'not_git_repository'
    if prover is None:
        return _receipt(view_id=view_id, base_gen=base_gen, fingerprint=fingerprint,
                        proof=_blank_proof(fingerprint, scan), decision=HOLD, reason=reason,
                        observed_head=None, now_ns=now_ns)
    return prover.prove(fingerprint, scan, view_id=view_id, base_gen=base_gen,
                        expected_card=expected_card, now_ns=now_ns)
