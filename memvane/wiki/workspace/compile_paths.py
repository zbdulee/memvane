#!/usr/bin/env python3
"""compile 산출물 경로의 **단일 상수 테이블**.

예전에는 9개 경로만 설정 가능했고(`compile.candidatePath` 등) 나머지 17개는 각 모듈이
리터럴로 들고 있었다 — 반쪽 추상화라 "설정한 경로"와 "하드코딩된 경로"가 같은 디렉터리
안에서 섞였고, 오타 하나(`verify.skippedPath`)가 유료 과금 루프의 입구가 됐다. 위치는
프로젝트 안에 고정하고(`dataDir` 미도입) 이름은 전부 여기에 둔다.

**파일별 sidecar는 여기에 두지 않는다** — `*.lock` · `*.claimed.*` ·
`*.compact-stamp`는 원본 파일명에서 파생되므로 함수(`path.with_name(...)`)의 결과다.
다만 카드 writer 세 개(compiler·verifier·offline migration)가 공유하는
`CARD_WRITE_LOCK`은 특정 원본 파일에 속하지 않는 project-level sidecar라
경로 계약을 이 테이블에 둔다.

경로 주입 방어는 `ledger()` 하나가 한다. 설정 문자열 검증자(`safe_compile_file`)는
죽었지만 그것이 막던 **symlink escape**는 그대로 살아 있어야 한다: 상수 이름이라도
`<root>/.memvane/compile/candidates.jsonl`이 compile 디렉터리 밖을 가리키는
symlink일 수 있고, 그러면 원장 append가 프로젝트 밖 파일을 덮어쓴다.

`wikiPath`는 여전히 사용자 입력이므로 `wiki_compile.safe_managed_dir`이 남는다 —
이 모듈이 그것을 대체하지 않는다.
"""
from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from memvane.settings import project_dir
from memvane.settings.api import compile_rel, compile_dir

# 프로젝트 활성 폴더(`project_dir.active_name`) 아래 `compile/`. 상수는 새 프로젝트
# 기준값이고, 프로젝트가 있으면 `compile_rel(root)`가 활성 폴더를 따른다
# (아직 옮기지 않은 프로젝트는 `.auto-context/compile`).
COMPILE_DIR = project_dir.CURRENT + "/compile"

# --- 원장/큐 (JSONL) --------------------------------------------------------
CANDIDATES = "candidates.jsonl"
SOURCE_QUEUE = "source-queue.jsonl"
SOURCE_REFRESH_PENDING = "source-refresh-pending.jsonl"
TOMBSTONES = "tombstones.jsonl"
MANIFEST = "generated-manifest.jsonl"
MERGE_NEEDED = "merge-needed.jsonl"
VERIFY_QUEUE = "verify-queue.jsonl"
VERIFY_LOG = "verify-log.jsonl"
VERIFY_SKIPPED = "verify-skipped.jsonl"
VERIFY_DELETED = "verify-deleted.jsonl"
DEDUP_NEEDED = "dedup-needed.jsonl"
DEDUP_SKIPPED = "dedup-skipped.jsonl"
DEDUP_DELETED = "dedup-deleted.jsonl"
DISCARD_LEDGER = "discard-ledger.jsonl"

# --- 커서·식힘·로그 ---------------------------------------------------------
COMPILE_COOLDOWN = "cooldown"
LEGACY_VERIFY_COOLDOWN = "verify-cooldown"  # 0.x 고아 파일 (읽지 않고 지우기만 한다)
VERIFY_ENGINE_COOLDOWN = "verify-engine-cooldown.json"
DEDUP_JUDGE_COOLDOWN = "dedup-judge-cooldown"
DEDUP_JUDGE_ENGINE_COOLDOWN = "dedup-judge-engine-cooldown.json"
CARD_WRITE_LOCK = ".card-write.lock"
COMPILE_RUN_LOCK = ".compile-run.lock"




def rel(name: str, root=None) -> str:
    """프로젝트 루트 기준 POSIX 상대 경로(bash·문서·사용자 메시지용)."""
    return "{}/{}".format(compile_rel(root), name)


def under(root, name: str) -> Path:
    """`<root>/<활성 폴더>/compile/<name>` (검증 없음: 쓰기 전 `ledger`를 쓴다)."""
    return Path(root) / compile_rel(root) / name




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


def _lock_base(root, *, folder=None, create=False):
    """An existing project folder's real compile directory, without aliases."""
    root = Path(root).resolve(strict=True)
    directory = project_dir.folder(root) if folder is None else Path(folder)
    if directory not in tuple(root / name for name in project_dir.NAMES):
        raise OSError('unsafe compile project folder')
    if folder is None and create and not os.path.lexists(directory):
        directory.mkdir(mode=0o700)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or directory.resolve(strict=True) != directory):
        raise OSError('unsafe compile project folder')
    base = directory / 'compile'
    if create:
        base.mkdir(mode=0o700, exist_ok=True)
    info = base.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or base.resolve(strict=True) != base):
        raise OSError('unsafe compile directory for lock')
    return base


def _named_lock(fd, path):
    opened, named = os.fstat(fd), path.lstat()
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
            or opened.st_nlink != 1 or opened.st_mode & 0o077
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        raise OSError('unsafe or changed compile lock')


def validate_compile_run_lock(root, fd):
    """The acquired FD still names this project's active compile run lock."""
    base = _lock_base(root)
    _named_lock(fd, base / COMPILE_RUN_LOCK)
    return base


def try_compile_run_lock(root, *, folder=None):
    """Nonblocking run exclusion, optionally on a mover's explicit folder."""
    base = _lock_base(root, folder=folder, create=True)
    path = base / COMPILE_RUN_LOCK
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _named_lock(fd, path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _named_lock(fd, path)
        if folder is None:
            validate_compile_run_lock(root, fd)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def card_write_lock(root, *, folder=None):
    """Serialize project card read-decision-write sections across all writers.

    The queue/kick locks guard their own ledgers, not wiki card bytes.  This
    project-local lock is intentionally independent so the compiler, verifier,
    and one-shot migration cannot interleave a stale read with a later replace
    or unlink.  ``O_NOFOLLOW`` closes a lock-file symlink escape before flock.
    """
    base = _lock_base(root, folder=folder, create=True)
    path = base / CARD_WRITE_LOCK
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        _named_lock(handle.fileno(), path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _named_lock(handle.fileno(), path)
        if folder is None and _lock_base(root) != base:
            raise OSError('changed compile directory for card lock')
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
