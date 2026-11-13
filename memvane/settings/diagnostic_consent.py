"""Read project consent for detailed diagnostic capture."""
from __future__ import annotations

import os
from pathlib import Path

from memvane.settings import config
from memvane.settings import project_dir

_PROVED_STATES = frozenset({"candidate", "preview"})

_WORKTREE_STATE = "worktree"

_WORKTREE_REASON = "linked_worktree_read_only"

def _canonical_dir(value) -> str | None:
    """An absolute, canonical (symlink-free) path of an existing directory."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if not os.path.isabs(value):
        return None
    try:
        if os.path.realpath(value) != value or not os.path.isdir(value):
            return None
    except (OSError, ValueError):
        return None
    return value

def _proved_root(route) -> str | None:
    """The project root of a candidate/preview decision, or None."""
    if not isinstance(route, dict) or route.get("state") not in _PROVED_STATES:
        return None
    install_id = route.get("installId")
    journal = route.get("journalPath")
    if not isinstance(install_id, str) or not install_id:
        return None
    if not isinstance(journal, str) or not os.path.isabs(journal):
        return None
    return _canonical_dir(route.get("projectRoot"))

def _consenting_root(decision) -> str | None:
    """The project whose settings decide consent for ``decision``."""
    if not isinstance(decision, dict):
        return None
    state = decision.get("state")
    if state in _PROVED_STATES:
        return _proved_root(decision)
    if state == _WORKTREE_STATE:
        if decision.get("reason") != _WORKTREE_REASON:
            return None
        if (_canonical_dir(decision.get("worktreeRoot")) is None
                or _canonical_dir(decision.get("worktreeProjectRoot")) is None):
            return None
        # Only a proved main route; a nested worktree or reader hint is off.
        return _proved_root(decision.get("mainRoute"))
    return None

def _settings_file(root: str) -> Path | None:
    """The regular, non-symlink settings file of exactly ``root``."""
    settings = project_dir.settings_path(root)
    if settings is None or settings.is_symlink() or not settings.is_file():
        return None
    return settings

def _project_opted_in(root: str) -> bool:
    """Re-read ``root``'s own settings now; True only for boolean true."""
    settings = _settings_file(root)
    if settings is None:
        return False
    # The settings must be discovered at the proved root itself: never a
    # parent's settings and never the empty fallback of an unreadable file.
    found = config.find_project_config(root)
    if (not isinstance(found, dict)
            or found.get("configFormat") != "auto-context-dir"
            or found.get("projectRoot") != root
            or found.get("configPath") != str(settings)):
        return False
    first = found.get("config")
    if not isinstance(first, dict) or config.diagnostics_config(
            first.get("diagnostics"))["enabled"] is not True:
        return False
    # Second read through the public loader: a concurrent writer turning the
    # option off (or leaving a partial file) between the reads is honored.
    loaded = config.load_project_config(root)
    if not isinstance(loaded, dict) or config.diagnostics_config(
            loaded.get("diagnostics"))["enabled"] is not True:
        return False
    # The file must still be the same settings path after both reads; if it
    # vanished, a parent's settings could have answered the second read.
    after = _settings_file(root)
    return after is not None and str(after) == str(settings)

def enabled(decision) -> bool:
    """Whether a new detailed diagnostic may be recorded for ``decision``."""
    try:
        root = _consenting_root(decision)
        if root is None:
            return False
        return _project_opted_in(root) is True
    except Exception:  # noqa: BLE001 - consent fails closed on anything
        return False
