"""Disposable owner-private receipts for unchanged verified view generations.

A receipt is an optimization, not a reader authority. It is unusable while a
SQLite sidecar exists, and every read compares the current database identity.
"""
import json
import os
from pathlib import Path
import stat

from memvane.kernel.atomic_rename import atomic_json, private_state
from memvane.kernel.memvane_version import plugin_version

SCHEMA = 'memvane-pin-receipt-v1'


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_receipt_key')
        result[key] = value
    return result


def identity(path):
    try:
        path = Path(path)
        private_state(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            return None
        if any(os.path.lexists(str(path) + suffix) for suffix in ('-journal', '-wal', '-shm')):
            return None
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    except (OSError, ValueError):
        return None


def _path(path, view_id):
    if len(view_id) != 64 or any(c not in '0123456789abcdef' for c in view_id):
        raise ValueError('invalid_view')
    return Path(path).parent / ('.view-pin-receipt-' + view_id + '.json')


def matches(path, view_id, incarnation, gen_id, seal, node):
    if node is None:
        return False
    try:
        target = _path(path, view_id)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                return False
            raw = os.read(fd, 4097)
            after = os.fstat(fd)
            if (len(raw) != info.st_size or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) !=
                    (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                return False
        finally:
            os.close(fd)
        expected = {'schema': SCHEMA, 'version': plugin_version(), 'view': view_id,
                    'incarnation': incarnation, 'generation': gen_id, 'seal': seal, 'database': node}
        return (json.loads(raw.decode('utf8'), object_pairs_hook=_unique_pairs) == expected
                and identity(path) == node)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def save(path, view_id, incarnation, gen_id, seal, node):
    if node is None or identity(path) != node:
        return
    try:
        target = _path(path, view_id)
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            return
        atomic_json(target, {'schema': SCHEMA, 'version': plugin_version(), 'view': view_id,
                    'incarnation': incarnation, 'generation': gen_id, 'seal': seal, 'database': node})
    except (OSError, ValueError, TypeError):
        pass  # A missing receipt always means full verification next time.


def verified(store, db, head, live_handle, deadline, record=None):
    from memvane.store.view.view_rows import seal_digest, _seal_fields
    stamp = store._commit_stamp(db)
    cached = None if record is None else record.get('verified')
    if cached is not None and cached['stamp'] == stamp and cached['ordinal'] == head.ordinal:
        return cached['generation'], cached['capture']
    node = identity(store._path) if store._reader_only and record is not None else None
    row = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                     (live_handle.view_id, head.ordinal)).fetchone() if node is not None else None
    generation = dict(row) if row is not None else None
    if (generation is not None and generation['gen_id'] == head.gen_id
            and generation['epoch'] == head.epoch and generation['policy_digest'] == store._policy
            and generation['coverage'] in ('lexical', 'complete')
            and generation['kind'] in ('normal', 'rollback')
            and seal_digest(_seal_fields(store._incarnation, generation)) == generation['seal_digest']
            and matches(store._path, live_handle.view_id, store._incarnation,
                                    head.gen_id, generation['seal_digest'], node)):
        _handle, entry, _saved = store._sealed_handle(
            db, dict(checkout_evidence=generation['capture_json'], view_id=generation['view_id']), live_handle)
        capture = entry['capture']
        pin_verified = 'receipt'
    else:
        generation, capture = store._rows_verify(db, head, live_handle, deadline)
        pin_verified = 'full'
        if node is not None and store._commit_stamp(db) == stamp:
            save(store._path, live_handle.view_id, store._incarnation,
                             head.gen_id, generation['seal_digest'], node)
    if record is not None:
        record['verified'] = dict(stamp=stamp, ordinal=head.ordinal, generation=generation,
                                  capture=capture, pinVerified=pin_verified)
    return generation, capture
