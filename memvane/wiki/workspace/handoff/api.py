"""Raw queue occurrences and their native live name."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from memvane.wiki.workspace import queue_return
from memvane.wiki.workspace.handoff import budget
from memvane.wiki.workspace.handoff.budget import bounded, child_options, charge


SLAB = 64 * 1024
MAX_ROWS = 4096
MAX_BYTES = 8 * 1024 ** 2


def native_live(queue, *, create=False):
    folder = queue_return._dir(queue, create=create)
    return folder / queue.name if folder is not None else None


def public_name(path):
    path = Path(path)
    parent = path.parent
    if (parent.name == hashlib.sha256(os.fsencode(path.name)).hexdigest()[:24]
            and tuple(p.name for p in list(parent.parents)[:3]) == ('returns', 'v1', '.handoff')):
        return parent.parents[3] / path.name
    if path.name.startswith(('ready-', 'native.claimed.')) and tuple(p.name for p in list(parent.parents)[:3]) == ('returns', 'v1', '.handoff'):
        for name in ('source-queue.jsonl', 'verify-queue.jsonl'):
            if parent.name == hashlib.sha256(os.fsencode(name)).hexdigest()[:24]:
                return parent.parents[3] / name
    return path


def _unique(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError('duplicate_queue_key')
    return row


def _invalid(value):
    raise ValueError('invalid_json_constant')


def occurrence(raw):
    """The raw LF terminator is protocol data; Python's other line separators are content."""
    if len(raw) > SLAB or not raw.endswith(b'\n'):
        return None, False
    try:
        text = raw.decode('utf-8')
        row = json.loads(text, object_pairs_hook=_unique, parse_constant=_invalid)
        if not isinstance(row, dict):
            return None, False
        projected = json.dumps(row, ensure_ascii=False, allow_nan=False)
        # The released reader runs splitlines() both today and on its orphan serialization.
        safe = len(text.splitlines()) == 1 and len(projected.splitlines()) == 1
        projected.encode('utf-8')
        return row, safe
    except (UnicodeError, ValueError, RecursionError):
        return None, False


def destinations(raw):
    """Validate the actual released JSONL and its released orphan serialization before publishing."""
    public, native, opaque = [], [], []
    pieces = raw.split(b'\n')
    for line in pieces[:-1]:
        physical = line + b'\n'
        job, safe = occurrence(physical)
        (public if safe else native if job is not None else opaque).append(physical)
    opaque.append(pieces[-1])
    return b''.join(public), b''.join(native), b''.join(opaque)


def prepare_delivery(queue, key, raw):
    """Create a short, private no-replace witness before a batch records its offering."""
    folder = queue_return._dir(queue, create=True)
    name = hashlib.sha256(key.encode('ascii')).hexdigest()[:32]
    queue_return._exact_file(folder / ('offer-' + name), raw)
    return name


def deliver(queue, name):
    """A prepared witness disappears only by moving onto a native consumer's ready name."""
    import re
    from memvane.kernel.atomic_rename import fsync_dir
    if not re.fullmatch('[a-f0-9]{32}', name):
        raise ValueError('invalid_queue_delivery')
    folder = queue_return._dir(queue)
    if folder is None:
        raise OSError('missing_queue_delivery')
    offer = folder / ('offer-' + name)
    if os.path.lexists(offer):
        queue_return._move_claim(offer, folder / ('ready-' + name))
    fsync_dir(folder)


def bounded_rows(path, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
    """Yield ordered physical rows with byte offsets, without allocating an oversized record."""
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_size > max_bytes or not os.path.samestat(before, path.lstat())):
            raise OSError('queue_snapshot_limit')
        budget.charge(budget.channel(path), before.st_size)
        offset = count = 0
        carry = b''
        while offset + len(carry) < before.st_size:
            data = stream.read(min(SLAB, before.st_size - stream.tell()))
            if not data:
                raise OSError('queue_short_read')
            pieces = (carry + data).split(b'\n')
            carry = pieces.pop()
            for piece in pieces:
                raw = piece + b'\n'
                if len(raw) > SLAB or count >= max_rows:
                    raise OSError('queue_occurrence_limit')
                count += 1
                budget.observe(path, before.st_ino, offset, raw)
                yield offset, raw
                offset += len(raw)
            if len(carry) >= SLAB:
                raise OSError('queue_occurrence_limit')
        if carry:
            if count >= max_rows:
                raise OSError('queue_occurrence_limit')
            budget.observe(path, before.st_ino, offset, carry)
            yield offset, carry
        after = os.fstat(fd)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or not os.path.samestat(after, path.lstat())):
            raise OSError('queue_snapshot_changed')


def evidence(root, relative, limit):
    """Read one verified card snapshot and reuse it only while both named identities still match."""
    import stat
    from memvane.store.api import regular
    ledger = budget.current()
    if ledger is None:
        return regular(root, relative, limit)
    root = Path(root)
    path = root / relative
    if (not path.is_relative_to(root) or root.resolve() != root or path.resolve() != path or
            Path(relative).is_absolute() or '..' in Path(relative).parts):
        raise ValueError('invalid_handoff_evidence_path')
    node, directory = path.lstat(), root.lstat()
    lane = 'verify' if path.name.startswith('.verify-swap-') else 'evidence'
    if lane == 'evidence':
        limit = min(limit, 4 * 1024 ** 2)
    if (not stat.S_ISREG(node.st_mode) or node.st_uid != os.getuid() or node.st_nlink != 1
            or node.st_size > limit):
        raise ValueError('invalid_handoff_evidence_snapshot')
    key = (lane, path, directory.st_dev, directory.st_ino, node.st_dev, node.st_ino, node.st_size,
           node.st_mtime_ns, node.st_ctime_ns)
    if key not in ledger.snapshots:
        budget.charge(lane, node.st_size)
        result = regular(root, relative, node.st_size)
        if (result[1].get('ctime_ns') != node.st_ctime_ns or result[1].get('ino') != node.st_ino or
                result[2] != {'dev': directory.st_dev, 'ino': directory.st_ino}):
            raise BlockingIOError('handoff_evidence_changed')
        ledger.snapshots[key] = result
    return ledger.snapshots[key]


def withdraw(queue):
    """The caller holds the released queue and card locks and has observed no competing claim."""
    folder = queue_return._dir(queue, create=True)
    ready = folder / ('ready-' + queue_return._token())
    queue_return._move_claim(queue, ready)
    queue_return.ensure_live(queue)
    return ready


def delivery_pending(queue, name):
    folder = queue_return._dir(queue)
    return folder is not None and ((folder / ('ready-' + name)).exists() or
                                   next(folder.glob('native.claimed.*.' + name), None) is not None)


def take_delivery(queue, name, validate):
    """Own only this batch's durable witness; released claims and unrelated ready names are untouched."""
    import re
    from memvane.wiki.workspace.queue import _is_pid_alive
    if re.fullmatch('[0-9a-f]{32}', name) is None:
        raise ValueError('invalid_queue_delivery')
    with queue_return.legacy_card_lock(queue, blocking=False), queue_return.queue_locked(queue, blocking=False):
        if not validate():
            return None
        folder = queue_return._dir(queue)
        ready = folder / ('ready-' + name)
        own = folder / ('native.claimed.%d.%s' % (os.getpid(), name))
        if ready.exists():
            queue_return._move_claim(ready, own)
            return own
        for path in folder.glob('native.claimed.*.' + name):
            match = re.fullmatch(r'native\.claimed\.(\d+)\.' + name, path.name)
            if match and not _is_pid_alive(int(match[1])):
                queue_return._move_claim(path, own)
                return own
    return None


def defer_unread(queue, path):
    """Return an unstarted, owned occurrence for a new pass without fabricating a successful read."""
    import re
    if path is None or path.parent != queue_return._dir(queue):
        return
    match = re.fullmatch(r'native\.claimed\.%d\.([0-9a-f]{32})' % os.getpid(), path.name)
    if match:
        with queue_return.queue_locked(queue, blocking=False):
            queue_return._move_claim(path, path.with_name('ready-' + match[1]))


def settle_observed(queue, stamp, raw):
    """The caller owns the project and queue locks; only one exact, fully proved public row is eligible."""
    node = queue.lstat()
    if (node.st_dev, node.st_ino, node.st_size, node.st_ctime_ns, node.st_mtime_ns) != stamp:
        return False
    claim = queue.with_name(queue.name + '.claimed.%d.%s' % (os.getpid(), queue_return._token()))
    queue_return._move_claim(queue, claim, private=False)
    queue_return.ensure_live(queue)
    rows = queue_return.ClaimRows(claim)
    if rows.size != len(raw) or rows.digest != hashlib.sha256(raw).hexdigest():
        return False
    queue_return.finish_locked(queue, claim, [], rows)
    return True
