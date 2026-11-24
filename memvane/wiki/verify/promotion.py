"""Exchange a verified candidate with the visible card, retaining the actual displaced inode."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, rename_exchange, rename_noreplace, write_new_file
from memvane.kernel.persistent_identity import persistent_identity_equal
from memvane.settings.api import compile_dir, find_project_config, ledger
from memvane.wiki.card.types import AUTO_START_RE
from memvane.wiki.workspace.types import MANIFEST, VERIFY_DELETED, MACHINE_DELETE_ACTION
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
    # The time, marker, selected route and exact source revision distinguish generations.
    binding = {key: job[key] for key in ('targetPath', 'pendingPath', 'sourceHash', 'expectedTargetHash',
               'sourceRevisions', 'sourceFingerprints', 'selectedRoute', 'ts', 'visibleVerdict') if key in job}
    identity = hashlib.sha256(canonical_json_bytes(binding)).hexdigest()
    return _archive_folder(root, identity, create)


def _archive_folder(root, identity, create=False):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
        raise ValueError('invalid retirement owner')
    canonical = root / CURRENT / 'compile'
    if canonical.resolve() != canonical:
        raise ValueError('unsafe promotion archive')
    base = compile_dir(root, create=create)
    if base is None or base != canonical:
        raise ValueError('unsafe promotion archive')
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


def _record(path, value=None, *, max_bytes=16384):
    if value is not None and not os.path.lexists(path):
        payload = dict(value, version=1)
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        write_new_file(path, canonical_json_bytes(dict(payload, checksum=digest)))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > max_bytes or info.st_nlink != 1:
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
    receipt = folder / 'stamp-decision'
    if os.path.lexists(receipt):
        decision = _stamp_decision(receipt, folder / 'base')
        base = decision.get('base')
        if decision.get('job') != _stamp_key(job) or not isinstance(base, dict) or base.get('sha256') != data['base']:
            raise ValueError('stamp decision belongs to another job or base')
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


def _source_bytes(path, expected_text, *, with_node=False):
    if path.resolve() != path:
        raise ValueError('unsafe visible card')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('unsafe visible card')
        raw = stream.read()
    if raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n') != expected_text:
        raise ValueError('visible card changed')
    if with_node:
        return raw, {'dev': info.st_dev, 'ino': info.st_ino, 'sha256': hashlib.sha256(raw).hexdigest()}
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


def _stamp_key(job):
    original = {k: v for k, v in job.items() if k not in ('_pendingCleanup', '_requeue_count')}
    return hashlib.sha256(canonical_json_bytes(original)).hexdigest()


def _stamp_decision(receipt, base, value=None):
    from memvane.store.types import MAX_BODY
    # JSON escaping may expand each byte sixfold, plus the new verification fields.
    return _record(receipt, value, max_bytes=6 * base.stat().st_size + MAX_BODY)


def _publish_decision(root, job, live, folder, *, rendered=None, expected_base=None):
    """Bind the exact paid output to the job and the reviewed inode before staging it."""
    receipt = folder / 'stamp-decision'
    review = folder / 'review.md'
    node = _snapshot(live)
    if expected_base is not None and expected_base != node:
        return 'retry'
    key = _stamp_key(job)
    if not os.path.lexists(receipt):
        if rendered is None:
            return None
        # A pre-receipt image has no job/base proof. Keep it private for inspection.
        if os.path.lexists(review):
            _move(review, folder / 'unbound-review.md')
        saved = _stamp_decision(receipt, live, {'job': key, 'base': node, 'rendered': rendered})
    else:
        saved = _stamp_decision(receipt, live)
    if (saved.get('job') != key or not persistent_identity_equal(saved.get('base'), node)
            or not isinstance(saved.get('rendered'), str)):
        return 'retry'
    base = live.read_bytes()
    if hashlib.sha256(base).hexdigest() != node['sha256']:
        return 'retry'
    image = saved['rendered'].encode()
    if not os.path.lexists(review):
        write_new_file(review, image, mode=stat.S_IMODE(live.stat().st_mode))
    if _snapshot(review)['sha256'] != hashlib.sha256(image).hexdigest():
        return 'retry'
    return publish(root, job, review, live, saved['rendered'], base)


def write_stamp(path, rendered, expected, owner, fallback):
    """A visible card is always exchanged; the hidden review image is not a visible card."""
    path = Path(path).absolute()
    if re.fullmatch(r'\.[^.].*\.pending-[a-f0-9]{16}', path.name):
        return fallback(path, rendered)
    try:
        root, job = _stamp_owner(path, owner, expected, rendered)
        base, reviewed = _source_bytes(path, expected, with_node=True)
        folder = _folder(root, job, create=True)
        review = folder / 'review.md'
        previous = recover(root, job, review, path)
        if previous is not None:
            return previous == 'published'
        if not os.path.lexists(folder / 'intent'):
            return _publish_decision(root, job, path, folder, rendered=rendered, expected_base=reviewed) == 'published'
        intent = _intent(root, folder, job)
        if _snapshot(review) != intent['stamp'] or hashlib.sha256(base).hexdigest() != intent['base']:
            return False
        return publish(root, job, review, path, review.read_bytes().decode(), base) == 'published'
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False


def resume_stamp(root, job, live, fresh):
    try:
        folder = _folder(root, job)
    except FileNotFoundError:
        return None
    if not os.path.lexists(folder / 'intent'):
        if not os.path.lexists(folder / 'stamp-decision'):
            return None
        return _publish_decision(root, job, live, folder) if fresh() else 'retry'
    review = folder / 'review.md'
    state = recover(root, job, review, live)
    if state is not None:
        return state
    intent = _intent(root, folder, job)
    if not fresh() or _snapshot(review) != intent['stamp'] or _snapshot(live)['sha256'] != intent['base']:
        return 'retry'
    return publish(root, job, review, live, review.read_bytes().decode(), (folder / 'base').read_bytes())


def _target_ledger(root, name, target, *, action=None):
    """Physical LF rows. Bind both the latest record and its occurrence at this target."""
    path = ledger(root, name)
    if path is None or path != root / CURRENT / 'compile' / name:
        raise ValueError('unsafe retirement ledger')
    count, last = 0, None
    try:
        stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
    except FileNotFoundError:
        return count, last
    with stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('unsafe retirement ledger')
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not line.endswith(b'\n'):
                raise ValueError('invalid retirement ledger')
            if row.get('targetPath') != target or (action is not None and row.get('action') != action):
                continue
            if name == VERIFY_DELETED and row.get('verdict') not in ('fail', 'inconclusive'):
                continue
            count, last = count + 1, row
    return count, last


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest_generation(history):
    count, last = history
    return {'count': count, 'latest': _digest(last)}


def _same_generation(before, history):
    # Compaction can remove preceding rows. A new creation after the same
    # last row still advances the count, including byte-identical creations.
    current = _manifest_generation(history)
    return current['latest'] == before['latest'] and current['count'] <= before['count']


def _time(value):
    if not isinstance(value, str):
        raise ValueError('invalid deletion time')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('invalid deletion time')
    return dt


def _created_after_audit(root, target, latest, audit):
    if not latest or latest.get('action') != 'created' or not audit.get('deletedAt'):
        return False
    deleted_at = _time(audit['deletedAt'])
    if _time(latest['ts']) > deleted_at:
        return True
    # Released creation timestamps have one-second precision. Within that
    # second, a later creation follows its completed deletion in manifest order.
    _, marker = _target_ledger(root, MANIFEST, target, action=MACHINE_DELETE_ACTION)
    return bool(marker and marker.get('verdict') == audit.get('verdict') and
                marker.get('engine') == audit.get('engine') and _time(marker['ts']) >= deleted_at)


def _audit_owner(root, target, audit, create=False):
    return _folder(root, {'targetPath': target, 'visibleVerdict': 'retire-audit-' + _digest(audit)}, create)


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
        target = job['targetPath']
        audit = _target_ledger(root, VERIFY_DELETED, target)
        if not audit[1] or audit[1].get('verdict') != verdict or audit[1].get('engine') != manifest_row['engine']:
            return False
        _record(_audit_owner(root, target, audit, create=True) / 'owner', {'folder': folder.name})
        _record(intent_file, {'path': target, 'reviewed': node, 'manifest': manifest_row,
                              'previous': _manifest_generation(_target_ledger(root, MANIFEST, target)),
                              'audit': _digest(audit)})
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


def _legacy_audit_matches(folder, intent, audit):
    row = intent['manifest']
    return (audit and row.get('targetPath') == audit.get('targetPath') == intent['path']
            and row.get('verdict') == audit.get('verdict') and row.get('engine') == audit.get('engine')
            and _time(row['ts']) <= _time(audit['deletedAt'])
            and _time(audit['deletedAt']).timestamp() <= (folder / 'retire').stat().st_mtime)


def _legacy_generation(root, folder, intent, history, audit):
    # The first native format lacks a manifest predecessor digest. Its unchanged
    # public inode must predate the retirement and identify the actual archived card.
    path = ledger(root, MANIFEST)
    if not _legacy_audit_matches(folder, intent, audit) or path is None or not history[1]:
        return False
    node = path.stat()
    if max(node.st_ctime_ns, node.st_mtime_ns) > (folder / 'retire').stat().st_mtime_ns:
        return False
    marker = AUTO_START_RE.search((folder / 'displaced.md').read_text(encoding='utf-8'))
    return bool(marker and history[1].get('sourceHash') == marker.group(1)
                and history[1].get('action') in ('created', 'updated'))


def _legacy_owner(root, target, audit):
    base = compile_dir(root) / 'retired-wiki' / '.promotions'
    if not base.exists():
        return None
    choices = []
    for entry in base.iterdir():
        if not re.fullmatch('[a-f0-9]{64}', entry.name) or not os.path.lexists(entry / 'retire'):
            continue
        folder = _archive_folder(root, entry.name)
        intent = _record(folder / 'retire')
        if (intent.get('path') == target and 'previous' not in intent
                and _legacy_audit_matches(folder, intent, audit)):
            choices.append(folder)
    return choices[0] if len(choices) == 1 else None


def _retirement_status(root, folder, target, verdict, *, manifest_has, repair, check=False):
    intent = _record(folder / 'retire')
    if intent['path'] != target or not os.path.lexists(folder / 'archive.json'):
        return 'retry'
    archived = _record(folder / 'archive.json')
    slot = folder / 'displaced.md'
    if (archived['path'] != target or archived['verdict'] != verdict
            or archived['archivePath'] != slot.relative_to(root).as_posix()
            or intent['reviewed'] != archived['displaced']):
        return 'retry'
    if os.path.lexists(folder / 'finished'):
        return 'retired' if _record(folder / 'finished') == archived else 'retry'
    row = intent.get('manifest')
    if (not isinstance(row, dict) or set(row) != {'ts', 'targetPath', 'action', 'status', 'verdict', 'engine'}
            or not isinstance(row['ts'], str) or not isinstance(row['engine'], str)
            or verdict not in ('fail', 'inconclusive') or row['targetPath'] != target or row['verdict'] != verdict
            or row.get('action') != MACHINE_DELETE_ACTION or row.get('status') != 'deleted'
            or not persistent_identity_equal(archived['displaced'], _snapshot(slot))):
        return 'retry'
    if manifest_has and manifest_has(row):
        return verdict
    if os.path.lexists(root / target):
        return 'retry'
    history = _target_ledger(root, MANIFEST, target)
    audit = _target_ledger(root, VERIFY_DELETED, target)
    if 'previous' in intent:
        if not _same_generation(intent['previous'], history) or intent['audit'] != _digest(audit):
            return 'retry'
    elif not _legacy_generation(root, folder, intent, history, audit[1]):
        return 'retry'
    if check or (repair and repair(row)):
        return verdict
    return 'retry'


def retirement_status(root, job, *, manifest_has=None, repair=None):
    for verdict in ('fail', 'inconclusive'):
        try:
            folder = _folder(root, dict(job, visibleVerdict='delete-' + verdict))
        except FileNotFoundError:
            continue
        if os.path.lexists(folder / 'retire'):
            return _retirement_status(root, folder, job['targetPath'], verdict,
                                      manifest_has=manifest_has, repair=repair)
    return None


def missing_retirement(root, target, *, check, manifest_has, repair, sync):
    """The caller holds the card and mutation locks before interpreting a missing card."""
    audit = _target_ledger(root, VERIFY_DELETED, target)
    if not audit[1]:
        return 'manual'
    try:
        owner = _record(_audit_owner(root, target, audit) / 'owner')
    except FileNotFoundError:
        _, last = _target_ledger(root, MANIFEST, target)
        if _created_after_audit(root, target, last, audit[1]):
            return 'manual'
        folder = _legacy_owner(root, target, audit[1])
        if folder is None:
            return 'retry'  # An audit alone precedes unlink and cannot prove who removed the card.
    else:
        folder = _archive_folder(root, owner['folder'])
    verdict = audit[1]['verdict']
    intent = _record(folder / 'retire')
    if 'audit' in intent and intent['audit'] != _digest(audit):
        return 'retry'
    history = _target_ledger(root, MANIFEST, target)
    if 'previous' in intent and not _same_generation(intent['previous'], history):
        status = _retirement_status(root, folder, target, verdict, manifest_has=manifest_has, repair=None)
        if (history[1] and history[1].get('action') == 'created' and
                (status in ('retired', verdict) or _created_after_audit(root, target, history[1], audit[1]))):
            return 'manual'
        return 'retry'
    if ('previous' not in intent and history[1] and history[1].get('action') == 'created'
            and manifest_has(intent['manifest'])):
        # The recorded deletion already precedes this creation. The writer must
        # preserve a manual removal even if the old verifier did not mark completion.
        status = _retirement_status(root, folder, target, verdict, manifest_has=manifest_has, repair=None)
        return 'manual' if status in (verdict, 'retired') else 'retry'
    status = _retirement_status(root, folder, target, verdict, manifest_has=manifest_has, repair=repair, check=check)
    if status == 'retired':
        return 'retry'
    if status != verdict:
        return 'retry'
    if not check:
        sync()
        _record(folder / 'finished', _record(folder / 'archive.json'))
    return 'machine'


def finish_retirement(root, job, verdict):
    folder = _folder(root, dict(job, visibleVerdict='delete-' + verdict))
    _record(folder / 'finished', _record(folder / 'archive.json'))
