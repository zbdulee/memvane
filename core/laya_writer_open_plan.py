"""Metadata-only preparation for a selected v5 writer's first content proof.

A plan describes existing canonical files and sealed coordination locks. It
grants no SQLite open, writer, reader, mover, cohort or immutable capability.
The caller must acquire lifetime/physical windows and prove the full current
installation before issuing normal writer authority.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading
import weakref

import laya_install_journal as J
import laya_journal_reader as R
import laya_reader_lease as L
import laya_reader_windows as W

_issued = weakref.WeakKeyDictionary()


@dataclass(frozen=True, eq=False, init=False)
class WriteOpenPlan:
    install_id: str
    queue_path: Path
    plan_id: str

    def __init__(self):
        raise ValueError('writer_plan_not_issued')

    def __reduce__(self):
        raise TypeError('writer_plan_not_serializable')


def _canonical_directory(value):
    path = Path(value)
    R._need(path.is_absolute() and path.resolve(strict=True) == path,
            'writer_plan_path_invalid')
    info = path.lstat()
    R._need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o022, 'writer_plan_path_invalid')
    return path


def _manifest(install_id):
    try:
        parent, fd, info = L._parent(install_id, create=False)
    except FileNotFoundError as exc:
        raise J.JournalEvidenceError('writer_windows_not_prepared') from exc
    try:
        if not os.path.lexists(parent / W._MANIFEST):
            raise J.JournalEvidenceError('writer_windows_not_prepared')
        return W._manifest(install_id, parent, fd, info).decode('utf8')
    finally:
        os.close(fd)


def _snapshot(project, executing_root):
    project = _canonical_directory(project)
    root = _canonical_directory(executing_root)
    # Classification reads installer metadata too. Its legacy journal opener
    # must never repair a hot image while preparing this non-authority plan.
    with J.read_only_journals():
        decision = J.select_route(str(project), action='index')
        R._need(decision.get('state') == 'candidate'
                and decision.get('projectRoot') == str(project), 'writer_route_unavailable')
        path = R._journal(Path(decision['journalPath']))
        state = path.parent
        R._need(path.stat().st_size <= R._MAX_IMAGE, 'writer_plan_journal_too_large')
        with R._readonly(path, state) as db:
            R._need(J._schema_version_read(db) == 5, 'writer_plan_v5_required')
            bundle = J._row(db)
            selection, record = R._selection(db)
        R._need(bundle['state'] in ('active', 'restored_old') and bundle['pending'] is None
                and record['capability'] == 'v6-ro-v1'
                and record['store_format'] == 'v6', 'writer_selection_unavailable')
        selected = bundle['selected']
        R._need(type(selected) is dict and selected['bundleKind'] == 'full_plugin'
                and selected['pluginRoot'] == str(root) == decision['pluginRoot'],
                'writer_bundle_unavailable')
        R._need(record['install_id'] == selected['installId'] == decision['installId']
                and J.ID.fullmatch(record['install_id']) is not None
                and record['scope'] == bundle['scope_id'] == decision['scopeId']
                and record['incarnation'] == decision['dbIncarnation']
                and record['profile'] == decision['vectorProfile']['profileId'],
                'writer_plan_binding_changed')
        R._need(record['project'] == bundle['project_root'] == str(project)
                and record['project_node'] == R._node(project)
                and record['project_node'] == [bundle['project_dev'], bundle['project_ino']]
                and record['state'] == str(state) and record['state_node'] == R._node(state),
                'writer_plan_binding_changed')
        raw = J._observed_route(bundle, None)
        route, digest = J._route(raw, path)
        R._need(digest == bundle['selected_route_sha'], 'writer_plan_route_changed')
        for key in ('installId', 'pluginRoot', 'generation', 'vectorProfile',
                    'bundleManifestSha256', 'ownershipManifestSha256'):
            R._need(route[key] == decision[key] == selected[key], 'writer_plan_route_changed')
        R._need(record['db_path'] == route['dbPath'] == decision['dbPath']
                and route['queuePath'] == decision['queuePath'], 'writer_plan_binding_changed')
        attestation, attestation_sha = J._attestation(path)
        R._need(attestation is not None and attestation_sha == bundle['provider_attestation_sha']
                and attestation_sha == record['attestation_sha256'], 'writer_plan_attestation_changed')
        for key in ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256', 'vectorProfile'):
            R._need(attestation[key] == selected[key], 'writer_plan_attestation_changed')
        components = {}
        for role, prefix in (('base', 'db'), ('catalog', 'catalog'), ('binding', 'binding')):
            location = Path(record[prefix + '_path'])
            node = R._file(location, state)
            R._need(node == record[prefix + '_node'], 'writer_plan_component_changed')
            components[role] = dict(path=str(location), node=node)
        queue = Path(decision['queuePath'])
        components['queue'] = dict(path=str(queue), node=R._file(queue, state))
        from laya_incremental_docs import FILE
        sidecar = Path(record['sidecar_path'])
        R._need(sidecar == state / FILE, 'writer_plan_component_changed')
        components['sidecar'] = dict(path=str(sidecar), node=(
            R._file(sidecar, state) if os.path.lexists(sidecar) else None))
        R._need(len({row['path'] for row in components.values()}) == len(components),
                'writer_plan_component_changed')
        manifest = _manifest(record['install_id'])
        return dict(project=str(project), executing_root=str(root), decision=decision,
                    bundle=bundle, endpoint=record, reader_epoch=selection.epoch,
                    endpoint_id=selection.preferred_endpoint, components=components,
                    manifest=manifest, attestation_sha=attestation_sha)


def capture_writer_plan(project, *, executing_root):
    """Capture existing metadata without opening any content component."""
    snapshot = _snapshot(project, executing_root)
    packed = R._raw(snapshot)
    token = object.__new__(WriteOpenPlan)
    object.__setattr__(token, 'install_id', snapshot['endpoint']['install_id'])
    object.__setattr__(token, 'queue_path', Path(snapshot['components']['queue']['path']))
    object.__setattr__(token, 'plan_id', R._sha(b'memvane-writer-open-plan-v1\0' + packed))
    _issued[token] = (os.getpid(), threading.current_thread(), packed, snapshot)
    return token


def _plan_record(plan):
    R._need(type(plan) is WriteOpenPlan and plan in _issued, 'writer_plan_not_issued')
    pid, thread, packed, snapshot = _issued[plan]
    R._need(pid == os.getpid() and thread is threading.current_thread(), 'writer_plan_expired')
    R._need(R._raw(snapshot) == packed
            and plan.install_id == snapshot['endpoint']['install_id']
            and plan.queue_path == Path(snapshot['components']['queue']['path'])
            and plan.plan_id == R._sha(b'memvane-writer-open-plan-v1\0' + packed),
            'writer_plan_changed')
    return packed, snapshot


def validate_writer_plan(plan):
    """Recheck the same issued description; this does not grant authority."""
    packed, snapshot = _plan_record(plan)
    current = _snapshot(snapshot['project'], snapshot['executing_root'])
    R._need(R._raw(current) == packed, 'writer_plan_changed')
