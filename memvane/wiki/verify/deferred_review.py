"""Hold the original candidate while an unobserved released claim could name its pending path."""
from __future__ import annotations

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.wiki.verify.handoff_work import HOLD_SCAN_LIMIT, Work
from memvane.wiki.workspace import api as workspace


PURPOSE = 'deferred-replacement'


def reserve(queue, job, image, bound):
    if image is None:
        return False
    route = workspace.route_identity(bound)
    raw = canonical_json_bytes(job) + b'\n'
    binding = dict(purpose=PURPOSE, route=route, job=job)
    for work in Work.entries(queue, 'hold', control_only=True, max_entries=HOLD_SCAN_LIMIT):
        previous = work.state
        if (previous['binding'] == binding and previous['phase'] in ('reserved', 'held')
                and work.read('raw') == raw and work.read('image') == image):
            return True
    work = Work.create(queue, 'hold', binding, raw, image=image)
    work.commit('held', transaction=work.state['id'], proof={'awaitingReferences': True})
    return True


def active(queue):
    return [w for w in Work.entries(queue, 'hold', control_only=True, max_entries=HOLD_SCAN_LIMIT) if w.state['binding'].get('purpose') == PURPOSE
            and w.state['phase'] in ('reserved', 'held')]


def resume(root, config, wiki, queue, bound):
    from memvane.wiki.verify import candidate_stage, pending_handoff
    for work in active(queue)[:16]:
        binding = work.state['binding']
        if binding['route'] != workspace.route_identity(bound):
            continue
        job = binding['job']
        if work.read('raw') != canonical_json_bytes(job) + b'\n':
            continue
        image = work.read('image')
        path = root / job['pendingPath']
        if work.state['phase'] == 'reserved':
            work.commit('held', transaction=work.state['id'], proof={'awaitingReferences': True})
        pending_handoff.enqueue_locked(root, config, wiki, queue, job, bound, lambda: candidate_stage.write(path, image),
                                       produced_bytes=image, deferred_id=work.state['id'])


def activated(queue, parent, child):
    identity = parent.state['binding'].get('deferred')
    if identity:
        work = Work.lookup(queue, 'hold', identity)
        if not work or work.state['binding'].get('job') != child.state['binding']['job']:
            raise OSError('deferred replacement differs from publication')
        if work.state['phase'] == 'held':
            work.commit('finished', proof={'successor': child.state['id']}).archive()
