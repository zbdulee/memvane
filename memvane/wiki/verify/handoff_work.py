"""Versioned, owner-private verify handoffs. Mutators share the caller's existing queue/card lock."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from memvane.kernel.atomic_rename import canonical_json_bytes, rename_noreplace

SLAB = 65536
CONTROL_LIMIT = 1024 ** 2
BLOB_LIMIT = {'raw': 8 * 1024 ** 2, 'image': 4 * 1024 ** 2}
KINDS = ('publication', 'legacy', 'hold')
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
HEX_ID = re.compile(r'[a-f0-9]{32}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')

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


def _file(parent, name, limit, *, keep=True, expected=None, budget=None):
    with _opened(name, READ, parent=parent) as fd:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit):
            raise OSError('unsafe_handoff_file')
        if expected is not None and info.st_size != expected['length']:
            raise OSError('handoff_length_changed')
        if budget is not None:
            if budget[0] < info.st_size:
                raise _ScanLimit
            budget[0] -= info.st_size
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


def _publish(parent, name, data, *, replace=False):
    temp = '.w-' + uuid.uuid4().hex
    with _opened(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, parent=parent) as fd:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('handoff_short_write')
            view = view[written:]
        os.fsync(fd)
    _file(parent, temp, len(data), keep=False, expected={'length': len(data), 'sha256': _sha(data)})
    if replace:
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
    else:
        rename_noreplace(parent, temp, name)
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


def _control(fd, kind, identity, budget=None):
    record = json.loads(_file(fd, 'work', CONTROL_LIMIT, budget=budget), object_pairs_hook=_unique)
    if not isinstance(record, dict):
        raise ValueError('invalid_handoff_control')
    checksum = record.pop('checksum', None)
    if checksum != _sha(canonical_json_bytes(record)):
        raise ValueError('invalid_handoff_checksum')
    _validate(record, kind, identity)
    return record


def _save(fd, state, *, replace=False):
    _validate(state, state['kind'], state['id'])
    data = canonical_json_bytes(dict(state, checksum=_sha(canonical_json_bytes(state))))
    if len(data) > CONTROL_LIMIT:
        raise ValueError('handoff_control_limit')
    _publish(fd, 'work', data, replace=replace)


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


@contextmanager
def _collection(root, kind, closed, create=False):
    if closed:
        with _directory(root, 'closed', create) as archived, _directory(archived, kind, create) as parent:
            yield parent
    else:
        with _directory(root, kind, create) as parent:
            yield parent


def _check_blob(fd, name, meta, state, *, budget=None):
    phase = state['phase']
    if meta.get('storage') != 'inode':
        _file(fd, name, BLOB_LIMIT[name], keep=False, expected=meta, budget=budget)
        return
    if phase == 'reserved' and not _exists(fd, name):
        return  # Reservation precedes the native atomic move.
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_ino != meta['ino'] or info.st_size < meta['length']
            or phase != 'reserved' and stat.S_IMODE(info.st_mode) != 0o600):
        raise OSError('handoff_batch_inode_changed')
    if phase == 'finished':
        node = dict(ino=info.st_ino, size=info.st_size, mtime=info.st_mtime_ns, ctime=info.st_ctime_ns)
        if state.get('sourceNode') != node or state.get('cursor') != node['size']:
            raise OSError('handoff_batch_eof_changed')


def _retained(fd, kind, identity, budget=None):
    state = _control(fd, kind, identity, budget)
    for blob, meta in state['blobs'].items():
        _check_blob(fd, blob, meta, state, budget=budget)
    return state


def _permit(parent, identity, kind):
    data = json.loads(_file(parent, '.release-' + identity, CONTROL_LIMIT + 1024), object_pairs_hook=_unique)
    if not isinstance(data, dict) or set(data) != {'ino', 'state', 'checksum'}:
        raise ValueError('invalid_handoff_release')
    checksum = data.pop('checksum')
    if checksum != _sha(canonical_json_bytes(data)) or type(data['ino']) is not int:
        raise ValueError('invalid_handoff_release')
    _validate(data['state'], kind, identity)
    if data['state']['phase'] != 'finished':
        raise ValueError('invalid_handoff_release')
    return data


class _ScanLimit(Exception):
    pass


class WorkEntries(list):
    """Valid work plus explicit incomplete, corrupt or unknown entries."""
    def __init__(self):
        super().__init__()
        self.issues = ()


class Work:
    def __init__(self, queue, state, *, closed=False):
        self.queue = Path(queue)
        self._state = state
        self._closed = closed
        base = self.queue.parent / '.handoff' / 'v1'
        self.folder = (base / 'closed' if closed else base) / state['kind'] / state['id']
        self.path = self.folder / 'work'

    @property
    def state(self):
        return deepcopy(self._state)

    @contextmanager
    def _open(self):
        with private_root(self.queue) as root, _collection(root, self._state['kind'], self._closed) as parent:
            with _directory(parent, self._state['id']) as fd:
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
        with private_root(queue, create=True) as root, _directory(root, kind, True) as parent, ExitStack() as stack:
            try:
                closed = stack.enter_context(_collection(root, kind, True))
            except FileNotFoundError:
                closed = None
            for _ in range(16):
                identity = uuid.uuid4().hex
                if closed is not None and (_exists(closed, identity) or _exists(closed, '.release-' + identity)):
                    continue
                try:
                    fd = stack.enter_context(_directory(parent, identity, True, exclusive=True))
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError('handoff_id_collision')
            state = dict(version=1, id=identity, kind=kind, queue=Path(queue).name, phase='reserved',
                         binding=deepcopy(binding), blobs=blobs)
            _validate(state, kind, identity)
            for name, data in payloads.items():
                _publish(fd, name, data)
            _save(fd, state)
        return cls(queue, state)

    @classmethod
    def entries(cls, queue, kind, *, max_bytes=8 * 1024 ** 2, max_entries=64):
        if kind not in KINDS:
            raise ValueError('invalid_handoff_kind')
        entries, issues, budget = WorkEntries(), [], [max_bytes]
        with ExitStack() as stack:
            try:
                hidden = stack.enter_context(_hidden(queue))
                with os.scandir(hidden) as generations:
                    for index, entry in enumerate(generations):
                        if index >= 64:
                            issues.append({'name': entry.name, 'reason': 'handoff_scan_limit'})
                            break
                        if entry.name != 'v1':
                            issues.append({'name': entry.name, 'reason': 'unknown_handoff_generation'})
                root = stack.enter_context(_directory(hidden, 'v1'))
                parent = stack.enter_context(_directory(root, kind))
            except FileNotFoundError:
                entries.issues = tuple(issues)
                return entries
            with os.scandir(parent) as children:
                for count, child in enumerate(children):
                    name = child.name
                    if count >= max_entries:
                        issues.append({'name': name, 'reason': 'handoff_scan_limit'})
                        break
                    if not HEX_ID.fullmatch(name):
                        issues.append({'name': name, 'reason': 'unknown_handoff_entry'})
                        continue
                    try:
                        with _directory(parent, name) as fd:
                            state = _control(fd, kind, name, budget)
                            if state['queue'] != Path(queue).name:
                                continue
                            for blob, meta in state['blobs'].items():
                                _check_blob(fd, blob, meta, state, budget=budget)
                        entries.append(cls(queue, state))
                    except _ScanLimit:
                        issues.append({'name': name, 'reason': 'handoff_scan_limit'})
                        break
                    except (OSError, ValueError, RecursionError, TypeError) as exc:
                        issues.append({'name': name, 'reason': str(exc)})
        entries.issues = tuple(issues)
        return entries

    @classmethod
    def lookup(cls, queue, kind, identity):
        if kind not in KINDS or not isinstance(identity, str) or not HEX_ID.fullmatch(identity):
            raise ValueError('invalid_handoff_id')
        found = None
        with ExitStack() as stack:
            try:
                root = stack.enter_context(private_root(queue))
            except FileNotFoundError:
                return None
            for closed in (False, True):
                try:
                    parent = stack.enter_context(_collection(root, kind, closed))
                except FileNotFoundError:
                    continue
                releasing = closed and _exists(parent, '.release-' + identity)
                if not releasing and not _exists(parent, identity):
                    continue
                if found is not None:
                    raise OSError('conflicting_handoff_locations')
                if releasing:
                    state = _permit(parent, identity, kind)['state']
                else:
                    with _directory(parent, identity) as fd:
                        state = _retained(fd, kind, identity)
                if state['queue'] != Path(queue).name:
                    raise ValueError('handoff_queue_mismatch')
                found = cls(queue, state, closed=closed)
        return found

    def archive(self):
        """Remove a finished work from active scans; its raw and proof remain addressable."""
        state = self._state
        if state['phase'] != 'finished':
            raise ValueError('handoff_not_finished')
        kind, identity = state['kind'], state['id']
        with private_root(self.queue) as root, _collection(root, kind, False) as source:
            with _collection(root, kind, True, True) as target:
                if _exists(source, identity):
                    with _directory(source, identity) as fd:
                        if canonical_json_bytes(_retained(fd, kind, identity)) != canonical_json_bytes(state):
                            raise OSError('handoff_control_changed')
                    rename_noreplace(source, identity, identity, target_parent_fd=target)
                os.fsync(target)
                os.fsync(source)
                with _directory(target, identity) as fd:
                    if canonical_json_bytes(_retained(fd, kind, identity)) != canonical_json_bytes(state):
                        raise OSError('handoff_archive_mismatch')
        self._closed = True
        self.folder = self.queue.parent / '.handoff' / 'v1' / 'closed' / kind / identity
        self.path = self.folder / 'work'
        return self

    def release(self):
        """The caller proves successors own every duty before giving up these bytes."""
        state, kind, identity = self._state, self._state['kind'], self._state['id']
        if state['phase'] != 'finished':
            raise ValueError('handoff_not_finished')
        if not self._closed:
            self.archive()
        token = '.release-' + identity
        with private_root(self.queue) as root, _collection(root, kind, True) as parent:
            if not _exists(parent, token):
                if not _exists(parent, identity):
                    return
                with _directory(parent, identity) as fd:
                    if canonical_json_bytes(_retained(fd, kind, identity)) != canonical_json_bytes(state):
                        raise OSError('handoff_control_changed')
                    permit = {'ino': os.fstat(fd).st_ino, 'state': state}
                _publish(parent, token, canonical_json_bytes(dict(permit, checksum=_sha(canonical_json_bytes(permit)))))
            permit = _permit(parent, identity, kind)
            if canonical_json_bytes(permit['state']) != canonical_json_bytes(state):
                raise OSError('handoff_release_mismatch')
            if _exists(parent, identity):
                with _directory(parent, identity) as fd:
                    if os.fstat(fd).st_ino != permit['ino']:
                        raise OSError('handoff_release_inode_changed')
                    if set(os.listdir(fd)) - {'work', *state['blobs']}:
                        raise OSError('unknown_handoff_release_file')
                    if _exists(fd, 'work') and canonical_json_bytes(_control(fd, kind, identity)) != canonical_json_bytes(state):
                        raise OSError('handoff_control_changed')
                    for name, meta in state['blobs'].items():
                        if _exists(fd, name):
                            _check_blob(fd, name, meta, state)
                            os.unlink(name, dir_fd=fd)
                    os.fsync(fd)
                    if _exists(fd, 'work'):
                        os.unlink('work', dir_fd=fd)
                    os.fsync(fd)
                os.rmdir(identity, dir_fd=parent)
            os.fsync(parent)
            os.unlink(token, dir_fd=parent)
            os.fsync(parent)

    def read(self, name):
        if name not in self._state['blobs']:
            raise ValueError('unknown_handoff_blob')
        if self._state['blobs'][name].get('storage') == 'inode':
            raise ValueError('handoff_batch_requires_window')
        with self._open() as fd:
            return _file(fd, name, BLOB_LIMIT[name], expected=self._state['blobs'][name])

    def commit(self, phase, **updates):
        state = self._state
        kind, identity = state['kind'], state['id']
        if (phase != state['phase'] and phase not in _NEXT[kind][state['phase']] or
                updates.keys() - _UPDATES[kind].get(phase, set()) or state['phase'] == 'finished' and updates):
            raise ValueError('invalid_handoff_transition')
        proposed = dict(state, phase=phase, **deepcopy(updates))
        with self._open() as fd:
            current = _control(fd, kind, identity)
            unchanged = canonical_json_bytes(current) == canonical_json_bytes(state)
            already_committed = canonical_json_bytes(current) == canonical_json_bytes(proposed)
            if not unchanged and not already_committed:
                raise OSError('handoff_control_changed')
            if not already_committed:
                _save(fd, proposed, replace=True)
            else:
                os.fsync(fd)  # Reconfirm a rename that preceded a failed directory sync.
        self._state = proposed
        return self
