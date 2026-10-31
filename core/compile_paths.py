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
from contextlib import contextmanager
from pathlib import Path

import project_dir

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
BUDGET_REFUSED = "budget-refused.jsonl"

# --- 커서·식힘·로그 ---------------------------------------------------------
COMPILE_COOLDOWN = "cooldown"
LEGACY_VERIFY_COOLDOWN = "verify-cooldown"  # 0.x 고아 파일 (읽지 않고 지우기만 한다)
VERIFY_ENGINE_COOLDOWN = "verify-engine-cooldown.json"
DEDUP_JUDGE_COOLDOWN = "dedup-judge-cooldown"
DEDUP_JUDGE_ENGINE_COOLDOWN = "dedup-judge-engine-cooldown.json"
CARD_WRITE_LOCK = ".card-write.lock"


def compile_rel(root=None) -> str:
    """프로젝트 루트 기준 compile 디렉터리(`<활성 폴더>/compile`)."""
    return project_dir.active_name(root) + "/compile"


def rel(name: str, root=None) -> str:
    """프로젝트 루트 기준 POSIX 상대 경로(bash·문서·사용자 메시지용)."""
    return "{}/{}".format(compile_rel(root), name)


def under(root, name: str) -> Path:
    """`<root>/<활성 폴더>/compile/<name>` (검증 없음: 쓰기 전 `ledger`를 쓴다)."""
    return Path(root) / compile_rel(root) / name


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


@contextmanager
def card_write_lock(root):
    """Serialize project card read-decision-write sections across all writers.

    The queue/kick locks guard their own ledgers, not wiki card bytes.  This
    project-local lock is intentionally independent so the compiler, verifier,
    and one-shot migration cannot interleave a stale read with a later replace
    or unlink.  ``O_NOFOLLOW`` closes a lock-file symlink escape before flock.
    """
    base = compile_dir(Path(root).resolve(), create=True)
    if base is None:
        raise OSError("unsafe compile directory for card lock")
    path = base / CARD_WRITE_LOCK
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
