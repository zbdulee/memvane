"""Review an independent ordinary occurrence without removing a released queue reference."""
from __future__ import annotations

import hashlib
import os

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.wiki.workspace.handoff.api import evidence as regular
from memvane.store.types import MAX_BODY
from memvane.wiki.verify import handoff_work as store, native_review
from memvane.wiki.verify.pending_replacement import pending_matches_job
from memvane.wiki.workspace import api as workspace
from memvane.wiki.workspace.handoff.api import bounded_rows, occurrence


def _name(inode, offset, raw):
    key = canonical_json_bytes([inode, offset, hashlib.sha256(raw).hexdigest()])
    return 'ordinary-' + hashlib.sha256(key).hexdigest()


def _get(queue, name):
    try:
        with store.private_root(queue) as fd:
            identity = store._file(fd, name, 32).decode('ascii')
    except FileNotFoundError:
        return None
    work = store.Work.lookup(queue, 'hold', identity)
    if work is None or work.state['binding'].get('slot') != name:
        raise OSError('ordinary_slot_binding_changed')
    return work


def _clear_value(value):
    try:
        return len(canonical_json_bytes(value).decode().splitlines()) == 1
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def safe_outputs(job):
    # The selected branch fingerprint remains private. Any field copied to public ledgers must round-trip.
    return _clear_value({k: v for k, v in job.items() if k != 'sourceFingerprints'})


def _registration(queue, origin, before, offset, raw, job, proof, bound):
    slot = _name(before.st_ino, offset, raw)
    with native_review._locked(queue, blocking=False):
        current = origin.lstat()
        if (not os.path.samestat(before, current) or before.st_size != current.st_size
                or before.st_mtime_ns != current.st_mtime_ns or before.st_ctime_ns != current.st_ctime_ns):
            return
        work = _get(queue, slot)
        if work is None:
            binding = dict(purpose='native-review', action='borrow', route=workspace.route_identity(bound),
                           targetPath=job['targetPath'], job=job, slot=slot)
            work = next((w for w in store.Work.entries(queue, 'hold', control_only=True) if w.state['binding'] == binding), None)
            if work is None:
                work = store.Work.create(queue, 'hold', binding, raw)
            with store.private_root(queue) as fd:
                store._publish(fd, slot, work.state['id'].encode('ascii'))
        if work.state['phase'] == 'reserved' and work.read('raw') == raw:
            work.commit('held', transaction=work.state['id'], proof=dict(proof, published=True))


def owns_occurrence(queue, inode, offset, raw):
    work = _get(queue, _name(inode, offset, raw))
    return bool(work and work.state['phase'] in ('held', 'finished') and work.read('raw') == raw)


def _inputs(queue):
    from memvane.wiki.verify import batch_handoff
    from memvane.wiki.workspace.handoff.api import charge
    try:
        before = queue.lstat()
        for offset, raw in bounded_rows(queue):
            yield queue, before, offset, raw
    except (OSError, ValueError):
        pass
    for work in store.Work.entries(queue, 'hold', control_only=True):
        state = work.state
        if state['blobs']['raw'].get('storage') != 'inode' or state['phase'] != 'held':
            continue
        with batch_handoff.Batch(work)._input() as (fd, node):
            if state['sourceNode'] != node:
                continue
            origin, before = work.folder / 'raw', os.fstat(fd)
            for offset, length in state['proof'].get('handoff', {}).get('ordinary', []):
                if not 0 < length <= 65536 or not 0 <= offset <= node['size'] - length:
                    raise ValueError('invalid_ordinary_batch_pointer')
                charge('verify', length)
                raw = os.pread(fd, length, offset)
                if batch_handoff._node(os.fstat(fd)) != node:
                    break
                yield origin, before, offset, raw


def borrow(root, wiki, queue, config, bound):
    from memvane.wiki.verify.pending_replacement import handoff_freshness
    from memvane.wiki.card.api import parse_frontmatter
    try:
        observed = 0
        for origin, before, offset, raw in _inputs(queue):
            job, _ = occurrence(raw)
            if (not job or not isinstance(job.get('targetPath'), str) or not job.get('sourceRevisions')
                    or 'pendingPath' in job or '_pendingCleanup' in job or not safe_outputs(job)):
                continue
            if _get(queue, _name(before.st_ino, offset, raw)) is not None:
                continue
            observed += 1
            if observed > 16:
                break
            path = root / job['targetPath']
            if path == wiki or not path.is_relative_to(wiki) or path.resolve() != path:
                continue
            try:
                image, node, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
                text = image.decode()
                if (not pending_matches_job(text, job) or parse_frontmatter(text)[0].get('status') != 'generated' or
                        (handoff_freshness(root, config, job) or {}).get('state') != 'fresh'):
                    continue
                proof = dict(inode=node['ino'], device=node['dev'], sha256=hashlib.sha256(image).hexdigest())
                _registration(queue, origin, before, offset, raw, job, proof, bound)
            except (ValueError, OSError):
                continue
    except (FileNotFoundError, OSError, ValueError):
        return


def _selected_status(root, wiki, queue, job, stage, target, config, raw, offset, inode):
    from memvane.wiki.verify import promotion
    from memvane.wiki.verify.pending_replacement import handoff_freshness
    from memvane.wiki.card.api import parse_frontmatter
    if '_pendingCleanup' in job or promotion.has_intent(root, job):
        return 'run'
    try:
        path = stage[0] if stage else target
        data, node, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
        if not pending_matches_job(data.decode(), job):
            return 'retain'
        freshness = handoff_freshness(root, config, job) or {}
        if freshness.get('state') != 'fresh':
            return 'run' if stage and freshness.get('reason') == 'content_hash_mismatch' else 'retry'
        if stage:
            base, _, _ = regular(wiki, target.relative_to(wiki).as_posix(), MAX_BODY)
            return 'run' if hashlib.sha256(base).hexdigest() == stage[1] else 'retain'
        if parse_frontmatter(data.decode())[0].get('status') == 'verified':
            binding = dict(purpose='verified-occurrence', targetPath=job['targetPath'], inode=inode,
                           offset=offset, digest=hashlib.sha256(raw).hexdigest())
            with native_review._locked(queue, blocking=False):
                work = store.Work.create(queue, 'hold', binding, raw)
                work.commit('held', proof={'sha256': hashlib.sha256(data).hexdigest(), 'node': node})
                work.commit('finished').archive()
            return 'complete'
    except FileNotFoundError:
        return 'retain'
    except (UnicodeError, OSError, ValueError):
        return 'retry'
    return 'run'


def preserve_pending(root, wiki, queue, rows):
    """A released writer may reuse a hidden filename after withdrawal; retain its bytes first."""
    from memvane.wiki.verify.pending_replacement import pending_replacement
    node = queue.lstat()
    offset = 0
    for raw, job in rows:
        start, offset = offset, offset + len(raw)
        if not isinstance(job.get('pendingPath'), str) or not isinstance(job.get('targetPath'), str):
            continue
        target = root / job['targetPath']
        try:
            if target.resolve() != target or not target.is_relative_to(wiki):
                continue
            path, _ = pending_replacement(root, wiki, job, target)
        except (OSError, ValueError):
            continue
        try:
            image, _, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
        except FileNotFoundError:
            continue
        binding = dict(purpose='observed-pending', targetPath=job['targetPath'], inode=node.st_ino, offset=start,
                       rawSha=hashlib.sha256(raw).hexdigest(), imageSha=hashlib.sha256(image).hexdigest())
        name = 'observed-' + hashlib.sha256(canonical_json_bytes(binding)).hexdigest()
        with store.private_root(queue, create=True) as fd:
            try:
                identity = store._file(fd, name, 32).decode('ascii')
                work = store.Work.lookup(queue, 'hold', identity)
            except FileNotFoundError:
                works = store.Work.entries(queue, 'hold', control_only=True)
                work = next((w for w in works if w.state['binding'] == binding), None)
                if work is None:
                    work = store.Work.create(queue, 'hold', binding, raw, image=image)
                store._publish(fd, name, work.state['id'].encode('ascii'))
        if not work or work.state['binding'] != binding or work.read('image') != image or work.read('raw') != raw:
            raise OSError('unproved_pending_snapshot')
        if work.state['phase'] == 'reserved':
            work.commit('held', proof={'retained': True})
        if work.state['phase'] == 'held':
            work.commit('finished').archive()


def classify(root, wiki, rows, queue, config, selected):
    """Only the exact original private occurrence can consume a borrowed completion proof."""
    from memvane.wiki.verify.wiki_verify_worker import _valid_cleanup_marker
    kept, deferred = [], []
    for (line, job), (offset, _length) in zip(rows, rows.line_ranges):
        if (not isinstance(job.get('targetPath'), str) or not job['targetPath']
                or '_pendingCleanup' in job and not _valid_cleanup_marker(job)
                or not safe_outputs(job)):
            rows.quarantine(line)
            continue
        target = root / job['targetPath']
        if target.resolve() != target or not target.is_relative_to(wiki) or target == wiki:
            rows.quarantine(line)
            continue
        try:
            from memvane.wiki.verify.pending_replacement import pending_replacement
            stage = pending_replacement(root, wiki, job, target)
            files = [target, stage[0]] if stage else [target]
            if any(p.exists() and p.lstat().st_size > MAX_BODY for p in files):
                deferred.append(line)
                continue
        except (OSError, TypeError, ValueError):
            rows.quarantine(line)
            continue
        raw = (line + '\n').encode()
        work = _get(queue, _name(rows.inode, offset, raw))
        if work and work.state['phase'] == 'finished' and work.state.get('proof', {}).get('completed'):
            continue
        status = (_selected_status(root, wiki, queue, job, stage, target, config, raw, offset, rows.inode)
                  if selected is not None else 'run')
        if status == 'run':
            kept.append((line, job))
        elif status == 'retry':
            deferred.append(line)
        elif status == 'retain':
            rows.quarantine(line)
    rows[:] = kept
    return deferred


def claim_for_review(queue, selected_route, public_ready, claim, read, delivery=None):
    from memvane.wiki.verify import queue_batch_prepare
    try:
        if public_ready:
            claimed = claim(queue, private=True, before_claim=lambda path: queue_batch_prepare.guard_claim(path, selected_route))
        elif delivery:
            from memvane.wiki.workspace.handoff.api import take_delivery
            claimed = take_delivery(queue, delivery, lambda: queue_batch_prepare.delivery(queue) == delivery)
        else:
            claimed = None
    except OSError as error:
        import errno
        if not isinstance(error, BlockingIOError) and error.errno != errno.ENOSPC:
            raise
        claimed, public_ready = None, False
    rows = read(claimed) if claimed is not None else []
    if claimed is not None and not hasattr(rows, 'finish_verified'):
        from memvane.wiki.workspace.handoff.api import defer_unread
        defer_unread(queue, claimed)
        claimed, public_ready = None, False
    return claimed, rows, public_ready
