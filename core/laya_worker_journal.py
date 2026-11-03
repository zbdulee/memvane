"""Selected-worker metadata admission and narrowly owned legacy recovery."""
import os
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import laya_install_journal as J
import laya_journal_reader as R
from laya_index_a import IndexContractError
from laya_index_sync import sync_owner, validate_sync_owner
from resource_deferred import ResourceDeferred

_active = {}


def _probe(path, *, receipt=None, header=False):
    mode = 'header' if header else 'check' if receipt is not None else 'probe'
    result = subprocess.run([sys.executable, '-B', '-I',
        str(Path(__file__).with_name('laya_legacy_journal_probe.py')), mode, str(path)],
        input='' if receipt is None else json.dumps(receipt), encoding='utf8',
        capture_output=True, timeout=6)
    R._need(result.returncode == 0 and len(result.stdout) <= R._MAX_IMAGE,
            'worker_legacy_probe_failed')
    return json.loads(result.stdout)


def _legacy_binding(project, decision, journal, row, digest, route):
    selected = row['selected']
    R._need(row['_version'] in (3, 4) and row['state'] in ('active', 'restored_old')
            and selected is not None and row['selected_route_sha'] == digest
            and J._project(route) == (row['project_root'], row['project_dev'], row['project_ino'])
            and row['project_root'] == str(project) and row['scope_id'] == decision['scopeId']
            and all(selected.get(key) == decision.get(key) for key in
                    ('pluginRoot', 'installId', 'generation', 'bundleManifestSha256',
                     'ownershipManifestSha256', 'vectorProfile'))
            and selected['bundleKind'] == 'full_plugin'
            and row['provider_attestation_sha'] is not None
            and row['provider_file_identity'] is not None, 'worker_journal_binding_changed')
    proof = J._inspect_identity(selected)
    R._need(J._verified_executing_root(selected, proof) == selected['pluginRoot'],
            'worker_journal_binding_changed')
    attestation, digest = J._attestation(journal)
    R._need(attestation is not None and digest == row['provider_attestation_sha']
            and J._provider_file_identity(attestation) == row['provider_file_identity']
            and all(attestation.get(key) == selected.get(key) == proof.get(key) for key in
                ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256', 'vectorProfile')),
            'worker_journal_binding_changed')


def _metadata(project, *, legacy=None):
    import laya_index_host_route as host
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    from laya_writer_open_plan import _canonical_directory
    project = _canonical_directory(project)
    candidates = list(host._candidate_paths(project))
    if len(candidates) != 1 or candidates[0][0] != project:
        raise IndexContractError('selected_route_unavailable')
    root, auto, route_path = candidates[0]
    decision = host._classify_record(root, auto, route_path, project)
    if decision.get('state') != 'candidate':
        raise IndexContractError('selected_route_unavailable')
    journal = J._private_parent(decision['journalPath'])
    state = R._state(journal.parent)
    ledger_path, marker_path, expected_route = fresh._paths(project, journal)
    raw = J._observed_route({'project_root': str(project)}, None)
    route, digest = J._route(raw, journal)
    if not os.path.lexists(marker_path):
        # Only a failed readonly legacy attempt reaches this fallback. Recover
        # a bounded private copy in a fresh process, never source SQLite.
        legacy = _probe(journal) if legacy is None else legacy
        _legacy_binding(project, decision, journal, legacy['row'], digest, route)
        return dict(project=str(project), decision=decision, legacy=legacy,
                    project_node=R._node(project), state_node=R._node(state),
                    journal_node=R._file(journal, state),
                    queue_node=R._file(Path(decision['queuePath']), state), route_sha=digest)
    ledger = fresh._load(ledger_path)
    fresh._check_marker(ledger, marker_path)
    # The ledger/marker digest commits the complete route, including install
    # ID. An existing build record additionally states that ID independently.
    R._need(expected_route == route_path and ledger['phase'] == 'active'
            and ledger['projectRoot'] == str(project)
            and ledger['journalPath'] == str(journal) and ledger['routeSha256'] == digest
            and ledger['scope'] == decision['scopeId']
            and ledger['candidateIncarnation'] == decision['dbIncarnation']
            and ledger['vectorProfileId'] == decision['vectorProfile']['profileId']
            and ledger['candidateDb'] == decision['dbPath']
            and ledger['queueDb'] == decision['queuePath'], 'worker_journal_binding_changed')
    for field, identity in (('candidateDb', 'candidateIdentity'), ('queueDb', 'queueIdentity')):
        R._need(R._file(Path(ledger[field]), state) == ledger[identity], 'worker_journal_binding_changed')
    built = build.read(state)
    if built is not None:
        identity = built['identity']
        for field, expected in (('projectRoot', str(project)), ('stateRoot', str(state)),
                ('installId', decision['installId']), ('journalPath', str(journal)),
                ('queueDb', decision['queuePath']), ('candidateDb', decision['dbPath']),
                ('scopeId', decision['scopeId']), ('dbIncarnation', decision['dbIncarnation'])):
            R._need(identity[field] == expected, 'worker_journal_binding_changed')
    return dict(project=str(project), decision=decision, ledger=ledger,
                project_node=R._node(project), state_node=R._node(state),
                journal_node=R._file(journal, state), build=None if built is None else built['identity'])


class _Access:
    def __init__(self, project):
        self.project = Path(project)
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.started = self.finished = False
        self.owner = self.owner_context = self.token = None

    def _creator(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'worker_journal_owner_changed')

    def __enter__(self):
        self._creator()
        R._need(not self.started and not self.finished, 'worker_journal_reentered')
        self.started = True
        self.snapshot = _metadata(self.project)
        self.packed = R._raw(self.snapshot)
        self.queue = Path(self.snapshot['decision']['queuePath'])
        self.owner_context = sync_owner(self.queue)
        self.owner = self.owner_context.__enter__()
        if self.owner is None:
            self.owner_context.__exit__(None, None, None)
            self.finished = True
            raise ResourceDeferred('owner_busy')
        try:
            R._need(R._raw(_metadata(self.project, legacy=self.snapshot.get('legacy'))) == self.packed,
                    'worker_journal_binding_changed')
            self.deadline = time.monotonic()+2
            self.recovered = False
            if 'legacy' in self.snapshot:
                _probe(Path(self.snapshot['decision']['journalPath']), receipt=self.snapshot['legacy']['receipt'])
            _active[id(self)] = self
            self.token = J._legacy_worker_recovery.set(self)
            return self
        except BaseException:
            self.finished = True
            self.owner_context.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._creator()
        if self.finished or self.owner is None:
            return False
        R._need(J._legacy_worker_recovery.get() is self, 'worker_journal_exit_order')
        J._legacy_worker_recovery.reset(self.token)
        self.finished = True
        _active.pop(id(self), None)
        self.owner_context.__exit__(exc_type, exc, traceback)
        return False

    def check(self, path):
        self._creator()
        R._need(_active.get(id(self)) is self and not self.finished
                and J._legacy_worker_recovery.get() is self
                and Path(path) == Path(self.snapshot['decision']['journalPath']),
                'worker_journal_scope_expired')
        validate_sync_owner(self.owner, self.queue)
        R._need(R._raw(_metadata(self.project, legacy=self.snapshot.get('legacy'))) == self.packed,
                'worker_journal_binding_changed')
        if 'legacy' in self.snapshot and not self.recovered:
            _probe(Path(path), receipt=self.snapshot['legacy']['receipt'])
        validate_sync_owner(self.owner, self.queue)
        R._need(time.monotonic() < self.deadline, 'worker_journal_deadline')


def worker_journal_access(project):
    return _Access(project)


def recover_legacy(scope, path):
    R._need(type(scope) is _Access and _active.get(id(scope)) is scope,
            'worker_journal_scope_expired')
    scope.check(path)
    if scope.recovered:
        return False
    path = Path(path)
    if os.path.lexists(path.parent/J._INITIAL_INTENT):
        return False
    side = Path(str(path)+'-journal')
    if not os.path.lexists(side):
        return False
    try:
        header = _probe(path, header=True)
        R._need(header['receipt'][0]['node'][:2] == scope.snapshot['journal_node'],
                'worker_journal_binding_changed')
    except J.JournalEvidenceError:
        return False
    scope.check(path)
    # Conversion and initial recovery acquire this same queue owner before
    # publishing v5 or creating its intent. No converter can cross this gap.
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2,
                                 isolation_level=None)) as db:
        db.execute('PRAGMA application_id').fetchone()
    scope.recovered = True
    scope.check(path)
    return True
