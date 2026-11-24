"""Seal an oversized verify input before an ordinary consumer can claim it."""
from __future__ import annotations

import fcntl
from itertools import islice
import os
import stat

from memvane.wiki.verify import batch_handoff
from memvane.wiki.verify.handoff_work import HEX_ID, Work
from memvane.wiki.verify.pending_replacement import _VERIFY_QUEUE_BYTES
from memvane.wiki.workspace import api as workspace


def _dead(queue, claim):
    suffix = claim.name.removeprefix(queue.name + '.claimed.')
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
        if not (queue.parent / '.handoff').exists():
            return False
        holds = Work.entries(queue, 'hold')
    # A foreign generation alone has no authority over an independent native input.
    uncertain = any(issue['reason'] == 'handoff_scan_limit' or HEX_ID.fullmatch(issue['name'])
                    for issue in holds.issues)
    return uncertain or any(item.state['blobs']['raw'].get('storage') == 'inode' for item in holds)


def _source(queue):
    """Only stat inputs here. Unobserved or overlapping claims preserve every public name."""
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        raise BlockingIOError('verify_batch_claim_limit')
    try:
        size = queue.lstat().st_size
    except FileNotFoundError:
        size = 0
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


class BatchPending(BlockingIOError):
    pass


def _ready_locked(queue, bound):
    source = _source(queue)
    if source is None and not unresolved(queue):
        return True

    def dead(claim):
        return _dead(queue, claim)

    active = batch_handoff.active(queue, orphan_dead=dead)
    if active or active.issues:
        return False
    if source is not None:
        binding = {'purpose': 'verify-batch', 'route': workspace.route_identity(bound)}
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


def prepare(root, queue, bound):
    """Return False while the retained ranges still own review or unknown references."""
    if _source(queue) is None and not unresolved(queue):
        return True
    with workspace.card_write_lock(root, blocking=False):
        name = workspace.queue_lock_path(queue)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            node = os.fstat(fd)
            if (not stat.S_ISREG(node.st_mode) or node.st_nlink != 1 or node.st_uid != os.getuid()
                    or not os.path.samestat(node, name.lstat())):
                raise OSError('unsafe_verify_batch_lock')
            guard_claim(queue, bound)
            return True
        finally:
            os.close(fd)
