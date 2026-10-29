"""The per-project MemVane folder: ``.memvane/``, read fallback ``.auto-context/``.

A project keeps its settings (``settings.json``), wiki cards (``wiki/``) and
per-machine runtime files (route record, selection marker, locks,
``compile/``, ``file-changes/``) in one folder at the project root. The folder
is ``.memvane/``. A project onboarded before the rename still has
``.auto-context/``; the plugin reads it until the bundle upgrade worker
renames it once (``laya_project_dir_move``).

Exactly one folder is *active* and every writer writes there only:

* ``.memvane/`` when it is a real directory,
* else ``.auto-context/`` when it is a real directory (a project not moved yet),
* else ``.memvane/`` (a new project).

Readers look in ``.memvane/`` first and fall back to ``.auto-context/`` per
file (``runtime_file``, ``settings_path``). ``layout`` names the combination,
including an older git branch checked out over a moved project (``split``).
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat

CURRENT = '.memvane'
LEGACY = '.auto-context'
NAMES = (CURRENT, LEGACY)
SETTINGS = 'settings.json'
WIKI = 'wiki'
# Files a selected project keeps at the folder root, never tracked by git.
RUNTIME_FILES = ('laya-index-route.json', 'laya-index-selection.json',
                 'laya-qmd-cutover-operation.lock', '.install-update.lock',
                 'memvane-state-move.json',
                 'memvane-project-move.json')
RUNTIME_DIRS = ('compile', 'file-changes')
ROUTE = 'laya-index-route.json'
# An untracked hidden file that keeps `.memvane/wiki` (and its inode) alive
# when git checks out a branch whose cards live elsewhere; walkers skip it.
WIKI_ANCHOR = '.memvane-root'
# Finder metadata a merge may drop when both folders hold one.
DISPOSABLE = frozenset(('.DS_Store',))
# The project's own .gitignore lines MemVane suggests for the runtime files.
IGNORE_SUFFIXES = ('compile/', 'file-changes/', 'laya-index-route.json',
                   'laya-index-selection.json', 'memvane-*.json', '*.lock', '.*.lock',
                   'wiki/' + '.memvane-root')


def _real_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def active_name(project) -> str:
    """The folder name every writer of ``project`` uses."""
    if project is None:
        return CURRENT
    root = Path(project)
    if _real_dir(root / CURRENT):
        return CURRENT
    if _real_dir(root / LEGACY):
        return LEGACY
    return CURRENT


def folder(project) -> Path:
    return Path(project) / active_name(project)


def wiki_rel(project=None) -> str:
    return active_name(project) + '/' + WIKI


def is_folder_name(name) -> bool:
    return name in NAMES


def is_folder(path) -> bool:
    """True for ``<project>/.memvane`` or ``<project>/.auto-context``."""
    return Path(path).name in NAMES


def runtime_file(project, name) -> Path:
    """``.memvane/<name>`` when present, else ``.auto-context/<name>`` when
    present, else the active folder's path (where a writer creates it)."""
    root = Path(project)
    for folder_name in NAMES:
        path = root / folder_name / name
        if os.path.lexists(path):
            return path
    return root / active_name(root) / name


def settings_path(project) -> Path | None:
    """The settings file a project reads: ``.memvane/`` first, then legacy."""
    root = Path(project)
    for folder_name in NAMES:
        path = root / folder_name / SETTINGS
        if path.is_file():
            return path
    return None


def settings_candidates(project) -> list[Path]:
    root = Path(project)
    return [root / folder_name / SETTINGS for folder_name in NAMES]


def _has_cards(wiki: Path) -> bool:
    """Any non-dot file under ``wiki`` (a stray ``.DS_Store`` is not a card)."""
    if not _real_dir(wiki):
        return False
    for _base, dirs, files in os.walk(wiki):
        dirs[:] = [name for name in dirs if not name.startswith('.')]
        if any(not name.startswith('.') for name in files):
            return True
    return False


def _has_content(path: Path) -> bool:
    return os.path.lexists(path / SETTINGS) or _has_cards(path / WIKI)


def layout(project) -> str:
    """Which folders a project has and how they relate.

    ``none``      neither folder;
    ``legacy``    only ``.auto-context/`` (not moved yet);
    ``current``   only ``.memvane/``;
    ``adopt``     ``.memvane/`` holds the settings (a pulled or checked-out
                  rename) while ``.auto-context/`` keeps the runtime files
                  (the route record) and whatever git did not move (untracked
                  cards, hidden pending replacements);
    ``split``     ``.memvane/`` has no settings while ``.auto-context/`` has
                  them: an older branch is checked out over a moved project;
    ``conflict``  both folders hold settings or wiki cards and the runtime
                  files are in ``.memvane/`` (an older branch was merged).
    """
    root = Path(project)
    current, legacy = root / CURRENT, root / LEGACY
    has_current, has_legacy = _real_dir(current), _real_dir(legacy)
    if not has_current and not has_legacy:
        return 'none'
    if not has_current:
        return 'legacy'
    if not has_legacy:
        return 'current'
    current_settings = os.path.lexists(current / SETTINGS)
    if os.path.lexists(legacy / SETTINGS) and not current_settings:
        return 'split'
    if (current_settings and not os.path.lexists(legacy / SETTINGS) and
            os.path.lexists(legacy / ROUTE) and not os.path.lexists(current / ROUTE)):
        return 'adopt'
    if _has_content(legacy):
        return 'conflict'
    return 'adopt' if current_settings else 'conflict'


def rebase_rel(value, project=None):
    """A project-relative path under either folder, moved to the active one.

    ``.auto-context/wiki/x.md`` becomes ``.memvane/wiki/x.md`` once the
    project uses ``.memvane/``. Any other value is returned unchanged.
    """
    if not isinstance(value, str) or not value:
        return value
    parts = PurePosixPath(value).parts
    if not parts or parts[0] not in NAMES:
        return value
    active = active_name(project)
    if parts[0] == active:
        return value
    return str(PurePosixPath(active, *parts[1:]))


def legacy_ignore_lines(text: str) -> list[str]:
    """``.memvane`` equivalents of a ``.gitignore``'s ``.auto-context`` lines.

    Only lines the file does not already carry for ``.memvane`` are returned.
    """
    lines = [line.strip() for line in text.splitlines()]
    present = set(lines)
    suggested = []
    for line in lines:
        if not line or line.startswith('#') or LEGACY not in line:
            continue
        moved = line.replace(LEGACY, CURRENT)
        if moved not in present and moved not in suggested:
            suggested.append(moved)
    return suggested
