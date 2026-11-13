"""Paths in a project's ``.memvane`` directory."""
from __future__ import annotations

from pathlib import Path

from memvane.settings.types import CURRENT, NAMES, SETTINGS, WIKI


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


def ledger(root, name: str):
    """상수 이름 하나를 실제 경로로. compile 디렉터리 밖을 가리키면 None.

    `safe_compile_file`이 하던 symlink-escape 검사를 그대로 유지한다 — 이름이 상수라도
    파일 자체가 밖을 가리키는 symlink일 수 있다(그 경우 append가 프로젝트 밖을 덮어쓴다).
    """
    base = compile_dir(root)
    if base is None:
        return None
    path = (base / name).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    return path
