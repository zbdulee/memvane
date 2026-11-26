"""Versioned, owner-private verify handoffs stored in ``wiki-pipeline.sqlite3``.

Mutators share the caller's existing queue/card lock; each mutation is one SQLite
transaction with a compare-and-swap on the canonical control JSON.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from memvane.kernel.atomic_rename import canonical_json_bytes, rename_noreplace

SLAB = 65536
CONTROL_LIMIT = 1024 ** 2
BLOB_LIMIT = {'raw': 8 * 1024 ** 2, 'image': 4 * 1024 ** 2}
KINDS = ('publication', 'legacy', 'hold')
ORPHAN_MIN_AGE_SECONDS = 60.0
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
HEX_ID = re.compile(r'[a-f0-9]{32}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
STAGED = 'staged'  # .handoff/v1/staged/<id>/raw holds an inode-stored batch raw

# Bindings, blob identities and work IDs never change. Evidence is added only by
# the kind and phase that own it; finishing preserves already committed proof.
_FIELDS = {
    'publication': {'candidate': dict, 'queueBefore': dict, 'swapDigest': 'sha', 'swapNode': dict,
                    'retired': list, 'holds': list},
    'legacy': {'sourceDigest': 'sha', 'proof': dict, 'imported': dict},
    'hold': {'cursor': int, 'offset': int, 'length': int, 'sourceNode': dict,
             'transaction': str, 'proof': dict},
}
_NEXT = {
    'publication': {'reserved': ('creating', 'cancelled'), 'creating': ('staged', 'cancelled'),
                    'staged': ('prepared', 'cancelled'), 'prepared': ('published', 'cancelled'),
                    'published': ('finished',), 'cancelled': ('finished',), 'finished': ()},
    'legacy': {'reserved': ('imported',), 'imported': ('finished',), 'finished': ()},
    'hold': {'reserved': ('held', 'manual'), 'held': ('published', 'finished', 'manual'),
             'published': ('finished',), 'manual': ('held', 'finished'), 'finished': ()},
}

_UPDATES = {
    'publication': {'creating': {'candidate'}, 'staged': {'candidate', 'swapNode'},
                    'prepared': {'queueBefore', 'swapNode', 'swapDigest', 'retired', 'holds'},
                    'cancelled': {'candidate'}},
    'legacy': {'imported': {'sourceDigest', 'proof', 'imported'}},
    'hold': {'held': set(_FIELDS['hold']), 'manual': {'sourceNode', 'proof', 'transaction'},
             'published': {'proof', 'transaction'}, 'finished': {'proof'}},
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_value(value):
    if isinstance(value, dict):
        return all(type(k) is str and _json_value(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_json_value(v) for v in value)
    return (type(value) in (str, int, bool, type(None)) or
            type(value) is float and math.isfinite(value))


@contextmanager
def _opened(name, flags, *, parent=None, mode=0o600):
    fd = os.open(name, flags, mode, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _directory(parent, name, create=False, *, exclusive=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            if exclusive:
                raise
        os.fsync(parent)  # A prior creator can have stopped before this sync.
    with _opened(name, DIR, parent=parent) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError('unsafe_handoff_directory')
        yield fd
        if not os.path.samestat(info, os.stat(name, dir_fd=parent, follow_symlinks=False)):
            raise OSError('handoff_directory_changed')


@contextmanager
def _hidden(queue, create=False):
    base = Path(queue).parent.absolute()
    if base.resolve(strict=True) != base:
        raise OSError('unsafe_handoff_queue_parent')
    with _opened(base, DIR) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError('unsafe_handoff_queue_parent')
        with _directory(parent, '.handoff', create) as hidden:
            yield hidden


@contextmanager
def private_root(queue, *, create=False):
    """Yield a pinned .handoff/v1 dir FD; a missing read-only tree raises FileNotFoundError."""
    with _hidden(queue, create) as hidden, _directory(hidden, 'v1', create) as version:
        yield version


def _file(parent, name, limit, *, keep=True, expected=None, lane=None, charge_budget=True):
    with _opened(name, READ, parent=parent) as fd:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit):
            raise OSError('unsafe_handoff_file')
        if expected is not None and info.st_size != expected['length']:
            raise OSError('handoff_length_changed')
        if charge_budget:
            from memvane.wiki.workspace.handoff.api import charge
            charge(lane or ('evidence' if name == 'image' else 'private'), info.st_size)
        digest = hashlib.sha256()
        blocks = []
        remaining = info.st_size
        while remaining:
            block = os.read(fd, min(SLAB, remaining))
            if not block:
                raise OSError('handoff_short_read')
            digest.update(block)
            if keep:
                blocks.append(block)
            remaining -= len(block)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not os.path.samestat(info, named) or info.st_size != named.st_size or
                info.st_ctime_ns != named.st_ctime_ns or info.st_mtime_ns != named.st_mtime_ns):
            raise OSError('handoff_file_changed')
        if expected is not None and digest.hexdigest() != expected['sha256']:
            raise OSError('handoff_digest_changed')
        return b''.join(blocks)


def _publish(parent, name, data, *, replace=False, charge_budget=True):
    temp = '.w-' + uuid.uuid4().hex
    try:
        with _opened(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, parent=parent) as fd:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('handoff_short_write')
                view = view[written:]
            os.fsync(fd)
        _file(parent, temp, len(data), keep=False, expected={'length': len(data), 'sha256': _sha(data)},
              lane='evidence' if name == 'image' else 'private', charge_budget=charge_budget)
        if replace:
            os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            rename_noreplace(parent, temp, name)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=parent)
        except OSError:
            pass
        raise
    os.fsync(parent)


def _unique(pairs):
    result = dict(pairs)
    if len(pairs) != len(result):
        raise ValueError('handoff_duplicate_key')
    return result


_BASE = {'version', 'id', 'kind', 'queue', 'binding', 'phase', 'blobs'}


def _validate(state, kind, identity):
    if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1:
        raise ValueError('unknown_handoff_version')
    if (state.get('kind') != kind or state.get('id') != identity or not _BASE <= state.keys() or
            not isinstance(state['queue'], str) or state['queue'] in ('', '.', '..') or
            Path(state['queue']).name != state['queue'] or not isinstance(state['binding'], dict) or
            not _json_value(state) or state['phase'] not in _NEXT[kind]):
        raise ValueError('invalid_handoff_control')
    blobs = state['blobs']
    if not isinstance(blobs, dict) or set(blobs) not in ({'raw'}, {'raw', 'image'}):
        raise ValueError('invalid_handoff_blobs')
    for name, meta in blobs.items():
        if isinstance(meta, dict) and meta.get('storage') == 'inode':
            if (kind != 'hold' or name != 'raw' or set(meta) != {'storage', 'ino', 'length'}
                    or type(meta['ino']) is not int or meta['ino'] <= 0
                    or type(meta['length']) is not int or meta['length'] < 0):
                raise ValueError('invalid_handoff_inode')
            continue
        if (not isinstance(meta, dict) or set(meta) != {'length', 'sha256'} or
                type(meta['length']) is not int or not 0 <= meta['length'] <= BLOB_LIMIT[name] or
                not isinstance(meta['sha256'], str) or not SHA.fullmatch(meta['sha256'])):
            raise ValueError('invalid_handoff_blob')
    acquired = state.keys() - _BASE
    if (state['phase'] == 'reserved' and acquired or kind == 'publication' and
            state['phase'] in ('creating', 'staged') and acquired - _UPDATES[kind][state['phase']]):
        raise ValueError('handoff_fields_before_phase')
    for key in acquired:
        expected = _FIELDS[kind].get(key)
        value = state[key]
        if expected == 'sha':
            valid = type(value) is str and SHA.fullmatch(value)
        else:
            valid = type(value) is expected and (expected is not int or value >= 0)
        if not valid:
            raise ValueError('invalid_handoff_update_' + key)


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False




def _canonical(state):
    return canonical_json_bytes(state).decode('utf-8')


def _charge(kind, binding, blobs):
    # A newly created card's initial control, or the source inode, is its primary authority.
    if (kind == 'legacy' or binding.get('action') in ('create', 'borrow') or
            blobs['raw'].get('storage') == 'inode'):
        return 0
    return sum(blob['length'] for blob in blobs.values())


def _charge_read(name, size):
    from memvane.wiki.workspace.handoff.api import charge
    charge('evidence' if name == 'image' else 'private', size)


def _target(binding, raw):
    value = binding.get('targetPath')
    if isinstance(value, str) and value:
        return value
    if not raw:
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    value = row.get('targetPath') if isinstance(row, dict) else None
    return value if isinstance(value, str) and value else None


def _store(queue, *, readonly=False):
    from memvane.wiki.pipeline.api import open_pipeline_dir
    from memvane.wiki.pipeline.types import PipelineError
    directory = Path(queue).parent.absolute()
    if directory.resolve(strict=True) != directory:
        raise OSError('unsafe_handoff_queue_parent')
    info = os.stat(directory)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise OSError('unsafe_handoff_queue_parent')
    try:
        return open_pipeline_dir(directory, readonly=readonly)
    except PipelineError as exc:
        raise OSError(str(exc)) from exc


@contextmanager
def _reader(queue):
    """Yield a read-only store, or None when no handoff has been recorded yet."""
    try:
        store = _store(queue, readonly=True)
    except FileNotFoundError:
        yield None
        return
    with store:
        yield store


def _decode(row):
    state = json.loads(row['control_json'], object_pairs_hook=_unique)
    _validate(state, row['kind'], row['stage_id'])
    if state['phase'] != row['phase'] or _canonical(state) != row['control_json']:
        raise ValueError('invalid_handoff_control')
    return state


@contextmanager
def _staged_dir(queue, identity, *, create=False):
    with private_root(queue, create=create) as root, _directory(root, STAGED, create) as staged:
        with _directory(staged, identity, create) as fd:
            yield fd


def _check_inode(queue, state):
    meta = state['blobs']['raw']
    phase = state['phase']
    with _staged_dir(queue, state['id']) as fd:
        if phase == 'reserved' and not _exists(fd, 'raw'):
            return  # Reservation precedes the native atomic move.
        info = os.stat('raw', dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_ino != meta['ino'] or info.st_size < meta['length']
                or phase != 'reserved' and stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError('handoff_batch_inode_changed')
        if phase == 'finished':
            node = dict(ino=info.st_ino, size=info.st_size, mtime=info.st_mtime_ns, ctime=info.st_ctime_ns)
            if state.get('sourceNode') != node or state.get('cursor') != node['size']:
                raise OSError('handoff_batch_eof_changed')


def _check_blobs(store, queue, state):
    for name, meta in state['blobs'].items():
        if meta.get('storage') == 'inode':
            _check_inode(queue, state)
            continue
        data = store.work_blob(state['id'], name)
        if data is None or len(data) != meta['length']:
            raise OSError('handoff_length_changed')
        if _sha(data) != meta['sha256']:
            raise OSError('handoff_digest_changed')


class WorkEntries(list):
    """Valid work plus explicit corrupt or unknown entries."""
    def __init__(self):
        super().__init__()
        self.issues = ()


class Work:
    def __init__(self, queue, state, *, closed=False):
        self.queue = Path(queue)
        self._state = state
        self._closed = closed
        # Only an inode-stored batch raw lives on disk; ``folder.name`` is the work id.
        self.folder = self.queue.parent / '.handoff' / 'v1' / STAGED / state['id']
        self.path = self.folder / 'raw'

    @property
    def state(self):
        return deepcopy(self._state)

    @contextmanager
    def _open(self):
        with _staged_dir(self.queue, self._state['id']) as fd:
            yield fd

    @classmethod
    def create(cls, queue, kind, binding, raw, *, image=None):
        if kind not in KINDS or not isinstance(binding, dict) or not _json_value(binding):
            raise ValueError('invalid_handoff_binding')
        payloads = {'raw': raw}
        if image is not None:
            payloads['image'] = image
        for name, data in payloads.items():
            if not isinstance(data, bytes) or len(data) > BLOB_LIMIT[name]:
                raise ValueError('handoff_' + name + '_limit')
        blobs = {name: {'length': len(data), 'sha256': _sha(data)} for name, data in payloads.items()}
        return cls._reserve(queue, kind, binding, blobs, payloads)

    @classmethod
    def _reserve(cls, queue, kind, binding, blobs, payloads):
        from memvane.wiki.pipeline.types import PipelineError, StageQuotaExceeded
        amount = _charge(kind, binding, blobs)
        inode = blobs['raw'].get('storage') == 'inode'
        for name, data in payloads.items():
            _charge_read(name, len(data))
        with _store(queue) as store:
            for _ in range(16):
                identity = uuid.uuid4().hex
                state = dict(version=1, id=identity, kind=kind, queue=Path(queue).name, phase='reserved',
                             binding=deepcopy(binding), blobs=blobs)
                _validate(state, kind, identity)
                try:
                    store.insert_work(
                        identity, state['queue'], kind, 'reserved', _canonical(state),
                        raw_blob=payloads.get('raw'), image_blob=payloads.get('image'),
                        charged_bytes=amount, target_path=_target(binding, payloads.get('raw')),
                    )
                except StageQuotaExceeded:
                    raise OSError(errno.ENOSPC, 'handoff_copy_quota') from None
                except PipelineError as exc:
                    if str(exc) == 'stage_id_collision':
                        continue
                    raise OSError(str(exc)) from exc
                if inode:
                    try:
                        with _staged_dir(queue, identity, create=True):
                            pass
                    except BaseException:
                        store.delete_work(identity, _canonical(state))
                        raise
                return cls(queue, state)
        raise FileExistsError('handoff_id_collision')

    @classmethod
    def reclaim_orphans(cls, queue, kind, *, min_age_seconds=ORPHAN_MIN_AGE_SECONDS):
        """Remove empty inode-batch directories whose work row no longer exists."""
        if kind not in KINDS:
            raise ValueError('invalid_handoff_kind')
        reclaimed = 0
        try:
            with private_root(queue) as root, _directory(root, STAGED) as parent, _reader(queue) as store:
                with os.scandir(parent) as children:
                    names = [child.name for child in children if HEX_ID.fullmatch(child.name)]
                for name in names:
                    if store is not None and store.get_work(name) is not None:
                        continue
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if time.time() - info.st_mtime < min_age_seconds:
                            continue
                        os.rmdir(name, dir_fd=parent)  # Never removes a directory holding a raw.
                        reclaimed += 1
                    except OSError:
                        continue
                if reclaimed:
                    os.fsync(parent)
        except FileNotFoundError:
            return 0
        return reclaimed

    @classmethod
    def entries(cls, queue, kind, *, control_only=False):
        if kind not in KINDS:
            raise ValueError('invalid_handoff_kind')
        entries, issues = WorkEntries(), []
        with _reader(queue) as store:
            if store is None:
                return entries
            for row in store.list_works(Path(queue).name, kind):
                try:
                    state = _decode(row)
                    if not control_only:
                        _check_blobs(store, queue, state)
                    entries.append(cls(queue, state))
                except (OSError, ValueError, RecursionError, TypeError) as exc:
                    issues.append({'name': row['stage_id'], 'reason': str(exc)})
        entries.issues = tuple(issues)
        return entries

    @classmethod
    def lookup(cls, queue, kind, identity):
        if kind not in KINDS or not isinstance(identity, str) or not HEX_ID.fullmatch(identity):
            raise ValueError('invalid_handoff_id')
        with _reader(queue) as store:
            if store is None:
                return None
            row = store.get_work(identity)
            if row is None or row['kind'] != kind:
                return None
            if row['queue'] != Path(queue).name:
                raise ValueError('handoff_queue_mismatch')
            state = _decode(row)
            if not (row['closed'] and state['phase'] == 'finished' and _released(queue, state)):
                _check_blobs(store, queue, state)
            return cls(queue, state, closed=bool(row['closed']))

    def _swap(self, proposed, *, closed=None):
        with _store(self.queue) as store:
            if store.cas_work(self._state['id'], _canonical(self._state), _canonical(proposed),
                              proposed['phase'], closed=closed):
                return
            row = store.get_work(self._state['id'])
            if (row is None or row['control_json'] != _canonical(proposed)
                    or closed is not None and bool(row['closed']) != closed):
                raise OSError('handoff_control_changed')

    def archive(self):
        """Remove a finished work from active scans; its raw and proof remain addressable."""
        if self._state['phase'] != 'finished':
            raise ValueError('handoff_not_finished')
        self._swap(self._state, closed=True)
        self._closed = True
        return self

    def release(self):
        """The caller proves successors own every duty before giving up these bytes."""
        state = self._state
        if state['phase'] != 'finished':
            raise ValueError('handoff_not_finished')
        if not self._closed:
            self.archive()
        with _store(self.queue) as store:
            row = store.get_work(state['id'])
            if row is None:
                return
            if row['control_json'] != _canonical(state):
                raise OSError('handoff_control_changed')
            if state['blobs']['raw'].get('storage') == 'inode':
                self._drop_inode_raw()
            if not store.delete_work(state['id'], _canonical(state)):
                raise OSError('handoff_control_changed')

    def _drop_inode_raw(self):
        state = self._state
        try:
            with private_root(self.queue) as root, _directory(root, STAGED) as parent:
                if not _exists(parent, state['id']):
                    return
                with _directory(parent, state['id']) as fd:
                    if set(os.listdir(fd)) - {'raw'}:
                        raise OSError('unknown_handoff_release_file')
                    if _exists(fd, 'raw'):
                        _check_inode(self.queue, state)
                        os.unlink('raw', dir_fd=fd)
                        os.fsync(fd)
                os.rmdir(state['id'], dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            return

    def read(self, name):
        if name not in self._state['blobs']:
            raise ValueError('unknown_handoff_blob')
        meta = self._state['blobs'][name]
        if meta.get('storage') == 'inode':
            raise ValueError('handoff_batch_requires_window')
        with _store(self.queue, readonly=True) as store:
            data = store.work_blob(self._state['id'], name)
        if data is None or len(data) != meta['length']:
            raise OSError('handoff_length_changed')
        _charge_read(name, len(data))
        if _sha(data) != meta['sha256']:
            raise OSError('handoff_digest_changed')
        return data

    def commit(self, phase, **updates):
        state = self._state
        kind = state['kind']
        if (phase != state['phase'] and phase not in _NEXT[kind][state['phase']] or
                updates.keys() - _UPDATES[kind].get(phase, set()) or state['phase'] == 'finished' and updates):
            raise ValueError('invalid_handoff_transition')
        proposed = dict(state, phase=phase, **deepcopy(updates))
        _validate(proposed, kind, state['id'])
        if _canonical(proposed) != _canonical(state):
            self._swap(proposed)
        self._state = proposed
        return self


def _released(queue, state):
    """A finished inode batch whose raw directory is gone is mid-release."""
    if state['blobs']['raw'].get('storage') != 'inode':
        return False
    try:
        with _staged_dir(queue, state['id']):
            return False
    except FileNotFoundError:
        return True
