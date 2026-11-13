"""Which install a wake target names: a selected route or a registered initial build.

Read-only. ``app.runtime_dispatch.resolve`` asks before it runs a child; an
initial-build writer and a preview proof recheck the same answer in-process.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from memvane.journal.types import DispatchTargetError


def dispatch_target(project, *, build):
    """Stable identity and progress of ``project``'s target, or ``{'terminal': True}``.

    ``build`` admits a registered initial build behind a not yet selected
    (``legacy``) route and names the sync worker as the entry; otherwise the
    selected wiki worker. Raises ``DispatchTargetError`` with a fixed code.
    """
    project = _project(project)
    from memvane.journal.api import select_route
    route = select_route(str(project), action='update')
    if route.get('state') == 'candidate':
        from memvane.journal.api import verify_dispatch_target
        try:
            proof = verify_dispatch_target(route)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchTargetError('target_journal_changed') from exc
        stable = {key: proof[key] for key in ('installId', 'scopeId',
                                             'storeIncarnation', 'journalPath')}
        stable.update(storePath=route['storePath'])
        progress = {'selectionEpoch': proof['selectionEpoch'],
                    'generation': proof.get('headGeneration')}
    elif build and route.get('state') == 'legacy':
        from memvane.journal.initial_build import lookup
        try:
            record = lookup(project)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchTargetError('target_journal_changed') from exc
        if record is None:
            raise DispatchTargetError('target_unavailable')
        identity = record['identity']
        if record['phase'] in ('bound', 'retired'):
            return {'terminal': True}
        from memvane.journal.api import inspect_journal
        try:
            journal = inspect_journal(identity['journalPath'])
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchTargetError('target_journal_changed') from exc
        if record.get('schema') == 'memvane-initial-build-v1':
            from memvane.journal.api import _api_attestation as _attestation, _api_route as _route, _api_v1_provider_manifest as _v1_provider_manifest
            from memvane.route.api import classify_v4 as _classify_v4, read_regular as _read_regular
            from memvane.route.types import MAX_RECORD_BYTES
            import hashlib
            from memvane.settings import api as project_dir
            try:
                state = Path(identity['stateRoot'])
                raw = _read_regular(state / 'route.json', MAX_RECORD_BYTES)
                route_record, _ = _route(raw, Path(identity['journalPath']))
                candidate = _classify_v4(project, project_dir.settings_path(project),
                                         route_record)
                attestation, digest = _attestation(Path(identity['journalPath']))
                _v1_provider_manifest(attestation, journal['vectorProfile'])
                if (candidate.get('state') != 'candidate'
                        or hashlib.sha256(raw).hexdigest() != identity['routeSha256']
                        or journal['state'] != 'prepared'
                        or journal['routeSha256'] != identity['routeSha256']
                        or digest != journal['attestationSha256']
                        or digest != identity['attestationSha256']
                        or candidate['vectorProfile'] != journal['vectorProfile']
                        or any(journal[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))
                        or any(candidate[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    raise ValueError('initial_build_identity_changed')
                proof = {'stateFormat': journal['stateFormat'], 'writerFloor': journal['writerFloor']}
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                raise DispatchTargetError('target_journal_changed') from exc
        else:
            raise DispatchTargetError('target_journal_changed')
        stable = {'installId': identity['installId'], 'scopeId': identity['scopeId'],
                  'storeIncarnation': identity['storeIncarnation'],
                  'storePath': identity['storePath'], 'journalPath': identity['journalPath']}
        progress = {'selectionEpoch': journal['epoch'],
                    'generation': record.get('sealed', {}).get('generation')
                    if isinstance(record.get('sealed'), dict) else None}
    else:
        raise DispatchTargetError('target_unavailable')
    from memvane.kernel.memvane_version import same_major, writer_floor_allows
    try:
        matching_major = same_major(proof.get('stateFormat'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DispatchTargetError('target_journal_changed') from exc
    if not matching_major:
        raise DispatchTargetError('state_format_mismatch')
    try:
        writable = writer_floor_allows(proof.get('writerFloor'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DispatchTargetError('target_journal_changed') from exc
    if not writable:
        raise DispatchTargetError('writer_floor')
    if any(not isinstance(value, str) or not value for value in stable.values()):
        raise DispatchTargetError('target_identity_invalid')
    _check_entries(build)
    return {'terminal': False, **stable, **progress}


def _project(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or str(path.resolve(strict=False)) != str(path):
        raise DispatchTargetError('target_invalid')
    try:
        info = path.stat()
    except OSError as exc:
        raise DispatchTargetError('target_unavailable') from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DispatchTargetError('target_invalid')
    return path


def _check_entries(build):
    from memvane.kernel.bundle_paths import entry_path, plugin_root
    worker = plugin_root() / ('memvane/sync/product_sync_worker.py' if build
                              else 'memvane/wiki/compile/selected_execution.py')
    for entry in (entry_path(), worker):
        try:
            info = entry.lstat()
        except OSError as exc:
            raise DispatchTargetError('target_entry_unavailable') from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise DispatchTargetError('target_entry_invalid')
