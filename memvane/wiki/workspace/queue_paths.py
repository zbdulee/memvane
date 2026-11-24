"""Safe compile queue and lock paths."""
from __future__ import annotations

import fcntl
import uuid
from memvane.kernel.atomic_rename import fsync_dir, write_new_file
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
    from memvane.wiki.workspace.handoff.api import public_name
    public = public_name(path)
    return public.with_name(f"{public.name}.lock")


def requeue_lines(path: Path, raw_lines: list[str]):
    if not raw_lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(_queue_lock_path(path), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.name not in (cp.SOURCE_QUEUE, cp.VERIFY_QUEUE):
                with path.open('a', encoding='utf-8') as handle:
                    for line in raw_lines:
                        handle.write(line if line.endswith('\n') else line + '\n')
                return
            from memvane.wiki.workspace.queue_return import lines_bytes
            try:
                live = path.read_bytes()
            except FileNotFoundError:
                live = b''
            if live and not live.endswith(b'\n'):
                raise OSError('incomplete queue tail')
            swap = path.parent / ('.qr-' + uuid.uuid4().hex)
            write_new_file(swap, live + lines_bytes(raw_lines))
            swap.replace(path)
            fsync_dir(path.parent)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
