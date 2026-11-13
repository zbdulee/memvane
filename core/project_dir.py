"""Paths in a project's ``.memvane`` directory."""
from __future__ import annotations

from pathlib import Path

CURRENT = '.memvane'
NAMES = (CURRENT,)
SETTINGS = 'settings.json'
WIKI = 'wiki'
IGNORE_SUFFIXES = ('compile/', 'file-changes/', 'laya-index-route.json',
                   'laya-index-selection.json', 'memvane-*.json', '*.lock', '.*.lock',
                   'wiki/' + '.memvane-root')


def active_name(project) -> str:
    return CURRENT


def folder(project) -> Path:
    return Path(project) / CURRENT


def wiki_rel(project=None) -> str:
    return CURRENT + '/' + WIKI


def is_folder_name(name) -> bool:
    return name == CURRENT


def is_folder(path) -> bool:
    return Path(path).name == CURRENT


def runtime_file(project, name) -> Path:
    return Path(project) / CURRENT / name


def settings_path(project) -> Path | None:
    path = Path(project) / CURRENT / SETTINGS
    return path if path.is_file() else None


def settings_candidates(project) -> list[Path]:
    return [Path(project) / CURRENT / SETTINGS]
