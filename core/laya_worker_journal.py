"""Selected-worker metadata admission and narrowly owned legacy recovery."""
import os
from pathlib import Path
import threading
import time

import laya_install_journal as J
import laya_journal_reader as R
from laya_index_a import IndexContractError
from laya_index_sync import sync_owner, validate_sync_owner
from resource_deferred import ResourceDeferred

_active = {}


def _metadata(project):
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
    ledger = fresh._load(ledger_path)
    fresh._check_marker(ledger, marker_path)
    raw = J._observed_route({'project_root': str(project)}, None)
    _route, digest = J._route(raw, journal)
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
            R._need(R._raw(_metadata(self.project)) == self.packed, 'worker_journal_binding_changed')
            self.deadline = time.monotonic()+2
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
        R._need(R._raw(_metadata(self.project)) == self.packed, 'worker_journal_binding_changed')
        validate_sync_owner(self.owner, self.queue)
        R._need(time.monotonic() < self.deadline, 'worker_journal_deadline')


def worker_journal_access(project):
    return _Access(project)


def recover_legacy(scope, path):
    R._need(type(scope) is _Access and _active.get(id(scope)) is scope,
            'worker_journal_scope_expired')
    scope.check(path)
    path = Path(path)
    if os.path.lexists(path.parent/J._INITIAL_INTENT):
        return False
    side = Path(str(path)+'-journal')
    if not os.path.lexists(side):
        return False
    R._file(side, path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        header = os.pread(fd, 100, 0)
        R._need([info.st_dev, info.st_ino] == scope.snapshot['journal_node'],
                'worker_journal_binding_changed')
    finally:
        os.close(fd)
    if (len(header) != 100 or header[:16] != b'SQLite format 3\0'
            or int.from_bytes(header[60:64], 'big') not in (3, 4)
            or int.from_bytes(header[68:72], 'big') != J.APP_ID):
        return False
    scope.check(path)
    # Conversion and initial recovery acquire this same queue owner before
    # publishing v5 or creating its intent. No converter can cross this gap.
    recovered = J._recover_interrupted_transaction(path)
    scope.check(path)
    return recovered
