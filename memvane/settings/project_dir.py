"""Paths in a project's ``.memvane`` directory."""
from __future__ import annotations

from pathlib import Path

CURRENT = '.memvane'
NAMES = (CURRENT,)
SETTINGS = 'settings.json'
WIKI = 'wiki'


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


def compile_rel(root=None) -> str:
    """프로젝트 루트 기준 compile 디렉터리(`<활성 폴더>/compile`)."""
    return active_name(root) + "/compile"


def compile_dir(root, create: bool = False):
    """`<root>/<활성 폴더>/compile`. root 밖으로 나가면 None.

    판정은 `wiki_compile.safe_managed_dir(root, compile_rel(root))`과 같다
    (resolve 후 root 포함 + 디렉터리 여부). `create=False`면 존재하지 않아도 경로를
    돌려준다 — 읽기 경로가 디렉터리를 만드는 부작용을 갖지 않게 한다.
    """
    root = Path(root)
    path = (root / compile_rel(root)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            return None
    elif create:
        path.mkdir(parents=True, exist_ok=True)
    return path
