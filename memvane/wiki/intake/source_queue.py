"""Bounded source queue images. Only publish a snapshot while its live name still agrees."""
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from memvane.kernel.atomic_rename import fsync_dir
from memvane.wiki.intake import wiki_source_intent as intent
from memvane.wiki.workspace.api import queue_lock_path

SLAB = 64 * 1024
MAX_ROWS = 4096


class HandoffRequired(OSError):
    """The public bytes must stay in place until a bounded batch reader takes ownership."""


def _identity(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _named(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError('source_queue_not_regular')
    return info


def _open_read(path):
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')


def _unique_keys(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError('ambiguous_source_row')
    return obj


def read_live(path, limit, keys=None):
    info = _named(path)
    lines, groups = [], {}
    if info is None:
        return info, lines, groups
    if info.st_size > limit:
        raise HandoffRequired('source_queue_read_limit')
    with _open_read(path) as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(info):
            raise BlockingIOError('source_queue_changed')
        offset = 0
        while offset < info.st_size:
            if len(lines) >= MAX_ROWS:
                raise HandoffRequired('source_queue_row_limit')
            raw = handle.readline(min(SLAB, info.st_size - offset))
            if not raw.endswith(b'\n'):
                raise HandoffRequired('source_queue_incomplete_or_oversized_row')
            offset += len(raw)
            index = len(lines)
            lines.append(raw)
            try:
                text = raw.decode('utf-8')
                if len(text.splitlines()) != 1:
                    continue
                row = json.loads(text, object_pairs_hook=_unique_keys)
            except (ValueError, RecursionError):
                continue
            key = intent.live_key(row) if keys is None else intent.operation_key(row)
            if key is not None and (keys is None or key in keys):
                groups.setdefault(key, []).append((index, row))
        if _identity(os.fstat(handle.fileno())) != _identity(info):
            raise BlockingIOError('source_queue_changed')
    return info, lines, groups


def _merge_rows(lines, groups, records):
    for row in records:
        group = groups.get(intent.operation_key(row), ())
        row = intent.merge_source_job(row, (old for _, old in group))
        text = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if len(text.splitlines()) != 1:
            raise HandoffRequired('source_queue_incompatible_row')
        try:
            raw = (text + '\n').encode('utf-8')
        except UnicodeEncodeError as exc:
            raise HandoffRequired('source_queue_incompatible_row') from exc
        if len(raw) > SLAB:
            raise HandoffRequired('source_queue_oversized_row')
        if group:
            lines[group[0][0]] = raw
            for index, _ in group[1:]:
                lines[index] = b''
        else:
            lines.append(raw)


def publish(path, original, image=None, verified=None):
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(queue_lock_path(path), flags, 0o600), 'r+b') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise OSError('source_queue_lock_not_regular')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _identity(_named(path)) != _identity(original):
            raise BlockingIOError('source_queue_changed')
        if image is None:
            # A previous producer may have renamed the same revision before its directory sync.
            with _open_read(path) as handle:
                if _identity(os.fstat(handle.fileno())) != _identity(original):
                    raise BlockingIOError('source_queue_changed')
                os.fsync(handle.fileno())
        else:
            if _identity(_named(Path(image.name))) != verified:
                raise OSError('source_queue_image_changed')
            os.replace(image.name, path)
        fsync_dir(path.parent)


def _verify_image(image, size, digest):
    identity = _identity(os.fstat(image.fileno()))
    image.seek(0)
    observed = hashlib.sha256()
    for remaining in range(size, 0, -SLAB):
        observed.update(image.read(min(remaining, SLAB)))
    info = os.fstat(image.fileno())
    if info.st_size != size or _identity(info) != identity or observed.digest() != digest:
        raise OSError('source_queue_image_mismatch')
    return identity


def store(path, original, lines, groups, records, limit):
    _merge_rows(lines, groups, records)
    size = sum(map(len, lines))
    if sum(bool(raw) for raw in lines) > MAX_ROWS:
        raise HandoffRequired('source_queue_row_limit')
    # The original read and the verification read share the producer's byte allowance.
    if size + (original.st_size if original else 0) > limit:
        raise HandoffRequired('source_queue_read_limit')
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.source-queue-', delete=False) as image:
        try:
            if original is not None:
                os.fchmod(image.fileno(), stat.S_IMODE(original.st_mode))
            expected = hashlib.sha256()
            for raw in lines:
                image.write(raw)
                expected.update(raw)
            image.flush()
            os.fsync(image.fileno())
            verified = _verify_image(image, size, expected.digest())
            publish(path, original, image, verified)
        finally:
            try:
                name = Path(image.name)
                if os.path.samestat(name.lstat(), os.fstat(image.fileno())):
                    name.unlink()
            except FileNotFoundError:
                pass
