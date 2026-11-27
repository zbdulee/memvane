"""Single-rename card publication and deterministic crash recovery."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import time
import uuid

from memvane.kernel.atomic_rename import fsync_dir, rename_exchange, rename_noreplace
from memvane.wiki.pipeline.store import _ensure_compile_dir, open_pipeline
from memvane.wiki.pipeline.types import (
    EXCHANGE_UNSUPPORTED,
    FENCE_GLOBAL_PUBLISH,
    PUBLISH_STATES,
    TEMP_PREFIX,
    TEMP_SUFFIX,
    PipelineError,
    PublishOutcome,
)


def _sha256_file(path: Path) -> tuple[int, str] | None:
    try:
        # O_NONBLOCK: a FIFO put at a card or temp path would block a read-only open forever.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
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


def _write_temp(target: Path, temp_name: str, payload: bytes, mode: int = 0o600) -> tuple[Path, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(temp_name)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        if mode != 0o600:
            os.fchmod(fd, mode)
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


_TEMP_NAME = re.compile(r'\.publish-([0-9a-f]{32})\.tmp\Z')
ORPHAN_TEMP_MIN_AGE_SECONDS = 60.0


def _parent_fd(target: Path) -> int:
    return os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))


def _create_into_place(temp_path: Path, target: Path) -> None:
    parent = _parent_fd(target)
    try:
        rename_noreplace(parent, temp_path.name, target.name)
        os.fsync(parent)
    finally:
        os.close(parent)


def _exchange(temp_path: Path, target: Path) -> None:
    parent = _parent_fd(target)
    try:
        rename_exchange(parent, temp_path.name, parent, target.name)
        os.fsync(parent)
    finally:
        os.close(parent)


def _settle_exchange(temp_path: Path, target: Path, expected: str) -> str:
    """After temp<->live exchange the displaced file sits at the temp name.

    The locks are advisory, so an editor can save between the CAS read and the
    exchange. The displaced bytes then differ from ``expected``: swap back so the
    person's save is live again. The caller closes the txn, then removes the temp
    (``_finish_then_drop_temp``).
    """
    displaced = _sha256_file(temp_path)
    if displaced is not None and displaced[1] != expected:
        _exchange(temp_path, target)
        return 'conflict_user_edited'
    return 'published'


def _finish_then_drop_temp(store, tx_id: str, state: str, temp_path: Path, **audit) -> None:
    """Close the txn before removing its temp.

    The other order lets a stop between the two read as "renamed, then a person
    edited": recovery would close a conflict or abort as ``published_user_edited``
    and record a generation for a card that never landed. A temp left behind a
    closed txn is the orphan sweep's to remove.
    """
    store.finish_publish_txn(tx_id, state, **audit)
    _unlink_if_present(temp_path)
    fsync_dir(temp_path.parent)


# Errors the OS returns when the file system (or kernel) has no atomic exchange.
EXCHANGE_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in ('EINVAL', 'ENOTSUP', 'EOPNOTSUPP', 'ENOSYS'))
    if code is not None
)


def _cas_matches(live_sha: str | None, expected_target_sha: str | None) -> bool:
    if expected_target_sha is None:
        return live_sha is None
    return live_sha == expected_target_sha


def _apply(store, tx_id: str, rel: str, temp_path: Path, target: Path, temp_sha: str,
           expected: str | None, audit_kind: str | None, audit_payload: dict | None) -> PublishOutcome:
    live = _sha256_file(target)
    live_sha = live[1] if live is not None else None
    if live_sha == temp_sha:
        # Same bytes already live (a same-second repeat or a replayed publish).
        _finish_then_drop_temp(store, tx_id, 'published', temp_path,
                               audit_kind=audit_kind, audit_payload=audit_payload)
        return PublishOutcome(tx_id, rel, 'published', live_sha=live_sha, displaced_sha=expected)
    if not _cas_matches(live_sha, expected):
        _finish_then_drop_temp(store, tx_id, 'conflict_user_edited', temp_path)
        return PublishOutcome(tx_id, rel, 'conflict_user_edited', live_sha=live_sha, displaced_sha=expected)
    if expected is None:
        try:
            _create_into_place(temp_path, target)
        except FileExistsError:
            # A person created the path after the CAS read; keep their file.
            _finish_then_drop_temp(store, tx_id, 'conflict_user_edited', temp_path)
            current = _sha256_file(target)
            return PublishOutcome(tx_id, rel, 'conflict_user_edited',
                                  live_sha=current[1] if current else None, displaced_sha=None)
        state = 'published'
    else:
        try:
            _exchange(temp_path, target)
        except OSError as exc:
            if exc.errno not in EXCHANGE_UNSUPPORTED_ERRNOS:
                raise
            # 교환을 지원하지 않는 파일 시스템에서는 교체 게시를 하지 않는다. 교환은 일어나지 않았으므로
            # temp는 이 txn의 후보 바이트이고, live 카드는 손대지 않은 채 남는다(사람 편집을 잃지 않는다).
            # 대안(구현하지 않음): live를 하드링크로 백업한 뒤 os.replace로 덮고, 백업 해시가 expected와
            # 다르면 백업을 되돌린다. 편집기가 새 파일을 만들어 rename으로 저장하면 백업 링크와 replace
            # 사이에 들어온 그 저장은 백업에 잡히지 않아, 이 짧은 창에서 유실될 수 있다.
            _finish_then_drop_temp(store, tx_id, 'aborted', temp_path)
            return PublishOutcome(tx_id, rel, 'aborted', live_sha=live_sha, displaced_sha=expected,
                                  reason=EXCHANGE_UNSUPPORTED)
        state = _settle_exchange(temp_path, target, expected)
    if state == 'published':
        if expected is None:
            store.finish_publish_txn(tx_id, 'published', audit_kind=audit_kind, audit_payload=audit_payload)
        else:
            # The temp name holds the displaced card the CAS matched.
            _finish_then_drop_temp(store, tx_id, 'published', temp_path,
                                   audit_kind=audit_kind, audit_payload=audit_payload)
        return PublishOutcome(tx_id, rel, 'published', live_sha=temp_sha, displaced_sha=expected)
    _finish_then_drop_temp(store, tx_id, state, temp_path)
    current = _sha256_file(target)
    return PublishOutcome(tx_id, rel, state, live_sha=current[1] if current else None, displaced_sha=expected)


def publish_card(project_root: Path | str, wiki_root: Path | str, target_path: Path | str,
                 content: str | bytes, *, expected_target_sha: str | None = None,
                 audit_kind: str | None = None,
                 audit_payload: dict | None = None, tx_id: str | None = None,
                 lock_context=None, generation: dict | None = None) -> PublishOutcome:
    """Publish a card via same-dir temp, SQLite txn, CAS, and one atomic rename or exchange.

    Product callers pass ``lock_context``: the pipeline cannot import the workspace
    locks, and a caller that already holds the card lock would deadlock on a second
    flock of the same file. The default opens both lock files and serves tests.
    """
    root, wiki = Path(project_root), Path(wiki_root)
    target, rel = _validate_target(wiki, target_path)
    payload = content.encode('utf-8') if isinstance(content, str) else bytes(content)
    resolved_tx = tx_id or uuid.uuid4().hex
    temp_name = f'{TEMP_PREFIX}{resolved_tx}{TEMP_SUFFIX}'
    mode = 0o600
    if expected_target_sha is not None:
        # A replacement keeps the permission bits of the card it replaces.
        try:
            mode = stat.S_IMODE(target.lstat().st_mode)
        except FileNotFoundError:
            pass
    temp_path, temp_ino, temp_sha = _write_temp(target, temp_name, payload, mode)

    with open_pipeline(root) as store:
        store.begin_publish_txn(
            rel, temp_name, temp_ino, temp_sha,
            expected_target_sha=expected_target_sha, tx_id=resolved_tx,
            generation=generation,
        )
        locks = lock_context() if lock_context is not None else _card_locks(root)
        with locks:
            if store.get_fence(FENCE_GLOBAL_PUBLISH) is not None:
                _finish_then_drop_temp(store, resolved_tx, 'aborted', temp_path)
                return PublishOutcome(resolved_tx, rel, 'aborted', displaced_sha=expected_target_sha)
            return _apply(store, resolved_tx, rel, temp_path, target, temp_sha,
                          expected_target_sha, audit_kind, audit_payload)


def recover_publishes(project_root: Path | str, wiki_root: Path | str, *,
                      lock_context=None) -> list[PublishOutcome]:
    """Deterministically roll forward, roll back, or finalize interrupted publish txns,
    then remove ``.publish-*.tmp`` files that no open txn owns.

    One txn that cannot be recovered (its folder became a symlink, its path a
    directory or FIFO, its parent a file, or the exchange fails for a reason other
    than ``exchange_unsupported``) is closed ``aborted`` with its files left in
    place, so the other txns and the caller's verify pass still run. Store errors
    still propagate.
    """
    root, wiki = Path(project_root), Path(wiki_root)
    outcomes: list[PublishOutcome] = []
    with open_pipeline(root) as store:
        pending = store.open_publish_txns(state='publishing')
        locks = lock_context() if lock_context is not None else _card_locks(root)
        with locks:
            for row in pending:
                try:
                    outcomes.append(_recover_one(store, wiki, row))
                except (PipelineError, OSError) as exc:
                    store.finish_publish_txn(row['tx_id'], 'aborted')
                    outcomes.append(PublishOutcome(
                        row['tx_id'], row['target_path'], 'aborted',
                        displaced_sha=row['expected_target_sha'],
                        reason='unrecoverable:' + _error_code(exc),
                    ))
            _sweep_orphan_temps(store, wiki)
    return outcomes


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno is not None:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    return str(exc) or type(exc).__name__


def _replaceable_leftover(path: Path, row: dict) -> bool:
    """The txn's own candidate, or after a published exchange the card the CAS matched."""
    try:
        info = _sha256_file(path)
    except (PipelineError, OSError):
        return False
    if info is None:
        return False
    if info == (row['temp_ino'], row['temp_sha']):
        return True
    return row['state'] == 'published' and info[1] == row['expected_target_sha']


def _sweep_orphan_temps(store, wiki: Path) -> int:
    """Remove temps no open txn will ever rename.

    A temp written before its txn commit has no row. A temp behind a closed txn
    goes only when it holds that txn's own candidate bytes, which a later publish
    can write again, or the card a published exchange displaced after its CAS
    matched; any other file there (an editor's save, an unknown file) stays.
    """
    if not wiki.is_dir() or wiki.is_symlink():
        return 0
    owned = {row['temp_name'] for row in store.open_publish_txns(state='publishing')}
    now = time.time()
    leftovers: list[Path] = []
    for directory, dirnames, filenames in os.walk(wiki, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for name in filenames:
            if not _TEMP_NAME.fullmatch(name) or name in owned:
                continue
            path = Path(directory) / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime >= ORPHAN_TEMP_MIN_AGE_SECONDS:
                leftovers.append(path)
    if not leftovers:
        return 0
    closed = {row['temp_name']: row for state in PUBLISH_STATES if state != 'publishing'
              for row in store.open_publish_txns(state=state)}
    removed = 0
    for path in leftovers:
        row = closed.get(path.name)
        if row is not None and not _replaceable_leftover(path, row):
            continue
        try:
            _unlink_if_present(path)
        except OSError:
            # 3번 기준: 남은 temp는 해가 없고 다음 회차가 다시 지운다. 실패로 검수를 막지 않는다.
            continue
        removed += 1
    return removed


def _recover_one(store, wiki: Path, row: dict) -> PublishOutcome:
    tx_id = row['tx_id']
    target, rel = _validate_target(wiki, row['target_path'])
    temp_path = target.with_name(row['temp_name'])
    temp_info = _sha256_file(temp_path)
    expected = row['expected_target_sha']
    live = _sha256_file(target)
    live_sha = live[1] if live is not None else None
    ours = (row['temp_ino'], row['temp_sha'])

    if temp_info is not None and temp_info == ours:
        # Stopped before the rename or exchange.
        return _apply(store, tx_id, rel, temp_path, target, row['temp_sha'], expected, None, None)
    if temp_info is not None and live == ours and expected is not None:
        # Stopped after the exchange: the temp name holds the displaced file.
        state = _settle_exchange(temp_path, target, expected)
        _finish_then_drop_temp(store, tx_id, state, temp_path)
        current = _sha256_file(target)
        return PublishOutcome(tx_id, rel, state, live_sha=current[1] if current else None, displaced_sha=expected)
    if temp_info is not None:
        # Neither ours nor an exchange we made: keep the unknown file.
        store.finish_publish_txn(tx_id, 'aborted')
        return PublishOutcome(tx_id, rel, 'aborted', live_sha=live_sha, displaced_sha=expected)
    if live_sha == row['temp_sha']:
        store.finish_publish_txn(tx_id, 'published')
        return PublishOutcome(tx_id, rel, 'published', live_sha=live_sha, displaced_sha=expected)
    # Renamed, then a person edited or deleted the card: never overwrite or recreate it.
    store.finish_publish_txn(tx_id, 'published_user_edited')
    return PublishOutcome(tx_id, rel, 'published_user_edited', live_sha=live_sha, displaced_sha=expected)
