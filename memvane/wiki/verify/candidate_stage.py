"""Create and cancel one unpublished replacement under the card and queue locks."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re
import uuid

from memvane.kernel.atomic_rename import rename_noreplace
from memvane.store.api import regular


_ACTIVE = ContextVar('wiki_candidate_producer', default=None)


def _names(work):
    job = work.state['binding']['job']
    marker, rel = job['sourceHash'], job['targetPath']
    if (not isinstance(marker, str) or not re.fullmatch('[0-9a-f]{16}', marker)
            or not isinstance(rel, str) or Path(rel).is_absolute() or Path(rel).as_posix() != rel
            or '..' in Path(rel).parts or not Path(rel).name or Path(rel).name.startswith('.')):
        raise ValueError('unsafe candidate binding')
    target = Path(rel)
    final = target.with_name('.' + target.name + '.pending-' + marker)
    if final.as_posix() != job['pendingPath']:
        raise ValueError('candidate path differs from binding')
    return marker, final


def _check_record(work, path, data):
    marker, final = _names(work)
    record = work.state['candidate']
    stage = record.get('stage')
    keys = {'stage', 'path', 'marker', 'tmp', 'parentInode', 'length', 'sha256'}
    if stage in ('created', 'ready', 'staged'):
        keys.update(('inode', 'mtime'))
        if type(record.get('inode')) is not int or type(record.get('mtime')) is not int:
            raise ValueError('unknown candidate identity')
    elif stage != 'reserved':
        raise ValueError('unknown candidate stage')
    if 'cancelFrom' in record:
        if work.state['phase'] != 'cancelled' or record['cancelFrom'] not in ('tmp', 'final'):
            raise ValueError('invalid candidate cancellation')
        keys.add('cancelFrom')
        if 'detached' in record:
            if record['detached'] is not True:
                raise ValueError('invalid detached candidate')
            keys.add('detached')
    if (set(record) != keys or record.get('path') != final.as_posix() or record.get('marker') != marker
            or not isinstance(record.get('tmp'), str) or not re.fullmatch(r'\.pc-[0-9a-f]{32}', record['tmp'])
            or record.get('sha256') != hashlib.sha256(data).hexdigest() or record.get('length') != len(data)
            or path.parts[-len(final.parts):] != final.parts or path.parent.resolve() != path.parent
            or record.get('parentInode') != path.parent.stat().st_ino):
        raise ValueError('candidate reservation changed')
    return record


def _present(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def _parent(path, inode):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if path.parent.resolve() != path.parent or os.fstat(fd).st_ino != inode:
            raise ValueError('candidate parent changed')
        yield fd
    finally:
        os.close(fd)


def _proof(path, limit):
    raw, node, _ = regular(path.parent, path.name, limit)
    return raw, node['ino'], node['mtime_ns']


def _stage(work, path, data):
    marker, final = _names(work)
    if (not path.is_absolute() or path.parts[-len(final.parts):] != final.parts
            or not work.queue.is_relative_to(path.parents[len(final.parts) - 1])
            or path.parent.resolve() != path.parent):
        raise ValueError('candidate parent changed')
    if work.state['phase'] == 'staged':
        record = _check_record(work, path, data)
        if record['stage'] == 'staged' and _proof(path, len(data)) == (data, record['inode'], record['mtime']):
            return
        raise ValueError('staged candidate identity changed')
    if work.state['phase'] != 'reserved' or 'candidate' in work.state:
        raise ValueError('candidate creation already attempted')
    record = dict(stage='reserved', path=final.as_posix(), marker=marker, tmp='.pc-' + uuid.uuid4().hex,
                  parentInode=path.parent.stat().st_ino, length=len(data), sha256=hashlib.sha256(data).hexdigest())
    work.commit('creating', candidate=record)
    _check_record(work, path, data)
    if _present(path):
        raise FileExistsError('candidate already occupied')

    tmp = path.with_name(record['tmp'])
    with _parent(path, record['parentInode']) as parent:
        fd = os.open(tmp.name, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=parent)
    with os.fdopen(fd, 'wb') as stream:
        info = os.fstat(stream.fileno())
        created = dict(record, stage='created', inode=info.st_ino, mtime=info.st_mtime_ns)
        work.commit('creating', candidate=created)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    with _parent(path, record['parentInode']) as parent:
        os.fsync(parent)
    raw, inode, mtime = _proof(tmp, len(data))
    if raw != data or inode != created['inode']:
        raise ValueError('candidate temporary file changed')
    ready = dict(created, stage='ready', mtime=mtime)
    work.commit('creating', candidate=ready)
    _check_record(work, path, data)
    if _proof(tmp, len(data)) != (data, inode, mtime):
        raise ValueError('candidate temporary file changed')
    with _parent(path, record['parentInode']) as parent:
        rename_noreplace(parent, tmp.name, path.name)
        os.fsync(parent)
    if _proof(path, len(data)) != (data, inode, mtime):
        raise ValueError('published candidate identity changed')
    work.commit('staged', candidate=dict(ready, stage='staged'))


@contextmanager
def producer(work, path, produced_bytes):
    """Bind the writer callback to this operation and re-read its output before registration."""
    if work.read('image') != produced_bytes:
        raise ValueError('candidate does not match reserved image')
    token = _ACTIVE.set((work, path, produced_bytes))
    try:
        yield
        if work.state['phase'] in ('cancelled', 'finished'):
            return
        record = _check_record(work, path, produced_bytes)
        if (work.state['phase'] != 'staged' or record.get('stage') != 'staged'
                or _proof(path, len(produced_bytes)) != (produced_bytes, record['inode'], record['mtime'])):
            raise ValueError('candidate changed before queue publication')
    finally:
        _ACTIVE.reset(token)


def write(path, raw):
    active = _ACTIVE.get()
    if active is None or active[1:] != (path, raw):
        return False
    try:
        _stage(active[0], path, raw)
    except (OSError, ValueError):
        return False
    return True


def cancel(work, root):
    """Drop only an unpublished inode created by this cancelled work. Keep inconclusive files."""
    try:
        return _cancel(work, root)
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _cancel(work, root):
    if work.state['phase'] != 'cancelled':
        return False
    if 'candidate' not in work.state:
        return True  # No candidate name was offered to O_EXCL.
    _, rel = _names(work)
    final = root / rel
    data = work.read('image')
    record = _check_record(work, final, data)
    tmp = final.with_name(record['tmp'])
    stage = record.get('stage')
    if stage == 'reserved':
        return not _present(tmp)  # No rename could precede the creator record.
    quarantine = final.with_name('.px-' + work.state['id'])
    with _parent(final, record['parentInode']) as parent:
        if 'cancelFrom' not in record:
            if _present(quarantine):
                return False
            origin = 'tmp' if _present(tmp) else 'final'
            source = tmp if origin == 'tmp' else final
            if not _present(source):
                os.fsync(parent)
                return True
            if not _owned(source, origin, record, data):
                return False
            record = dict(record, cancelFrom=origin)
            work.commit('cancelled', candidate=record)
        origin = record['cancelFrom']
        source = tmp if origin == 'tmp' else final
        if not _present(quarantine):
            if record.get('detached'):
                os.fsync(parent)
                return True
            if not _owned(source, origin, record, data):
                return False
            rename_noreplace(parent, source.name, quarantine.name)
            os.fsync(parent)
            if not _owned(quarantine, origin, record, data):
                # The namespace changed after inspection. Restore only what this call moved.
                if not _present(source):
                    rename_noreplace(parent, quarantine.name, source.name)
                    os.fsync(parent)
                return False
        if not _owned(quarantine, origin, record, data):
            return False
        if not record.get('detached'):
            os.fsync(parent)
            work.commit('cancelled', candidate=dict(record, detached=True))
        if not _owned(quarantine, origin, record, data):
            return False
        os.unlink(quarantine.name, dir_fd=parent)
        os.fsync(parent)
    return True


def _owned(path, origin, record, data):
    try:
        raw, inode, mtime = _proof(path, len(data))
    except (OSError, ValueError):
        return False
    if inode != record['inode']:
        return False
    stage = record['stage']
    if stage == 'created':
        return origin == 'tmp' and (raw == data or raw == b'' and mtime == record['mtime'])
    return raw == data and mtime == record['mtime'] and (stage == 'ready' or stage == 'staged' and origin == 'final')
