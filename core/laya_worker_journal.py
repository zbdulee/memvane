"""Selected-worker metadata admission and sync ownership."""
import os
from pathlib import Path
import threading
import time

import laya_install_journal as J
import laya_journal_reader as R
from persistent_identity import persistent_node_equal
from laya_store_primitives import IndexContractError
from sync_owner_lock import sync_owner, validate_sync_owner
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
    raw = J._observed_route({'project_root': str(project)}, None)
    route, digest = J._route(raw, journal)
    R._need(os.path.lexists(marker_path), 'worker_journal_binding_changed')
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
        R._need(persistent_node_equal(ledger[identity], R._file(Path(ledger[field]), state)),
                'worker_journal_binding_changed')
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
    def __init__(self, project, wait=False):
        self.project = Path(project)
        self.wait = wait
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.started = self.finished = False
        self.owner = self.owner_context = None

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
        self.owner_context = sync_owner(self.queue, wait=self.wait)
        self.owner = self.owner_context.__enter__()
        if self.owner is None:
            self.owner_context.__exit__(None, None, None)
            self.finished = True
            raise ResourceDeferred('owner_busy')
        try:
            R._need(R._raw(_metadata(self.project)) == self.packed,
                    'worker_journal_binding_changed')
            self.deadline = time.monotonic()+2
            _active[id(self)] = self
            return self
        except BaseException:
            self.finished = True
            self.owner_context.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._creator()
        if self.finished or self.owner is None:
            return False
        self.finished = True
        _active.pop(id(self), None)
        self.owner_context.__exit__(exc_type, exc, traceback)
        return False

    def check(self, path):
        self._creator()
        R._need(_active.get(id(self)) is self and not self.finished
                and Path(path) == Path(self.snapshot['decision']['journalPath']),
                'worker_journal_scope_expired')
        validate_sync_owner(self.owner, self.queue)
        R._need(R._raw(_metadata(self.project)) == self.packed,
                'worker_journal_binding_changed')
        validate_sync_owner(self.owner, self.queue)
        R._need(time.monotonic() < self.deadline, 'worker_journal_deadline')


def worker_journal_access(project, *, wait=False):
    """``wait=True`` (off-hook sync worker only) waits briefly for a busy owner."""
    return _Access(project, wait)


