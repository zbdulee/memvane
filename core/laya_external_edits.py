"""Bounded external edit hints; only the receiving owner authorizes work.

The hook reads installation metadata and one edited text file, never another
project's index/claim store. The owner re-proves its full write route and the
current revision before passing a hint to the existing compile queue.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time
import uuid

MAX_INBOX_EDITS = 64
MAX_SEEN = 256
MAX_INSTALLS = 128
MAX_STATE_ENTRIES = 512
MAX_FILE_BYTES = 4 * 1024 * 1024
HOOK_SECONDS = 2.0
OWNER_SECONDS = 3.0
SCHEMA = 'memvane-external-edits-v1'
HOSTS = frozenset(('claude', 'codex', 'hermes'))
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX40 = re.compile(r'[0-9a-f]{40}\Z')


def _state_bases():
    import memvane_paths
    return memvane_paths.state_base(), memvane_paths.legacy_state_base()


def _inbox_path(decision):
    from laya_wiki_discovery import _paths
    scope, incarnation = decision.get('scopeId'), decision.get('dbIncarnation')
    if (not isinstance(scope, str) or not _HEX32.fullmatch(scope) or
            not isinstance(incarnation, str) or not _HEX32.fullmatch(incarnation)):
        raise ValueError('external_edit_binding_invalid')
    base, _, _ = _paths(decision)
    return base.parent / f'external-edits-{scope}-{incarnation}.json'


def _binding(decision):
    return {'schema': SCHEMA, 'projectRoot': decision['projectRoot'],
            'scopeId': decision['scopeId'], 'dbIncarnation': decision['dbIncarnation']}


def _valid_row(row):
    if not isinstance(row, dict) or set(row) != {'path', 'blob', 'host'}:
        return False
    path, blob = row['path'], row['blob']
    return (isinstance(path, str) and 0 < len(path) <= 4096 and '\\' not in path
            and '\x00' not in path and not Path(path).is_absolute()
            and bool(Path(path).parts)
            and all(part not in ('.', '..') for part in Path(path).parts)
            and Path(path).as_posix() == path and row['host'] in HOSTS
            and isinstance(blob, str) and (blob == 'deleted' or _HEX40.fullmatch(blob)))


def _read_box(path, decision):
    from laya_wiki_discovery import _read
    value = _read(path)
    if value is None:
        return {**_binding(decision), 'edits': [], 'seen': [], 'tokens': {}}
    if (not isinstance(value, dict) or set(value) != {*_binding(decision), 'edits', 'seen', 'tokens'}
            or any(value.get(k) != v for k, v in _binding(decision).items())
            or not isinstance(value['edits'], list) or len(value['edits']) > MAX_INBOX_EDITS
            or not all(_valid_row(row) for row in value['edits'])
            or not isinstance(value['seen'], list) or len(value['seen']) > MAX_SEEN
            or not all(_valid_row(row) for row in value['seen'])
            or not isinstance(value['tokens'], dict)
            or set(value['tokens']) != {row['path'] for row in value['edits']}
            or len(value['tokens']) != len(value['edits'])
            or not all(isinstance(token, str) and _HEX32.fullmatch(token)
                       for token in value['tokens'].values())):
        raise ValueError('external_edit_inbox_invalid')
    return value


def _key(row):
    return row['path'], row['blob']


def owner_for(path, targets):
    """One longest component prefix; an equal-length ambiguity has no owner."""
    matches = []
    for target in targets:
        root = Path(target['rawRoot'])
        try:
            Path(path).relative_to(root)
        except ValueError:
            continue
        matches.append((len(root.parts), target))
    if not matches:
        return None
    longest = max(size for size, _ in matches)
    winners = [target for size, target in matches if size == longest]
    return winners[0] if len(winners) == 1 else None


def _safe_path(path, cwd):
    path = Path(path)
    path = Path(os.path.abspath(path if path.is_absolute() else Path(cwd) / path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        return None
    return path


def _eligible(path, decision):
    from laya_source_exclusion import excluded_source
    from source_text import DENIED_SOURCE_SEGMENTS, is_source_text
    try:
        rel = path.relative_to(decision['projectRoot'])
    except ValueError:
        return False
    project = Path(decision['projectRoot'])
    for parent in (path.parent, *path.parent.parents):
        if (parent / '.git').is_file():
            return False
        if parent == project:
            break
    return (is_source_text(path.name) and bool(rel.parts)
            and not any(part.startswith('.') or part in DENIED_SOURCE_SEGMENTS
                        for part in rel.parts)
            and not excluded_source(rel.as_posix(),
                                    decision.get('recallSettings', {}).get('skipPaths', [])))


def _revision(path):
    """One bounded stable read; no body is retained or put into a hint."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return 'deleted', None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError('external_edit_source_unsafe')
        chunks, count = [], 0
        while True:
            block = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - count))
            if not block:
                break
            chunks.append(block)
            count += len(block)
            if count > MAX_FILE_BYTES:
                raise ValueError('external_edit_source_too_large')
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError('external_edit_source_changed')
    finally:
        os.close(fd)
    data = b''.join(chunks)
    return (hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
            hashlib.sha256(data).hexdigest())


def _targets(origin, deadline):
    import laya_index_host_route as routes
    import laya_install_journal as journal
    targets, seen, scanned = [], set(), 0
    for base in _state_bases():
        try:
            entries = []
            with os.scandir(base) as rows:
                for row in rows:
                    if len(entries) >= MAX_STATE_ENTRIES:
                        raise ValueError('external_edit_discovery_incomplete')
                    entries.append(Path(row.path))
            entries.sort()
        except FileNotFoundError:
            continue
        for state in entries:
            if time.monotonic() >= deadline:
                raise ValueError('external_edit_discovery_incomplete')
            if not _HEX32.fullmatch(state.name):
                continue
            if scanned >= MAX_INSTALLS:
                raise ValueError('external_edit_discovery_incomplete')
            scanned += 1
            try:
                info = state.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    continue
                pointer = state / 'install-journal.sqlite'
                metadata = journal.inspect_journal(pointer)
                project = metadata.get('projectRoot')
                if (metadata.get('state') not in ('active', 'restored_old')
                        or project == origin or project in seen or not isinstance(project, str)):
                    continue
                decision = routes.select_route(project, action='index')
                if (decision.get('state') != 'candidate' or decision.get('projectRoot') != project
                        or decision.get('journalPath') != str(pointer)):
                    continue
                seen.add(project)
                for collection in decision.get('collections', []):
                    if collection.get('role') == 'raw':
                        targets.append({'projectRoot': project, 'rawRoot': collection['sourceRoot'],
                                        'decision': decision})
            except (OSError, ValueError, RuntimeError):
                continue
    return targets


def notify(paths, *, cwd, host, origin):
    """Offer inert revision hints and normal sync wakes, never owner store work."""
    import laya_install_journal as journal
    import runtime_work
    from laya_wiki_discovery import _locked, _write
    deadline = time.monotonic() + HOOK_SECONDS
    try:
        targets = _targets(origin, deadline)
    except (OSError, ValueError, RuntimeError):
        return {'owners': 0, 'failed': 1, 'overflow': 0}
    notified, failed, overflow = set(), 0, 0
    if host not in HOSTS:
        return {'owners': 0, 'failed': 0, 'overflow': 0}
    # The inbox cap counts eligible pending sources, not preceding non-sources
    # in a mixed patch. Payload size and the deadline bound input traversal.
    for raw in paths:
        if time.monotonic() >= deadline:
            break
        owner, proved = None, False
        try:
            path = _safe_path(raw, cwd)
            owner = owner_for(path, targets) if path is not None else None
            if owner is None or not _eligible(path, owner['decision']):
                continue
            decision = owner['decision']
            proof = journal.verify_notification_target(decision)
            if proof.get('notificationOnly') is not True:
                raise ValueError('external_edit_notification_unproved')
            proved = True
            blob, _ = _revision(path)
            row = {'path': path.relative_to(owner['projectRoot']).as_posix(),
                   'blob': blob, 'host': host}
            inbox = _inbox_path(decision)
            if journal.verify_notification_target(decision) != proof:
                raise ValueError('external_edit_owner_changed')
            with _locked(inbox.with_suffix('.lock'), .15):
                box = _read_box(inbox, decision)
                pending = [r for r in box['edits'] if r['path'] == row['path']]
                prior = pending or [r for r in box['seen'] if r['path'] == row['path']]
                if not prior or _key(prior[-1]) != _key(row):
                    if not pending and len(box['edits']) >= MAX_INBOX_EDITS:
                        overflow += 1
                    else:
                        box['edits'] = [r for r in box['edits'] if r['path'] != row['path']]
                        box['edits'].append(row)
                        box['tokens'][row['path']] = uuid.uuid4().hex
                        _write(inbox, box)
            if owner['projectRoot'] not in notified:
                result = runtime_work.request('laya-sync', owner['projectRoot'])
                if not isinstance(result, dict) or result.get('status') not in ('queued', 'busy'):
                    raise ValueError('external_edit_wake_unavailable')
                notified.add(owner['projectRoot'])
        except Exception:
            # An unavailable recipient never denies A's completed edit or its
            # own sync. No hint grants authority without the owner's proof.
            failed += 1
            if proved and owner is not None and owner['projectRoot'] not in notified:
                try:
                    if journal.verify_notification_target(owner['decision']) != proof:
                        raise ValueError('external_edit_owner_changed')
                    result = runtime_work.request('laya-sync', owner['projectRoot'])
                    if isinstance(result, dict) and result.get('status') in ('queued', 'busy'):
                        notified.add(owner['projectRoot'])
                except Exception:
                    pass
    return {'owners': len(notified), 'failed': failed, 'overflow': overflow}


def drain(decision, *, recheck):
    """Full-proof owner consumes current hints using existing compile policy."""
    import config
    import laya_index_host_route as routes
    import laya_install_journal as journal
    import wiki_compile_enqueue as enqueue
    import wiki_selected_laya
    from laya_wiki_discovery import _locked, _write
    if not isinstance(decision.get('collections'), list):
        return {'remaining': 0, 'processed': 0}
    inbox = _inbox_path(decision)
    if not os.path.lexists(inbox):
        return {'remaining': 0, 'processed': 0}
    proof = journal.verify_selected_route_for_write(decision, 'compile')
    if proof.get('productRouteAllowed') is not True:
        raise ValueError('external_edit_owner_unproved')

    def checked():
        recheck()
        if journal.verify_selected_route_for_write(decision, 'compile') != proof:
            raise ValueError('external_edit_owner_changed')

    def readable_revision(path):
        try:
            return _revision(path)
        except (OSError, ValueError):
            # A source that becomes unreadable or exceeds the file budget is
            # stale input. Drop its hint without blocking other sources or
            # discovery. Route/proof failures still escape for a retry.
            return None, None

    checked()
    project = Path(decision['projectRoot'])
    found = config.find_project_config(str(project))
    if found['projectRoot'] != str(project):
        raise ValueError('external_edit_settings_changed')
    cfg = found['config']
    permitted = (config.event_enabled(cfg, 'postToolUse') and
                 enqueue.compile_gate(cfg, (enqueue.POST_TOOL_TRIGGER,)) is not None)
    processed, queued, spent = {}, False, 0
    accepted = []
    deleted = False
    deadline = time.monotonic() + OWNER_SECONDS
    # Owners serialize separately. Hooks hold the inbox lock only for a short
    # metadata replace; hashing and queue work never block incoming hints.
    with _locked(inbox.with_suffix('.owner.lock'), 1.0):
        with _locked(inbox.with_suffix('.lock'), 1.0):
            box = _read_box(inbox, decision)
        for row in box['edits']:
            if spent >= enqueue.compile_max_files() or time.monotonic() >= deadline:
                break
            path = _safe_path(row['path'], project)
            current = routes.select_route(str(project), action='compile')
            checked()
            revision = None
            if path is not None and _eligible(path, current):
                blob, revision = readable_revision(path)
            else:
                blob = None
            current_delete = blob == row['blob'] == 'deleted'
            deleted = deleted or current_delete
            consumed = current_delete
            if permitted and blob == row['blob'] and blob != 'deleted':
                record = enqueue._source_record(str(path), str(project), str(project), cfg,
                                                row['host'])
                if record is not None:
                    # Metadata/proof checks may outlive the first file read.
                    # Bind the queue row to bytes still current at enqueue.
                    now_blob, revision = readable_revision(path)
                    if now_blob == row['blob']:
                        record['contentRevision'] = revision
                        queue = enqueue._safe_queue_path(str(project))
                        if queue is None:
                            raise ValueError('external_edit_compile_queue_unsafe')
                        checked()
                        result = enqueue._append_unqueued(queue, [
                            (record['source']['collection'], record['source']['path'], record)], 1)
                        consumed = bool(result['records']) or result['result'].get('wakePending', False)
                        queued = queued or consumed
                        spent += 1
            processed[row['path']] = box['tokens'][row['path']]
            if consumed:
                accepted.append(row)
        if queued:
            checked()
            wiki_selected_laya.request_compile(project, (decision, proof))
        if deleted:
            import wiki_lifecycle_request
            checked()
            wiki_lifecycle_request.request(decision, 'source_edit')
        checked()
        with _locked(inbox.with_suffix('.lock'), 1.0):
            latest = _read_box(inbox, decision)
            for path, token in processed.items():
                if latest['tokens'].get(path) == token:
                    latest['edits'] = [r for r in latest['edits'] if r['path'] != path]
                    del latest['tokens'][path]
            for row in accepted:
                latest['seen'] = [r for r in latest['seen'] if r['path'] != row['path']]
                latest['seen'].append(row)
            latest['seen'] = latest['seen'][-MAX_SEEN:]
            checked()
            _write(inbox, latest)
    return {'remaining': len(latest['edits']), 'processed': len(processed)}
