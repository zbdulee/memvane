"""Single-rename card publication and deterministic crash recovery."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import uuid

from memvane.kernel.atomic_rename import fsync_dir, rename_noreplace
from memvane.wiki.pipeline.store import _ensure_compile_dir, open_pipeline
from memvane.wiki.pipeline.types import (
    FENCE_GLOBAL_PUBLISH,
    TEMP_PREFIX,
    TEMP_SUFFIX,
    PipelineError,
    PublishOutcome,
)


def _sha256_file(path: Path) -> tuple[int, str] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PipelineError('unsafe_publish_file')
        digest = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
        return info.st_ino, digest.hexdigest()
    finally:
        os.close(fd)


def _validate_target(wiki_root: Path, target_path: Path | str) -> tuple[Path, str]:
    wiki = Path(wiki_root)
    if wiki.is_symlink() or not wiki.is_absolute():
        raise PipelineError('unsafe_wiki_root')
    rel = Path(target_path)
    if rel.is_absolute() or '..' in rel.parts or any(part.startswith('.') for part in rel.parts):
        raise PipelineError('unsafe_target_path')
    target = wiki / rel
    cursor = wiki
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PipelineError('unsafe_target_path')
    if target.is_symlink():
        raise PipelineError('unsafe_target_path')
    return target, rel.as_posix()


@contextmanager
def _card_locks(project_root: Path):
    _ensure_compile_dir(project_root)
    root = Path(project_root)
    card_lock = root / '.memvane/compile/.card-write.lock'
    pub_lock = root / '.topical-publish.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    card_fd = os.open(card_lock, flags, 0o600)
    try:
        fcntl.flock(card_fd, fcntl.LOCK_EX)
        pub_fd = os.open(pub_lock, flags, 0o600)
        try:
            fcntl.flock(pub_fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(pub_fd, fcntl.LOCK_UN)
            finally:
                os.close(pub_fd)
    finally:
        try:
            fcntl.flock(card_fd, fcntl.LOCK_UN)
        finally:
            os.close(card_fd)


def _write_temp(target: Path, temp_name: str, payload: bytes) -> tuple[Path, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(temp_name)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        ino = os.fstat(fd).st_ino
    finally:
        os.close(fd)
    fsync_dir(target.parent)
    return temp_path, ino, hashlib.sha256(payload).hexdigest()


def _unlink_if_present(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _rename_into_place(temp_path: Path, target: Path, *, expected_target_sha: str | None) -> None:
    if expected_target_sha is None:
        parent_fd = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            rename_noreplace(parent_fd, temp_path.name, target.name)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    else:
        os.replace(temp_path, target)
        fsync_dir(target.parent)


def _cas_matches(live_sha: str | None, expected_target_sha: str | None) -> bool:
    if expected_target_sha is None:
        return live_sha is None
    return live_sha == expected_target_sha


def publish_card(project_root: Path | str, wiki_root: Path | str, target_path: Path | str,
                 content: str | bytes, *, expected_target_sha: str | None = None,
                 stage_id: str | None = None, audit_kind: str | None = None,
                 audit_payload: dict | None = None, tx_id: str | None = None,
                 lock_context=None) -> PublishOutcome:
    """Publish a card via same-dir temp, SQLite txn, CAS, and single atomic rename."""
    root, wiki = Path(project_root), Path(wiki_root)
    target, rel = _validate_target(wiki, target_path)
    payload = content.encode('utf-8') if isinstance(content, str) else bytes(content)
    resolved_tx = tx_id or uuid.uuid4().hex
    temp_name = f'{TEMP_PREFIX}{resolved_tx}{TEMP_SUFFIX}'
    temp_path, temp_ino, temp_sha = _write_temp(target, temp_name, payload)

    with open_pipeline(root) as store:
        store.begin_publish_txn(
            rel, temp_name, temp_ino, temp_sha,
            stage_id=stage_id, expected_target_sha=expected_target_sha, tx_id=resolved_tx,
        )
        locks = lock_context() if lock_context is not None else _card_locks(root)
        with locks:
            if store.get_fence(FENCE_GLOBAL_PUBLISH) is not None:
                _unlink_if_present(temp_path)
                store.finish_publish_txn(resolved_tx, 'aborted')
                return PublishOutcome(resolved_tx, rel, 'aborted', displaced_sha=expected_target_sha)
            live = _sha256_file(target)
            live_sha = live[1] if live is not None else None
            if not _cas_matches(live_sha, expected_target_sha):
                _unlink_if_present(temp_path)
                store.finish_publish_txn(resolved_tx, 'conflict_user_edited')
                return PublishOutcome(
                    resolved_tx, rel, 'conflict_user_edited',
                    live_sha=live_sha, displaced_sha=expected_target_sha,
                )
            _rename_into_place(temp_path, target, expected_target_sha=expected_target_sha)
            store.finish_publish_txn(
                resolved_tx, 'published', audit_kind=audit_kind, audit_payload=audit_payload,
            )
            if stage_id is not None:
                store.update_staged(stage_id, 'published')
        return PublishOutcome(
            resolved_tx, rel, 'published', live_sha=temp_sha, displaced_sha=expected_target_sha,
        )


def recover_publishes(project_root: Path | str, wiki_root: Path | str, *,
                      lock_context=None) -> list[PublishOutcome]:
    """Deterministically roll forward, roll back, or finalize interrupted publish txns."""
    root, wiki = Path(project_root), Path(wiki_root)
    outcomes: list[PublishOutcome] = []
    with open_pipeline(root) as store:
        pending = store.open_publish_txns(state='publishing')
        if not pending:
            return outcomes
        locks = lock_context() if lock_context is not None else _card_locks(root)
        with locks:
            for row in pending:
                outcomes.append(_recover_one(store, wiki, row))
    return outcomes


def _recover_one(store, wiki: Path, row: dict) -> PublishOutcome:
    tx_id = row['tx_id']
    target, rel = _validate_target(wiki, row['target_path'])
    temp_path = target.with_name(row['temp_name'])
    temp_info = _sha256_file(temp_path)
    expected = row['expected_target_sha']
    live = _sha256_file(target)
    live_sha = live[1] if live is not None else None

    if temp_info is not None:
        if temp_info != (row['temp_ino'], row['temp_sha']):
            store.finish_publish_txn(tx_id, 'aborted')
            return PublishOutcome(tx_id, rel, 'aborted', live_sha=live_sha, displaced_sha=expected)
        if not _cas_matches(live_sha, expected):
            _unlink_if_present(temp_path)
            store.finish_publish_txn(tx_id, 'conflict_user_edited')
            return PublishOutcome(
                tx_id, rel, 'conflict_user_edited', live_sha=live_sha, displaced_sha=expected,
            )
        _rename_into_place(temp_path, target, expected_target_sha=expected)
        store.finish_publish_txn(tx_id, 'published')
        if row['stage_id'] is not None:
            store.update_staged(row['stage_id'], 'published')
        return PublishOutcome(tx_id, rel, 'published', live_sha=row['temp_sha'], displaced_sha=expected)

    if live_sha == row['temp_sha']:
        store.finish_publish_txn(tx_id, 'published')
        if row['stage_id'] is not None:
            store.update_staged(row['stage_id'], 'published')
        return PublishOutcome(tx_id, rel, 'published', live_sha=live_sha, displaced_sha=expected)

    store.finish_publish_txn(tx_id, 'published_user_edited')
    if row['stage_id'] is not None:
        store.update_staged(row['stage_id'], 'published')
    return PublishOutcome(
        tx_id, rel, 'published_user_edited', live_sha=live_sha, displaced_sha=expected,
    )
