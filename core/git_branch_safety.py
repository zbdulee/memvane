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

The facts are the ones a later laya-db (worktree, branch) view records per
source (worktree path, HEAD commit, tracked paths); this module only answers
them per run. Every git call has a timeout and a run deadline. A ``.git``
entry that git cannot answer raises ``GitUnavailable``: the caller holds.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

GIT_TIMEOUT_SECONDS = 5.0
RUN_BUDGET_SECONDS = 20.0
MAX_BRANCH_TIPS = 64
MAX_PATHSPECS_PER_CALL = 200
# Per-worktree markers of a history operation that rewrites the checkout in
# several steps. A source can be absent only for the middle of one.
OPERATION_MARKERS = ('rebase-merge', 'rebase-apply', 'MERGE_HEAD',
                     'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG')
_REPO_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
             'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
             'GIT_NAMESPACE', 'GIT_PREFIX')

RETIRE = 'retire'
HOLD = 'hold'


class GitUnavailable(RuntimeError):
    """A git checkout exists but git could not answer within its bounds."""


def _env() -> dict:
    env = {key: value for key, value in os.environ.items() if key not in _REPO_ENV}
    # Paths are data, never globs; no index lock and no prompt from a hook.
    env.update(GIT_LITERAL_PATHSPECS='1', GIT_OPTIONAL_LOCKS='0',
               GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    return env


def run_git(args: list[str], cwd: Path, *, ok=(0,), deadline: float | None = None,
            timeout: float = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitUnavailable('git_budget_exhausted')
        timeout = min(timeout, remaining)
    try:
        # A user `log.showSignature` would mix gpg lines into `--format=%H`.
        result = subprocess.run(['git', '-c', 'log.showSignature=false', *args],
                                cwd=str(cwd), env=_env(),
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitUnavailable('git_call_failed') from exc
    if result.returncode not in ok:
        raise GitUnavailable('git_exit_' + str(result.returncode))
    return result


def find_dotgit(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / '.git'
        if os.path.lexists(candidate):
            return candidate
    return None


def repo_facts(path, *, deadline: float | None = None) -> dict | None:
    """Toplevel, git dir and common dir of the checkout holding ``path``.

    None when no ``.git`` entry is an ancestor: not a git project, so there is
    no branch to be invisible on. Raises ``GitUnavailable`` when a ``.git``
    entry exists but git cannot describe it.
    """
    start = Path(path).resolve()
    if not start.is_dir():
        start = start.parent
    if find_dotgit(start) is None:
        return None
    out = run_git(['rev-parse', '--path-format=absolute', '--show-toplevel',
                   '--git-dir', '--git-common-dir'], start, deadline=deadline)
    lines = out.stdout.decode('utf-8', 'surrogateescape').splitlines()
    if len(lines) != 3 or not all(lines):
        raise GitUnavailable('git_rev_parse_unexpected')
    toplevel, git_dir, common_dir = (Path(line).resolve() for line in lines)
    return {'toplevel': toplevel, 'gitDir': git_dir, 'commonDir': common_dir,
            'linked': git_dir != common_dir}


def linked_worktree(cwd) -> dict | None:
    """Prove ``cwd`` is inside a linked worktree of a non-bare main checkout.

    Returns ``{worktreeRoot, mainRoot, gitDir, commonDir}`` only when git
    reports a git dir under ``<common>/worktrees/`` and the main checkout's own
    git dir is that common dir. Submodules, bare repos, symlinked ``.git``
    entries and any git failure return None (the caller keeps its old path).
    """
    try:
        start = Path(cwd).resolve()
        dotgit = find_dotgit(start)
        # Only a linked worktree (or submodule) has a `.git` file. A normal
        # checkout never pays for a git process here.
        if dotgit is None or dotgit.is_symlink() or not dotgit.is_file():
            return None
        facts = repo_facts(start)
        if facts is None or not facts['linked']:
            return None
        worktree, common = facts['toplevel'], facts['commonDir']
        if (worktree != dotgit.parent.resolve() or common.name != '.git'
                or facts['gitDir'].parent != common / 'worktrees'
                or common.is_symlink() or not common.is_dir()):
            return None
        main = common.parent
        main_facts = repo_facts(main)
        if (main_facts is None or main_facts['linked']
                or main_facts['toplevel'] != main or main_facts['gitDir'] != common
                or main == worktree):
            return None
    except (GitUnavailable, OSError, RuntimeError, ValueError):
        return None
    return {'worktreeRoot': str(worktree), 'mainRoot': str(main),
            'gitDir': str(facts['gitDir']), 'commonDir': str(common)}


def operation_in_progress(facts: dict) -> str | None:
    """Name of the first in-progress rebase/merge/cherry-pick/revert/bisect."""
    for base in (facts['gitDir'], facts['commonDir']):
        for marker in OPERATION_MARKERS:
            if os.path.lexists(Path(base) / marker):
                return marker
    return None


def _z_paths(raw: bytes) -> set[str]:
    return {item.decode('utf-8', 'surrogateescape') for item in raw.split(b'\0') if item}


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
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve().relative_to(self.top).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None

    def _chunks(self, paths):
        ordered = sorted(paths)
        for start in range(0, len(ordered), MAX_PATHSPECS_PER_CALL):
            yield ordered[start:start + MAX_PATHSPECS_PER_CALL]

    def prefetch(self, paths) -> None:
        """Batch the index and branch-tip lookups for every path of a run."""
        pending = {path for path in paths if path is not None and path not in self._index}
        for chunk in self._chunks(pending):
            found = _z_paths(self._git(['ls-files', '-z', '--cached', '--', *chunk]).stdout)
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
