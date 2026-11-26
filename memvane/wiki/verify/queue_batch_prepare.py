"""Seal an oversized verify input before an ordinary consumer can claim it."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from itertools import islice
import os
import stat

from memvane.wiki.verify import batch_consumer, batch_handoff
from memvane.wiki.verify.handoff_work import HEX_ID, Work
from memvane.wiki.verify.pending_replacement import _VERIFY_QUEUE_BYTES
from memvane.wiki.workspace import api as workspace


def _dead(queue, claim):
    prefix = 'native.claimed.' if claim.name.startswith('native.claimed.') else queue.name + '.claimed.'
    suffix = claim.name.removeprefix(prefix)
    pid, separator, nonce = suffix.partition('.')
    if not separator or not nonce or '.' in nonce or not pid.isascii() or not pid.isdigit():
        return False
    try:
        owner = int(pid)
        if owner <= 0 or owner == os.getpid():
            return False
        os.kill(owner, 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError, OverflowError):
        pass
    return False


def unresolved(queue, holds=None):
    if holds is None:
        holds = Work.entries(queue, 'hold', control_only=True)
    # An unreadable work row is uncertain; there is no scan limit to exhaust.
    uncertain = any(HEX_ID.fullmatch(issue['name']) for issue in holds.issues)
    return uncertain or any(not batch_consumer.parked(batch_handoff.Batch(item)) for item in holds
                            if item.state['blobs']['raw'].get('storage') == 'inode')


def opaque_ranges(queue, holds=None):
    holds = Work.entries(queue, 'hold', control_only=True) if holds is None else holds
    return any(item.state['blobs']['raw'].get('storage') == 'inode' and
               item.state.get('proof', {}).get('opaque') for item in holds)


def _source(queue):
    """Only stat inputs here. Unobserved or overlapping claims preserve every public name."""
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        raise BlockingIOError('verify_batch_claim_limit')
    try:
        node = queue.lstat()
        size = node.st_size
    except FileNotFoundError:
        size = 0
    # A large single snapshot cannot also pay for claim validation and return in this pass.
    if size > _VERIFY_QUEUE_BYTES // 4 and not claims:
        return queue
    if 0 < size <= _VERIFY_QUEUE_BYTES:
        from memvane.wiki.workspace.handoff.api import bounded_rows, budget
        ledger = budget.current()
        stamp = ('queue-size', node.st_dev, node.st_ino, node.st_size, node.st_ctime_ns, node.st_mtime_ns)
        observed = ledger is not None and stamp in ledger.snapshots
        try:
            if not observed:
                for _ in bounded_rows(queue):
                    pass
                if ledger is not None:
                    ledger.snapshots[stamp] = True
        except OSError as error:
            if str(error) not in ('queue_occurrence_limit', 'handoff_source_rows_pass_limit', 'handoff_verify_rows_pass_limit'):
                raise
            size = _VERIFY_QUEUE_BYTES + 1
    if size > _VERIFY_QUEUE_BYTES:
        if claims:
            raise BlockingIOError('verify_batch_has_claim')
        return queue
    for claim in claims:
        claim_size = claim.lstat().st_size
        if claim_size > _VERIFY_QUEUE_BYTES and size == 0 and len(claims) == 1 and _dead(queue, claim):
            return claim
        size += claim_size
        if size > _VERIFY_QUEUE_BYTES:
            raise BlockingIOError('verify_batch_overlapping_inputs')
    return None


def delivery(queue):
    from memvane.wiki.workspace.handoff.api import delivery_pending
    if next(queue.parent.glob(queue.name + '.claimed.*'), None) is not None:
        return None
    for work in Work.entries(queue, 'hold', control_only=True):
        state = work.state
        if state['blobs']['raw'].get('storage') != 'inode' or state['phase'] != 'held':
            continue
        proof = state.get('proof', {})
        name = proof.get('lastDelivery')
        if not name or not delivery_pending(queue, name):
            continue
        batch = batch_handoff.Batch(work)
        with batch._input() as (_, node):
            if state['sourceNode'] == node and state['cursor'] == node['size']:
                return name
    return None


class BatchPending(BlockingIOError):
    pass


def _ready_locked(queue, bound):
    from memvane.wiki.workspace.handoff.api import public_name
    public = public_name(queue)
    source = _source(queue)
    if public != queue:
        if source is None:
            return True
        binding = {'purpose': 'queue-batch', 'route': workspace.route_identity(bound)}
        batch_handoff.adopt(public, source, binding=binding, orphan_dead=lambda path: _dead(public, path))
        return False
    if source is None and not unresolved(queue):
        return True

    def dead(claim):
        return _dead(queue, claim)

    active = batch_handoff.active(queue, orphan_dead=dead)
    if active.issues:
        return False
    active = [batch for batch in active if not batch_consumer.parked(batch)]
    if active:
        return False  # The outer prepare pass advances it without holding the queue lock.
    if source is not None:
        binding = {'purpose': 'queue-batch', 'route': workspace.route_identity(bound)}
        batch_handoff.adopt(queue, source, binding=binding, orphan_dead=dead)
        return False
    return True


def guard_claim(queue, bound):
    """Called with the card and queue locks, immediately before ordinary reclaim/claim."""
    try:
        ready = _ready_locked(queue, bound)
    except (OSError, ValueError, RuntimeError) as exc:
        raise BatchPending('verify_batch_requires_reassessment') from exc
    if not ready:
        raise BatchPending('verify_batch_retained')


@contextmanager
def _locked(root, queue):
    with workspace.card_write_lock(root, blocking=False):
        name = workspace.queue_lock_path(queue)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            node = os.fstat(fd)
            if (not stat.S_ISREG(node.st_mode) or node.st_nlink != 1 or node.st_uid != os.getuid()
                    or not os.path.samestat(node, name.lstat())):
                raise OSError('unsafe_verify_batch_lock')
            yield
        finally:
            os.close(fd)


def prepare(root, queue, bound):
    """Read the current window outside both shared locks; each commit reconfirms its generation."""
    source = _source(queue)
    if source is None and not unresolved(queue):
        return True
    held = Work.entries(queue, 'hold', control_only=True)
    for work in held:
        state = work.state
        if state['phase'] != 'held' or state['blobs']['raw'].get('storage') != 'inode':
            continue
        batch = batch_handoff.Batch(work)
        if batch_consumer.parked(batch):
            continue
        @contextmanager
        def commit():
            with _locked(root, queue):
                if (not state['binding']['batch'].get('native') and
                        next(queue.parent.glob(queue.name + '.claimed.*'), None) is not None):
                    raise BatchPending('released_claim_requires_reassessment')
                yield
        if held.issues:
            return False
        done = batch_consumer.advance(batch, gate=commit)
        return bool(done and source is None and not unresolved(queue))
    with _locked(root, queue):
        guard_claim(queue, bound)
        return True
