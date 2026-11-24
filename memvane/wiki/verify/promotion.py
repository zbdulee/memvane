"""Exchange a verified candidate with the visible card, retaining the actual displaced inode."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, rename_exchange, rename_noreplace, write_new_file
from memvane.settings.api import compile_dir, find_project_config
from memvane.settings.types import CURRENT


def _snapshot(path, *, sync=False):
    fd = os.open(path, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('unsafe promotion file')
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
        if sync:
            os.fsync(fd)
        after = path.lstat()
        if (not os.path.samestat(before, after) or before.st_mtime_ns != after.st_mtime_ns or
                before.st_ctime_ns != after.st_ctime_ns or before.st_size != after.st_size):
            raise ValueError('promotion file changed')
    return {'dev': before.st_dev, 'ino': before.st_ino, 'sha256': digest.hexdigest()}


def _folder(root, job, create=False):
    canonical = root / CURRENT / 'compile'
    if canonical.resolve() != canonical:
        raise ValueError('unsafe promotion archive')
    base = compile_dir(root, create=create)
    if base is None or base != canonical:
        raise ValueError('unsafe promotion archive')
    # The time, marker, selected route and exact source revision distinguish generations.
    binding = {key: job[key] for key in ('targetPath', 'pendingPath', 'sourceHash', 'expectedTargetHash',
               'sourceRevisions', 'sourceFingerprints', 'selectedRoute', 'ts', 'visibleVerdict') if key in job}
    identity = hashlib.sha256(canonical_json_bytes(binding)).hexdigest()
    if create:
        fsync_dir(root)
        fsync_dir(base.parent)
    path = base
    for part in ('retired-wiki', '.promotions', identity):
        path = path / part
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
            fsync_dir(path.parent)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('unsafe promotion archive')
    return path


def _record(path, value=None):
    if value is not None and not os.path.lexists(path):
        payload = dict(value, version=1)
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        write_new_file(path, canonical_json_bytes(dict(payload, checksum=digest)))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 16384 or info.st_nlink != 1:
            raise ValueError('unsafe promotion record')
        data = json.load(stream)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('invalid promotion record')
    digest = data.pop('checksum', None)
    if hashlib.sha256(canonical_json_bytes(data)).hexdigest() != digest:
        raise ValueError('promotion record checksum changed')
    del data['version']
    if value is not None and data != value:
        raise ValueError('promotion record changed')
    if value is not None:
        fsync_dir(path.parent)
    return data


def _intent(root, folder, job):
    data = _record(folder / 'intent')
    if not isinstance(data, dict) or set(data) != {'live', 'pending', 'base', 'stamp'}:
        raise ValueError('invalid promotion binding')
    stamp = data['stamp']
    pending_path = job.get('pendingPath') or (folder / 'review.md').relative_to(root).as_posix()
    if (data['live'] != job['targetPath'] or data['pending'] != pending_path
            or (job.get('pendingPath') is not None and data['base'] != job['expectedTargetHash']) or not isinstance(stamp, dict)
            or set(stamp) != {'dev', 'ino', 'sha256'} or type(stamp['dev']) is not int
            or type(stamp['ino']) is not int or not isinstance(stamp['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', stamp['sha256'])
            or _snapshot(folder / 'base')['sha256'] != data['base']):
        raise ValueError('invalid promotion binding')
    return data


def has_intent(root, job):
    """Recognize this job's durable promotion even while its stamped name is absent."""
    try:
        _intent(root, _folder(root, job), job)
        return True
    except (KeyError, TypeError, OSError, RuntimeError, ValueError):
        if job.get('pendingPath') is not None:
            return False
        try:
            return retirement_status(root, job) is not None
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return False


def _move(source, dest, *, exchange=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if source.parent.resolve() != source.parent or dest.parent.resolve() != dest.parent:
        raise ValueError('unsafe promotion directory')
    left = os.open(source.parent, flags)
    try:
        right = os.open(dest.parent, flags)
        try:
            if (source.parent.resolve() != source.parent or dest.parent.resolve() != dest.parent
                    or not os.path.samestat(os.fstat(left), source.parent.stat())
                    or not os.path.samestat(os.fstat(right), dest.parent.stat())):
                raise ValueError('promotion directory changed')
            if exchange:
                rename_exchange(left, source.name, right, dest.name)
            else:
                rename_noreplace(left, source.name, dest.name, target_parent_fd=right)
            os.fsync(left)
            os.fsync(right)
        finally:
            os.close(right)
    finally:
        os.close(left)


def _finish(root, folder, intent):
    path = folder / 'displaced.md'
    displaced = _snapshot(path, sync=True)
    if displaced['dev'] == intent['stamp']['dev'] and displaced['ino'] == intent['stamp']['ino']:
        raise ValueError('no displaced inode')
    # The exchange itself can precede either directory sync or this archive sidecar.
    fsync_dir(folder)
    fsync_dir((root / intent['live']).parent)
    sidecar = {'path': intent['live'], 'archivePath': path.relative_to(root).as_posix(),
               'expectedTargetHash': intent['base'], 'basePath': (folder / 'base').relative_to(root).as_posix(),
               'candidate': intent['stamp'], 'displaced': displaced}
    _record(folder / 'archive.json', sidecar)
    return 'published'


def recover(root, job, pending, live):
    """Return None for normal stamp/freshness validation, or resume an actual exchange."""
    try:
        folder = _folder(root, job)
    except FileNotFoundError:
        return None
    intent_path = folder / 'intent'
    if not os.path.lexists(intent_path):
        return None
    intent = _intent(root, folder, job)
    if intent['live'] != live.relative_to(root).as_posix() or intent['pending'] != pending.relative_to(root).as_posix():
        return 'retry'
    slot = folder / 'displaced.md'
    if os.path.lexists(folder / 'archive.json'):
        result = _record(folder / 'archive.json')
        node = slot.lstat()
        if (result['path'] != intent['live'] or result['archivePath'] != slot.relative_to(root).as_posix()
                or result['candidate'] != intent['stamp'] or result['expectedTargetHash'] != intent['base']
                or result['basePath'] != (folder / 'base').relative_to(root).as_posix()
                or not stat.S_ISREG(node.st_mode) or (node.st_dev, node.st_ino) !=
                (result['displaced']['dev'], result['displaced']['ino']) or os.path.lexists(pending)):
            return 'retry'
        return 'published'
    if not os.path.lexists(slot):
        return None if os.path.lexists(pending) else 'retry'
    material = _snapshot(slot)
    if material == intent['stamp']:
        # Re-enter the verifier on its original name and recheck source, stamp and live base.
        _move(slot, pending)
        return None
    if _record(folder / 'ready') != intent or os.path.lexists(pending):
        return 'retry'
    # A prepared work directory alone cannot prove publication if both named inodes changed.
    visible = live.lstat()
    if (visible.st_dev, visible.st_ino) != (intent['stamp']['dev'], intent['stamp']['ino']):
        return 'retry'
    return _finish(root, folder, intent)


def publish(root, job, pending, live, verified, base):
    """Called under the card/metadata locks after verifying the stamp and archiving the base."""
    folder = _folder(root, job, create=True)
    expected = hashlib.sha256(base).hexdigest()
    if job.get('pendingPath') is not None and job.get('expectedTargetHash') != expected:
        return 'stale'
    snapshot = folder / 'base'
    if not os.path.lexists(snapshot):
        write_new_file(snapshot, base)
    if _snapshot(snapshot)['sha256'] != expected:
        return 'retry'
    stamp = _snapshot(pending, sync=True)
    if stamp['sha256'] != hashlib.sha256(verified.encode()).hexdigest():
        return 'retry'
    intent = {'live': live.relative_to(root).as_posix(), 'pending': pending.relative_to(root).as_posix(),
              'base': expected, 'stamp': stamp}
    _record(folder / 'intent', intent)
    slot = folder / 'displaced.md'
    _move(pending, slot)
    if _snapshot(slot) != stamp:
        return 'retry'
    _record(folder / 'ready', intent)
    if _snapshot(live)['sha256'] != expected:
        _move(slot, pending)
        return 'stale'
    try:
        _move(slot, live, exchange=True)
    except (OSError, ValueError):
        if _snapshot(slot) == stamp:
            _move(slot, pending)
        raise
    return _finish(root, folder, intent)


def _source_bytes(path, expected_text):
    if path.resolve() != path:
        raise ValueError('unsafe visible card')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('unsafe visible card')
        raw = stream.read()
    if raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n') != expected_text:
        raise ValueError('visible card changed')
    return raw


def _stamp_owner(path, owner, expected, rendered):
    if owner is not None:
        return owner
    root = next((wiki.parent.parent for wiki in path.parents if wiki.name == 'wiki'
                 and wiki.parent.name in ('.memvane', '.auto-context')), None)
    if root is None:
        config = find_project_config(path.parent)
        root = Path(config['projectRoot']) if config.get('configPath') else path.parent
    # A repeated direct call may render a new timestamp for the same verdict.
    fields = [line for line in rendered.splitlines() if not line.startswith('verifiedAt:')]
    return root, {'targetPath': path.relative_to(root).as_posix(),
                  'sourceHash': hashlib.sha256(expected.encode()).hexdigest(),
                  'visibleVerdict': hashlib.sha256('\n'.join(fields).encode()).hexdigest()}


def write_stamp(path, rendered, expected, owner, fallback):
    """A visible card is always exchanged; the hidden review image is not a visible card."""
    path = Path(path).absolute()
    if re.fullmatch(r'\.[^.].*\.pending-[a-f0-9]{16}', path.name):
        return fallback(path, rendered)
    try:
        root, job = _stamp_owner(path, owner, expected, rendered)
        base = _source_bytes(path, expected)
        folder = _folder(root, job, create=True)
        review = folder / 'review.md'
        previous = recover(root, job, review, path)
        if previous is not None:
            return previous == 'published'
        if not os.path.lexists(review):
            write_new_file(review, rendered.encode(), mode=stat.S_IMODE(path.stat().st_mode))
        if os.path.lexists(folder / 'intent'):
            intent = _intent(root, folder, job)
            if _snapshot(review) != intent['stamp'] or hashlib.sha256(base).hexdigest() != intent['base']:
                return False
            rendered = review.read_bytes().decode('utf-8')
        elif review.read_bytes() != rendered.encode():
            return False
        return publish(root, job, review, path, rendered, base) == 'published'
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False


def resume_stamp(root, job, live, fresh):
    try:
        folder = _folder(root, job)
    except FileNotFoundError:
        return None
    if not os.path.lexists(folder / 'intent'):
        return None
    review = folder / 'review.md'
    state = recover(root, job, review, live)
    if state is not None:
        return state
    intent = _intent(root, folder, job)
    if not fresh() or _snapshot(review) != intent['stamp'] or _snapshot(live)['sha256'] != intent['base']:
        return 'retry'
    return publish(root, job, review, live, review.read_bytes().decode(), (folder / 'base').read_bytes())


def retire(root, job, live, expected, verdict, manifest_row):
    """Detach the visible file without unlinking an editor's inode or a later generation."""
    folder = _folder(root, dict(job, visibleVerdict='delete-' + verdict), create=True)
    slot = folder / 'displaced.md'
    intent_file = folder / 'retire'
    if not os.path.lexists(intent_file):
        raw = _source_bytes(live, expected)
        node = _snapshot(live)
        if node['sha256'] != hashlib.sha256(raw).hexdigest():
            return False
        _record(intent_file, {'path': job['targetPath'], 'reviewed': node, 'manifest': manifest_row})
    intent = _record(intent_file)
    if intent['path'] != live.relative_to(root).as_posix():
        return False
    if not os.path.lexists(slot):
        if _snapshot(live) != intent['reviewed']:
            return False
        _move(live, slot)
    captured = _snapshot(slot, sync=True)
    if captured != intent['reviewed']:
        # Leave an independently recreated live card alone; keep both names if necessary.
        if not os.path.lexists(live):
            _move(slot, live)
        else:
            _record(folder / 'conflict.json', {'path': intent['path'], 'archivePath': slot.relative_to(root).as_posix(),
                                               'displaced': captured, 'verdict': verdict})
        return False
    fsync_dir(folder)
    fsync_dir(live.parent)
    _record(folder / 'archive.json', {'path': intent['path'], 'archivePath': slot.relative_to(root).as_posix(),
                                      'displaced': captured, 'verdict': verdict})
    return not os.path.lexists(live)


def retirement_status(root, job, *, manifest_has=None):
    """An in-flight visible retirement cannot be settled from an absent ordinary card."""
    for verdict in ('fail', 'inconclusive'):
        try:
            folder = _folder(root, dict(job, visibleVerdict='delete-' + verdict))
            intent = _record(folder / 'retire')
        except FileNotFoundError:
            continue
        if intent['path'] != job['targetPath']:
            return 'retry'
        if not os.path.lexists(folder / 'archive.json'):
            return 'retry'
        archived = _record(folder / 'archive.json')
        node = (folder / 'displaced.md').lstat()
        if (archived['path'] != intent['path'] or archived['verdict'] != verdict
                or archived['archivePath'] != (folder / 'displaced.md').relative_to(root).as_posix()
                or intent['reviewed'] != archived['displaced']
                or not stat.S_ISREG(node.st_mode) or (node.st_dev, node.st_ino) !=
                (archived['displaced']['dev'], archived['displaced']['ino'])):
            return 'retry'
        if os.path.lexists(folder / 'finished'):
            return 'retired' if _record(folder / 'finished') == archived else 'retry'
        row = intent.get('manifest')
        if (manifest_has and isinstance(row, dict) and row.get('targetPath') == intent['path']
                and row.get('verdict') == verdict and manifest_has(row)):
            return verdict  # Durable machine-delete effect; re-offer the interrupted sync.
        return 'retry'
    return None


def finish_retirement(root, job, verdict):
    folder = _folder(root, dict(job, visibleVerdict='delete-' + verdict))
    _record(folder / 'finished', _record(folder / 'archive.json'))
