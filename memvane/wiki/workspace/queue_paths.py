"""Safe compile queue and lock paths."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from memvane.settings import types as project_types
from memvane.wiki.workspace import compile_paths as cp


def _safe_queue_path(project_root, configured_path=None):
    """Return a validated queue path inside the project compile directory."""
    if not isinstance(configured_path, str) or not configured_path:
        configured_path = cp.rel(cp.SOURCE_QUEUE)
    rel = Path(configured_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if (len(rel.parts) < 3 or rel.parts[0] != project_types.CURRENT
            or rel.parts[1] != "compile"):
        return None
    root = Path(project_root).resolve()
    target = root / rel
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        resolved_parent.relative_to(root)
    except (OSError, ValueError):
        return None
    if target.exists() and target.is_symlink():
        return None
    return target


def _queue_lock_path(path):
    path = Path(path)
    return path.with_name(f"{path.name}.lock")


@contextmanager
def queue_locked(path):
    with open(_queue_lock_path(path), 'a', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def requeue_lines(path: Path, raw_lines: list[str]):
    """Append rows back to a text queue (dedup pairs) under its lock."""
    if not raw_lines:
        return
    from memvane.wiki.workspace import ledger_queue
    if ledger_queue.kind(path) is not None:
        raise ValueError('ledger_queue_has_no_text_requeue')
    path.parent.mkdir(parents=True, exist_ok=True)
    with queue_locked(path):
        with path.open('a', encoding='utf-8') as handle:
            for line in raw_lines:
                handle.write(line if line.endswith('\n') else line + '\n')
