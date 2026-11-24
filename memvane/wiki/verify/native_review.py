"""Existing-card replacement jobs run from private work under the shared verify budget."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import uuid

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.wiki.verify.handoff_work import Work, WorkEntries
from memvane.wiki.workspace import api as workspace


def reserve(queue, parent, job, image, node):
    if not isinstance(image, bytes):
        raise ValueError('private_replacement_image_missing')
    raw = canonical_json_bytes(job) + b'\n'
    binding = {'purpose': 'native-review', 'parent': parent.state['id'], 'job': job,
               'targetPath': job['targetPath'], 'route': parent.state['binding']['route']}
    binding['candidate'] = {key: node[key] for key in ('dev', 'ino', 'mtime_ns')}
    work = Work.create(queue, 'hold', binding, raw, image=image)
    if work.read('raw') != raw or work.read('image') != image:
        raise OSError('private_replacement_incomplete')
    return work.state['id']


def _ours(work):
    return work.state['binding'].get('purpose') == 'native-review'


def activate(root, queue, parent):
    if parent.state['binding'].get('audience') != 'native':
        return  # Earlier generations may already have published to the released queue.
    owned = []
    for identity in parent.state['holds']:
        work = Work.lookup(queue, 'hold', identity)
        if work is None or work.state['binding'].get('parent') != parent.state['id']:
            raise OSError('replacement_handoff_missing')
        if _ours(work):
            owned.append(work)
    if len(owned) != 1:
        raise OSError('replacement_handoff_ambiguous')
    work = owned[0]
    if work.state['phase'] == 'reserved':
        work.commit('held', transaction=parent.state['id'], proof={'published': True})

    from memvane.wiki.verify import deferred_review
    deferred_review.activated(queue, parent, work)
    _protect_predecessors(root, queue, work)


def _protect_predecessors(root, queue, successor):
    """Keep independently edited candidates and their original jobs outside the scheduler."""
    from memvane.wiki.workspace.handoff.api import evidence as regular
    from memvane.store.types import MAX_BODY

    right = successor.state['binding']['job']
    if not right.get('sourceRevisions'):
        return
    for work in waiting(queue):
        state = work.state
        left = state['binding']['job']
        if left['targetPath'] != right['targetPath'] or _alive(state['proof'].get('owner')):
            continue
        if state['binding'].get('action') == 'create':
            from memvane.wiki.verify.pending_replacement import job_revisions
            def revisions(job):
                return {(r['collection'], r['path']): r['sha256'] for r in job_revisions(job)}
            before, after = revisions(left), revisions(right)
            if (before and before.keys() == after.keys() and before != after
                    and hashlib.sha256(work.read('image')).hexdigest() == right['expectedTargetHash']):
                work.commit('finished', proof=dict(state['proof'], superseded=successor.state['id'])).archive()
            continue
        if (not left.get('pendingPath') or left['pendingPath'] == right['pendingPath']
                or left.get('sourceRevisions') != right['sourceRevisions']):
            continue
        try:
            edited, _, _ = regular(root, left['pendingPath'], MAX_BODY)
        except (OSError, ValueError):
            continue
        if edited == work.read('image'):
            continue
        from memvane.wiki.verify.pending_replacement import pending_matches_job
        if pending_matches_job(edited.decode('utf8', 'replace'), left) and b'status: verified' in edited:
            continue
        manual = Work.create(queue, 'hold', {'purpose': 'manual-edit', 'original': state['id'],
                             'successor': successor.state['id'], 'path': left['pendingPath']},
                             work.read('raw'), image=edited)
        manual.commit('manual', transaction=state['id'], proof={'retained': True})
        work.commit('finished', proof={'manual': manual.state['id'], 'successor': successor.state['id']}).archive()


def cancel(queue, parent):
    entries = Work.entries(queue, 'hold', control_only=True)
    for work in entries:
        state = work.state
        if _ours(work) and state['binding'].get('parent') == parent.state['id'] and state['phase'] == 'reserved':
            work.commit('manual', proof={'cancelled': True}).commit('finished').archive()


def _same_job(work, job):
    from memvane.wiki.workspace.handoff.api import occurrence
    parsed, _ = occurrence(work.read('raw'))
    return parsed is not None and canonical_json_bytes(parsed) == canonical_json_bytes(job)


def waiting(queue):
    result = WorkEntries()
    all_work = Work.entries(queue, 'hold', control_only=True)
    issues = list(all_work.issues)
    for work in all_work:
        state = work.state
        if not _ours(work):
            continue
        if state['binding'].get('action') == 'create' and not state.get('proof', {}).get('published'):
            issues.append({'name': work.folder.name, 'reason': 'first_card_unpublished'})
            continue
        if state['phase'] != 'held':
            continue
        try:
            if (state.get('proof', {}).get('published') is not True or
                    state.get('transaction') != state['binding'].get('parent', state['id']) or
                    not isinstance(state['binding'].get('job'), dict)):
                raise ValueError('invalid_private_review')
            result.append(work)
        except (KeyError, TypeError, OSError, ValueError) as error:
            issues.append({'name': work.folder.name, 'reason': str(error)})
    result.issues = tuple(issues)
    return result


def references(queue, *, exclude_parent=None):
    all_work = Work.entries(queue, 'hold', control_only=True)
    paths = set()
    uncertain = bool(all_work.issues)
    for work in all_work:
        state = work.state
        if (_ours(work) and state['phase'] != 'finished' and
                not (exclude_parent and state['binding'].get('parent') == exclude_parent and state['phase'] == 'reserved')):
            job = state['binding'].get('job')
            if not isinstance(job, dict) or not isinstance(job.get('targetPath'), str):
                uncertain = True
                continue
            path = job.get('pendingPath')
            if isinstance(path, str):
                paths.add(path)
    return paths, uncertain


@contextmanager
def _locked(queue, *, blocking=True):
    with workspace.queue_lock_path(queue).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def _alive(owner):
    pid = owner.get('pid') if isinstance(owner, dict) else None
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process(queue, selected, work, callback):
    """Only the owner inside the existing queue lock schedules this occurrence; the engine runs outside it."""
    identity = work.state['id']
    owner = {'pid': os.getpid(), 'attempt': uuid.uuid4().hex}
    with _locked(queue):
        work = Work.lookup(queue, 'hold', identity)
        from memvane.wiki.verify import handoff_work as store
        rotation = 'review-turn-' + hashlib.sha256(queue.name.encode()).hexdigest()[:24]
        with store.private_root(queue) as fd:
            store._publish(fd, rotation, identity.encode('ascii'), replace=store._exists(fd, rotation))
        if work is None or work.state['phase'] != 'held':
            return False, False
        state = work.state
        proof = state.get('proof', {})
        route = state['binding'].get('route')
        if (not proof.get('published') or _alive(proof.get('owner')) or not _same_job(work, state['binding']['job']) or
                route != (workspace.route_identity(selected) if selected is not None else None)):
            return False, True
        others = Work.entries(queue, 'hold', control_only=True)
        if any(w.state['binding'].get('targetPath') == state['binding']['targetPath']
               and _alive(w.state.get('proof', {}).get('owner')) for w in others):
            return False, True
        previous = {k: v for k, v in proof.items() if k != 'owner'}
        work.commit('held', proof=dict(previous, owner=owner))
    keep = True
    try:
        outcome, keep = callback(work)
        return outcome, keep
    finally:
        with _locked(queue):
            if keep:
                work.commit('held', proof=previous)
            else:
                work.commit('finished', proof=dict(previous, completed=True)).archive()


def scheduled(queue, works):
    """Cycle independent targets across wakes; one active attempt per target and at most sixteen per pass."""
    from memvane.wiki.verify import handoff_work as store
    works = sorted(works, key=lambda w: (w.state['binding']['job'].get('ts', ''), w.folder.name), reverse=True)
    if not works:
        return []
    name = 'review-turn-' + hashlib.sha256(queue.name.encode()).hexdigest()[:24]
    with store.private_root(queue) as fd:
        try:
            previous = store._file(fd, name, 32).decode('ascii')
        except FileNotFoundError:
            previous = None
        order = [work.state['id'] for work in works]
        start = order.index(previous) + 1 if previous in order else 0
        chosen = (works[start:] + works[:start])[:16]
    return chosen


def busy(queue):
    entries = Work.entries(queue, 'hold', control_only=True)
    return bool(entries.issues or any((_ours(w) or w.state['binding'].get('purpose') == 'deferred-replacement')
                                      and w.state['phase'] != 'finished' for w in entries))
