"""Canonical filesystem path spelling."""
from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import time
try:
    import fcntl
except ImportError:
    fcntl = None


def canonical(path) -> Path:
    """``path`` resolved, in the file system's spelling where it differs."""
    resolved = Path(path).resolve()
    getpath = getattr(fcntl, 'F_GETPATH', None) if fcntl is not None else None
    if getpath is not None:
        try:
            fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        except OSError:
            return Path(os.path.normcase(str(resolved)))
        try:
            # MAXPATHLEN on macOS; fcntl caps the buffer at 1024 bytes.
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
        except (OSError, ValueError):
            raw = None
        finally:
            os.close(fd)
        if raw:
            spelled = os.fsdecode(raw.split(b'\0', 1)[0])
            if spelled and os.path.isabs(spelled):
                return Path(spelled)
    return Path(os.path.normcase(str(resolved)))


GIT_TIMEOUT_SECONDS = 5.0
_REPO_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
             'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
             'GIT_NAMESPACE', 'GIT_PREFIX')


class GitUnavailable(RuntimeError):
    """A git checkout exists but git could not answer within its bounds."""


def _env() -> dict:
    env = {key: value for key, value in os.environ.items() if key not in _REPO_ENV}
    # Paths are data, never globs; no index lock and no prompt from a hook.
    env.update(GIT_LITERAL_PATHSPECS='1', GIT_OPTIONAL_LOCKS='0',
               GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    return env


def run_git(args: list[str], cwd: Path, *, ok=(0,), deadline: float | None = None,
            timeout: float = GIT_TIMEOUT_SECONDS,
            env: dict | None = None) -> subprocess.CompletedProcess:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitUnavailable('git_budget_exhausted')
        timeout = min(timeout, remaining)
    try:
        # A user `log.showSignature` would mix gpg lines into `--format=%H`.
        result = subprocess.run(['git', '-c', 'log.showSignature=false', *args],
                                cwd=str(cwd), env=_env() if env is None else env,
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


def repo_facts(path, *, deadline: float | None = None, env: dict | None = None,
               global_args: tuple = ()) -> dict | None:
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
    out = run_git([*global_args, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                   '--git-dir', '--git-common-dir'], start, deadline=deadline, env=env)
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


def safe_managed_dir(root: Path, rel: str) -> Path | None:
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict]:
    """append-only 로그/큐 읽기. 못 읽는 줄은 **건너뛴다**(fail-open).

    `errors="replace"`인 이유: 이 파일들은 여러 프로세스가 append하는 운영 파일이라 한
    바이트만 깨져도 전체 읽기가 UnicodeDecodeError로 죽는다. 대체 문자는 그 줄의 JSON
    파싱만 실패시키고 아래 루프가 그 줄을 건너뛰므로, 손상은 손상된 줄에만 국한된다.
    """
    if not path.exists():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
