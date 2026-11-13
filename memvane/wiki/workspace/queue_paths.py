"""Safe compile queue and lock paths."""
from __future__ import annotations

from pathlib import Path
from memvane.settings import project_dir
from memvane.wiki.workspace import compile_paths as cp


def _safe_queue_path(project_root, configured_path=None):
    """Return a validated queue path inside the project compile directory."""
    if not isinstance(configured_path, str) or not configured_path:
        configured_path = cp.rel(cp.SOURCE_QUEUE)
    rel = Path(configured_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    if (len(rel.parts) < 3 or not project_dir.is_folder_name(rel.parts[0])
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
    return path.with_name(f"{path.name}.lock")
