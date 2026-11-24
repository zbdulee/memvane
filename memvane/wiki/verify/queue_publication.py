"""Recover one verify queue publication by its private work and same-directory rename witness."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from memvane.kernel.atomic_rename import fsync_dir
from memvane.store.api import regular
from memvane.wiki.verify import candidate_stage, native_review, pending_ownership as ownership
from memvane.wiki.verify.handoff_work import Work
from memvane.wiki.workspace import api as workspace


def _identity(info):
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _queue_node(queue):
    try:
        info = queue.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode):
        raise OSError('invalid verify queue')
    return {**_identity(info), 'size': info.st_size, 'mtime': info.st_mtime_ns, 'ctime': info.st_ctime_ns}


def _swap(queue, work):
    name = work.state['binding']['swap']
    if not isinstance(name, str) or re.fullmatch(r'\.verify-swap-[a-f0-9]{32}', name) is None:
        raise ValueError('invalid verify publication witness')
    return queue.with_name(name)


def reserve(queue, job, selected, before, image, *, produced):
    binding = {'audience': 'native', 'job': job, 'route': job.get('selectedRoute'), 'produced': produced,
               'swap': '.verify-swap-' + uuid.uuid4().hex}
    if selected is not None:
        binding['route'] = workspace.route_identity(selected)
    return Work.create(queue, 'publication', binding, before, image=image)


def _stage(queue, work, image):
    swap = _swap(queue, work)
    fd = os.open(swap, os.O_RDWR | os.O_EXCL | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w+b') as stream:
        node = _identity(os.fstat(fd))
        work.commit('staged', swapNode=node)
        if queue.exists():
            os.fchmod(fd, stat.S_IMODE(queue.lstat().st_mode))
        stream.write(image)
        stream.flush()
        os.fsync(fd)
        fsync_dir(queue.parent)
        check, state, _ = regular(queue.parent, swap.name, len(image))
        if check != image or any(state[key] != value for key, value in node.items()):
            raise OSError('incomplete verify publication')
    return swap


def _complete(root, queue, work):
    job = work.state['binding']['job']
    state = ownership.load(queue)
    changed = False
    rel = Path(job['pendingPath'])
    if not rel.parts or rel.is_absolute() or '..' in rel.parts or rel.as_posix() != job['pendingPath']:
        raise ValueError('invalid verify publication path')
    if work.state['binding']['produced'] and os.path.lexists(root / rel):
        image = work.read('image')
        candidate = work.state['candidate']
        node = {}
        try:
            material, node, _ = regular(root, rel.as_posix(), len(image))
        except (ValueError, FileNotFoundError):
            material = None  # A released consumer or a later edit can already own this name.
        if (image == material and candidate.get('stage') == 'staged'
                and candidate.get('inode') == node.get('ino') and candidate.get('mtime') == node.get('mtime_ns')):
            ownership.remember(root, state, job, image)
            changed = True
    for plan in work.state.get('retired', ()):
        name = plan['job']['pendingPath']
        if state['owners'].get(name) == plan['sha256'] and name not in state['retirements']:
            state['retirements'][name] = plan
            changed = True
    if changed:
        ownership.store(queue, state)
    native_review.activate(root, queue, work)
    work.commit('finished')
    work.archive()


def cancel(root, queue, work, references):
    if work.state['phase'] != 'cancelled':
        work.commit('cancelled')
    swap = _swap(queue, work)
    try:
        info = swap.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return False
        if work.state.get('swapNode') != _identity(info):
            return False
        swap.unlink()
        fsync_dir(queue.parent)
    job = work.state['binding']['job']
    if work.state['binding']['produced']:
        if references is None or job['pendingPath'] in references:
            return False
        if not candidate_stage.cancel(work, root):
            return False
    native_review.cancel(queue, work)
    work.commit('finished')
    work.archive()
    return True


def publish(root, queue, work, after, before_node, retired, holds):
    image = b''.join(after)
    swap = _stage(queue, work, image)
    work.commit('prepared', queueBefore=before_node, swapDigest=hashlib.sha256(image).hexdigest(),
                retired=retired, holds=holds)
    if _queue_node(queue) != before_node:
        raise OSError('verify queue changed during handoff')
    os.replace(swap, queue)
    fsync_dir(queue.parent)
    work.commit('published')
    _complete(root, queue, work)


def recover(root, queue, entries, references, selected):
    for work in entries:
        route = work.state['binding'].get('route')
        if route != (workspace.route_identity(selected) if selected is not None else None):
            continue
        phase = work.state['phase']
        if phase == 'finished':
            work.archive()
            continue
        if phase == 'prepared' and not os.path.lexists(_swap(queue, work)):
            fsync_dir(queue.parent)
            work.commit('published')
            phase = 'published'
        if phase == 'published':
            _complete(root, queue, work)
        else:
            from memvane.wiki.verify.pending_handoff import _references
            other_refs, _ = _references(queue, exclude_parent=work.state['id'])
            cancel(root, queue, work, other_refs)


def reserved(queue, path, *, fresh=False):
    publications = Work.entries(queue, 'publication')
    if publications.issues and not fresh:
        return True
    if any(work.state['phase'] != 'finished' and work.state['binding']['job'].get('pendingPath') == path
           for work in publications):
        return True
    return ownership.reserved(queue, path, fresh=fresh)
