"""Durably bind the first visible card to its native verification occurrence."""
import json
import os
import re
import stat
from pathlib import Path
import uuid

from memvane.kernel.atomic_rename import canonical_json_bytes, rename_noreplace
from memvane.settings import api as settings
from memvane.wiki.workspace.handoff.api import evidence as regular
from memvane.wiki.card.api import unresolved_pending_refreshes_strict
from memvane.wiki.workspace import types as ct
from memvane.wiki.verify import native_review
from memvane.wiki.verify.handoff_work import HOLD_SCAN_LIMIT, Work
from memvane.wiki.workspace import api as workspace


def _resume(root, wiki, queue, work, selected):
    state = work.state
    binding = state['binding']
    route = workspace.route_identity(selected) if selected is not None else None
    if binding.get('route') != route or binding['wiki'] != wiki.relative_to(root).as_posix():
        return False
    rel = Path(binding['job']['targetPath'])
    path = root / rel
    if (rel.is_absolute() or '..' in rel.parts or path.resolve() != path or
            not path.is_relative_to(wiki) or any(p.startswith('.') for p in path.relative_to(wiki).parts)):
        raise ValueError('unsafe initial card path')
    if unresolved_pending_refreshes_strict(root, before_ledger_write=True,
                                         missing_target=None if path.exists() else binding['manifest'],
                                         append_for=binding['manifest']) is None:
        return False
    image = work.read('image')
    proof = state.get('proof', {})
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.fstat(parent)
        parent_node = [directory.st_dev, directory.st_ino]
        if proof and (proof.get('parent') != parent_node or
                      not re.fullmatch(r'\.first-[0-9a-f]{32}', proof.get('temp', ''))):
            raise ValueError('initial card directory changed')
        if not proof.get('inode'):
            unknown = proof.get('unknown', [])
            tmp = proof.get('temp')
            if tmp and os.path.lexists(path.with_name(tmp)):
                if len(unknown) >= 64:
                    return False
                unknown = [*unknown, tmp]
                tmp = None
            if not tmp:
                tmp = '.first-' + uuid.uuid4().hex
                proof = dict(published=False, temp=tmp, parent=parent_node, unknown=unknown)
                work.commit('held', transaction=state['id'], proof=proof)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as output:
                node = os.fstat(output.fileno())
                output.write(image)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(parent)
            proof = dict(proof, inode=node.st_ino, device=node.st_dev)
            work.commit('held', transaction=state['id'], proof=proof)
        tmp = proof['temp']
        def matches(name):
            raw, node, _ = regular(path.parent, name, len(image))
            return raw == image and (node['dev'], node['ino']) == (proof['device'], proof['inode'])
        if not os.path.lexists(path):
            if not matches(tmp):
                return False
            if not _released_target_clear(queue, rel.as_posix()):
                return False
            workspace.recheck(selected)
            rename_noreplace(parent, tmp, path.name)
        os.fsync(parent)
        if not matches(path.name):
            return False
        _record_manifest(root, binding['manifest'])
        if not matches(path.name):
            return False
        proof = dict(proof, published=True)
        work.commit('held', transaction=state['id'], proof=proof)
        if not binding['review']:
            work.commit('finished', proof=proof).archive()
        return True
    finally:
        os.close(parent)


def _released_target_clear(queue, rel):
    from itertools import islice
    from memvane.wiki.verify import pending_handoff, queue_batch_prepare
    if queue_batch_prepare.unresolved(queue):
        return False
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        return False
    remaining = pending_handoff._VERIFY_QUEUE_BYTES
    for path in [queue, *claims]:
        rows, used = pending_handoff._rows(path, remaining)
        remaining -= used
        if any(not raw.endswith(b'\n') or not job.get('targetPath') or job.get('targetPath') == rel
               for raw, job in rows):
            return False
    return True


def _record_manifest(root, row):
    path = settings.ledger(root, ct.MANIFEST)
    if path is None or unresolved_pending_refreshes_strict(root, before_ledger_write=True, append_for=row) is None:
        raise OSError('initial card manifest unavailable')
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags, 0o600), 'r+b') as stream:
        node = os.fstat(stream.fileno())
        if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1 or node.st_uid != os.getuid():
            raise OSError('unsafe initial card manifest')
        latest, last = None, b''
        for line in stream:
            last = line
            try:
                recorded = json.loads(line)
                if not isinstance(recorded, dict):
                    raise ValueError('invalid initial card manifest')
                if recorded.get('targetPath') == row['targetPath']:
                    latest = recorded
            except (ValueError, RecursionError):
                if line.strip():
                    raise ValueError('invalid initial card manifest')
        after = path.lstat()
        if (not os.path.samestat(node, after) or
                (node.st_size, node.st_ctime_ns) != (after.st_size, after.st_ctime_ns) or
                (last and not last.endswith(b'\n'))):
            raise OSError('initial card manifest changed')
        present = latest == row
        if not present:
            data = canonical_json_bytes(row) + b'\n'
            stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        if not present:
            stream.seek(-len(data), os.SEEK_END)
            if stream.read(len(data)) != data:
                raise OSError('initial card manifest changed')
        if not os.path.samestat(node, path.lstat()):
            raise OSError('initial card manifest changed')
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def create(root, wiki, queue, job, image, selected, *, review, manifest):
    if queue is None:
        return False
    binding = dict(purpose='native-review', action='create', job=job, targetPath=job['targetPath'],
                   wiki=wiki.relative_to(root).as_posix(), review=review, manifest=manifest,
                   route=workspace.route_identity(selected) if selected is not None else None)
    try:
        queue.parent.mkdir(parents=True, exist_ok=True)
        with native_review._locked(queue, blocking=False):
            for work in Work.entries(queue, 'hold', control_only=True, max_entries=HOLD_SCAN_LIMIT):
                state = work.state
                old = state['binding']
                if (old.get('action') == 'create' and old.get('targetPath') == job['targetPath']
                        and state['phase'] in ('reserved', 'held') and not state.get('proof', {}).get('published')):
                    # A previous rename may have exposed this name before the editor unlinked it.
                    if old.get('route') != binding['route'] or work.read('image') != image:
                        return False
                    return _resume(root, wiki, queue, work, selected)
            work = Work.create(queue, 'hold', binding, canonical_json_bytes(job) + b'\n', image=image)
            return _resume(root, wiki, queue, work, selected)
    except (KeyError, OSError, RuntimeError, ValueError):
        return False


def recover(root, wiki, queue, selected):
    try:
        with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), native_review._locked(queue, blocking=False):
            Work.reclaim_orphans(queue, 'hold')
    except OSError:
        pass
    for work in Work.entries(queue, 'hold', control_only=True, max_entries=HOLD_SCAN_LIMIT):
        state = work.state
        binding = state['binding']
        if (binding.get('purpose') == 'native-review' and binding.get('action') == 'create'
                and state['phase'] in ('reserved', 'held') and not state.get('proof', {}).get('published')):
            try:
                with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), native_review._locked(queue, blocking=False):
                    current = Work.lookup(queue, 'hold', state['id'])
                    if current is not None and not current.state.get('proof', {}).get('published'):
                        _resume(root, wiki, queue, current, selected)
            except (OSError, KeyError, RuntimeError, ValueError):
                continue
