"""Move a queued inode without reading it; checkpoint bounded, evidenced ranges under the queue lock."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import base64
import hashlib
import os
import re
from pathlib import Path
import stat

from memvane.wiki.verify import handoff_work as store

SLAB = store.SLAB
_EMPTY = hashlib.sha256(b'').hexdigest()


def _node(info):
    return dict(ino=info.st_ino, size=info.st_size, mtime=info.st_mtime_ns, ctime=info.st_ctime_ns)


def _safe(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise OSError('unsafe_batch_inode')


def _available(parent, queue, origin, orphan_dead, *, native=False):
    if native:
        if origin == queue.name or re.fullmatch(r'ready-[0-9a-f]{32}', origin):
            return
        if (re.fullmatch(r'native\.claimed\.\d+\.[a-f0-9]{32}', origin) and orphan_dead is not None
                and orphan_dead(queue.with_name(origin))):
            return
        raise BlockingIOError('private_batch_owner_unconfirmed')
    if origin != queue.name:
        if not origin.startswith(queue.name + '.claimed.') or not orphan_dead or not orphan_dead(queue.with_name(origin)):
            raise BlockingIOError('batch_orphan_not_proven_dead')
    with os.scandir(parent) as contents:
        for entry in contents:
            if entry.name.startswith(queue.name + '.claimed.') and entry.name != origin:
                raise BlockingIOError('batch_has_unobserved_claim')


def _empty_live(parent, name):
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    except FileExistsError:
        _safe(os.stat(name, dir_fd=parent, follow_symlinks=False))
    else:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.fsync(parent)


def adopt(queue, source=None, *, binding=None, orphan_dead=None):
    """Caller holds the live queue lock and the legacy writer's card lock where applicable."""
    queue = Path(queue)
    source = queue if source is None else Path(source)
    from memvane.wiki.workspace.handoff.api import native_live
    private = source.parent != queue.parent
    if ((private and (native_live(queue) is None or source.parent != native_live(queue).parent)) or
            binding is not None and not isinstance(binding, dict)):
        raise ValueError('invalid_batch_origin')
    with store._opened(source.parent, store.DIR) as parent:
        _available(parent, queue, source.name, orphan_dead, native=private)
        info = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        _safe(info)
    prior = store.Work.entries(queue, 'hold', control_only=True)
    if prior.issues:
        raise BlockingIOError('uncertain_batch_reservations')
    for work in prior:
        state = work.state
        if (state['blobs']['raw'].get('storage') == 'inode' and state['blobs']['raw']['ino'] == info.st_ino):
            batch = Batch(work)
            batch.resume(orphan_dead=orphan_dead)
            return batch
    value = dict(binding or {})
    value['batch'] = {'origin': source.name, 'sourceNode': _node(info)}
    if private:
        value['batch']['native'] = True
    blobs = {'raw': {'storage': 'inode', 'length': info.st_size, 'ino': info.st_ino}}
    batch = Batch(store.Work._reserve(queue, 'hold', value, blobs, {}))
    batch.resume(orphan_dead=orphan_dead)
    return batch


def active(queue, *, orphan_dead=None):
    """Resume durable reservations under the existing queue lock; expose all unknown entries."""
    work = store.Work.entries(queue, 'hold', control_only=True)
    batches, issues = store.WorkEntries(), list(work.issues)
    for item in work:
        state = item.state
        if state['blobs']['raw'].get('storage') != 'inode':
            continue
        try:
            batch = Batch(item)
            if state['phase'] == 'reserved':
                batch.resume(orphan_dead=orphan_dead)
            if item.state['phase'] == 'held':
                batches.append(batch)
            elif item.state['phase'] == 'finished':
                item.archive()
            else:
                issues.append({'name': item.folder.name, 'reason': 'batch_requires_reassessment'})
        except (OSError, ValueError) as exc:
            issues.append({'name': item.folder.name, 'reason': str(exc)})
    batches.issues = tuple(issues)
    return batches


@dataclass(frozen=True)
class Window:
    offset: int
    end: int
    data: bytes
    eof: bool
    snapshot: dict
    digest: str
    work_id: str


def _piece(fd, start, length, lane):
    if not 0 <= length <= SLAB:
        raise ValueError('invalid_batch_window')
    from memvane.wiki.workspace.handoff.api import charge
    charge(lane, length)
    result = os.pread(fd, length, start)
    if len(result) != length:
        raise OSError('batch_short_read')
    return result


def _extend(chain, block):
    return store._sha(bytes.fromhex(chain) + hashlib.sha256(block).digest())


def _prefix(fd, old, start, data, lane):
    """64KiB block chain plus digest of a partial final block. At most one prior slab is reread."""
    tail_length = start % SLAB
    tail = _piece(fd, start - tail_length, tail_length, lane) if tail_length else b''
    if store._sha(tail) != old['tail']:
        raise OSError('batch_acknowledged_prefix_changed')
    material = tail + data
    chain = old['chain']
    if len(material) >= SLAB:
        chain = _extend(chain, material[:SLAB])
        material = material[SLAB:]
    return {'chain': chain, 'tail': store._sha(material)}


class Batch:
    def __init__(self, work):
        self.work = work
        self.lane = 'source' if work.queue.name.startswith('source-queue') else 'verify'

    @property
    def cursor(self):
        return self.work.state.get('cursor', 0)

    def resume(self, *, orphan_dead=None):
        state, queue = self.work.state, self.work.queue
        if state['phase'] != 'reserved':
            return
        saved = state['binding']['batch']
        origin = saved['origin']
        if not isinstance(origin, str) or Path(origin).name != origin:
            raise ValueError('invalid_batch_origin')
        directory = queue.parent
        if saved.get('native') is True:
            from memvane.wiki.workspace.handoff.api import native_live
            path = native_live(queue)
            if path is None:
                raise OSError('native_batch_origin_missing')
            directory = path.parent
        with self.work._open() as private, store._opened(directory, store.DIR) as parent:
            if not store._exists(private, 'raw'):
                _available(parent, queue, origin, orphan_dead, native=saved.get('native') is True)
                with store._opened(origin, store.READ, parent=parent) as fd:
                    info = os.fstat(fd)
                    _safe(info)
                    if (_node(info) != saved['sourceNode'] or
                            not os.path.samestat(info, os.stat(origin, dir_fd=parent, follow_symlinks=False))):
                        raise OSError('batch_origin_changed')
                    store.rename_noreplace(parent, origin, 'raw', target_parent_fd=private)
            with store._opened('raw', store.READ, parent=private) as fd:
                info = os.fstat(fd)
                _safe(info)
                if (info.st_ino != saved['sourceNode']['ino'] or info.st_size < saved['sourceNode']['size']):
                    raise OSError('batch_move_unproven')
                if store._exists(parent, origin) and os.path.samestat(
                        info, os.stat(origin, dir_fd=parent, follow_symlinks=False)):
                    raise OSError('conflicting_batch_names')
                if stat.S_IMODE(info.st_mode) != 0o600:
                    os.fchmod(fd, 0o600)
                os.fsync(fd)
                node = _node(os.fstat(fd))
            os.fsync(private)
            os.fsync(parent)
            _empty_live(parent, queue.name)
            self.work.commit('held', cursor=0, sourceNode=node, proof={'prefix': {'chain': _EMPTY, 'tail': _EMPTY}})

    @contextmanager
    def _input(self):
        with self.work._open() as folder, store._opened('raw', store.READ, parent=folder) as fd:
            info = os.fstat(fd)
            _safe(info)
            if (info.st_ino != self.work.state['blobs']['raw']['ino'] or
                    stat.S_IMODE(info.st_mode) != 0o600):
                raise OSError('invalid_batch_input')
            yield fd, _node(info)

    def _audit(self, fd, node, state, limit):
        proof = dict(state['proof'])
        audit = proof.get('audit', {})
        if audit.get('node') != node:
            audit = dict(node=node, offset=0, chain=_EMPTY)
        start = audit['offset']
        size = min(limit, state['cursor'] - start)
        if state['cursor'] > node['size']:
            raise OSError('batch_acknowledged_prefix_missing')
        block = _piece(fd, start, size, self.lane)
        if _node(os.fstat(fd)) != node:
            raise BlockingIOError('batch_changed_during_audit')
        pending = base64.b64decode(audit.get('partial', ''), validate=True) + block
        if len(pending) >= SLAB:
            audit['chain'] = _extend(audit['chain'], pending[:SLAB])
            pending = pending[SLAB:]
        audit['partial'] = base64.b64encode(pending).decode('ascii')
        audit['offset'] += size
        if audit['offset'] < state['cursor']:
            proof['audit'] = audit
            self.work.commit('held', proof=proof)
            return
        tail = store._sha(pending)
        if proof['prefix'] != {'chain': audit['chain'], 'tail': tail}:
            proof['conflict'] = {'node': node, 'offset': state['cursor']}
            self.work.commit('manual', proof=proof)
            raise OSError('batch_acknowledged_prefix_changed')
        proof.pop('audit', None)
        proof.pop('parked', None)
        self.work.commit('held', sourceNode=node, proof=proof)

    def read_window(self, max_bytes=SLAB, *, gate=nullcontext):
        state = self.work.state
        if state['phase'] != 'held' or type(max_bytes) is not int or not 0 < max_bytes <= SLAB:
            raise ValueError('invalid_batch_window')
        with self._input() as (fd, node):
            if state['sourceNode'] != node:
                with gate():
                    self._audit(fd, node, state, max_bytes)
                return None
            offset = state['cursor']
            data = _piece(fd, offset, min(max_bytes, node['size'] - offset), self.lane)
            if _node(os.fstat(fd)) != node:
                raise BlockingIOError('batch_changed_during_read')
        end = offset + len(data)
        return Window(offset, end, data, end == node['size'], node, store._sha(data), state['id'])

    def commit_window(self, window, proof, *, end=None):
        state = self.work.state
        end = window.end if end is None else end
        if (not isinstance(proof, dict) or not proof or state['phase'] != 'held' or
                window.work_id != state['id'] or window.offset != state['cursor'] or
                type(end) is not int or not window.offset < end <= window.end or
                window.end != window.offset + len(window.data) or store._sha(window.data) != window.digest):
            raise ValueError('batch_range_has_no_proof')
        with self._input() as (fd, node):
            if node != window.snapshot or node != state['sourceNode']:
                raise BlockingIOError('batch_generation_changed')
            data = _piece(fd, window.offset, end - window.offset, self.lane)
            if data != window.data[:len(data)]:
                raise OSError('batch_window_changed')
            prefix = _prefix(fd, state['proof']['prefix'], window.offset, data, self.lane)
            if _node(os.fstat(fd)) != node:
                raise BlockingIOError('batch_changed_during_commit')
            self.work.commit('held', cursor=end, proof=dict(state['proof'], prefix=prefix, handoff=proof))

    def finish(self, proof):
        state = self.work.state
        if not isinstance(proof, dict) or not proof or state['phase'] != 'held':
            raise ValueError('batch_completion_has_no_proof')
        with self._input() as (fd, node):
            if node != state['sourceNode'] or state['cursor'] != node['size']:
                raise BlockingIOError('batch_eof_unproven')
            self.work.commit('finished', proof={**state['proof'], 'completion': proof})
        self.work.archive()
