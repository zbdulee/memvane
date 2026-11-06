"""Metadata-only preparation for a selected v5 writer's first content proof.

A plan describes existing canonical files and sealed coordination locks. It
grants no SQLite open, writer, reader, mover, cohort or immutable capability.
The caller must acquire lifetime/physical windows and prove the full current
installation before issuing normal writer authority.

Store formats. The plan follows the journal's preferred reader endpoint
(``snapshot['endpoint']['store_format']``):

- ``v6``: unchanged. The snapshot has no ``store`` key, so the packed bytes
  and ``plan_id`` equal those of earlier releases.
- ``schema3``: capability ``schema3-ro-v1`` and checkout policy
  ``selected-main-schema3-v1``. Install identity is checked as for v6 except
  the record incarnation, which is the store's. The snapshot adds ``store``
  (see ``plan_store``), read from the store with one query_only connection.

Both formats keep the five v6 ``components`` and their physical locks: the
lock manifest contract is shared, and an older writer is kept out by the
journal record, not by a lock. ``laya_writer_scope`` refuses every v6
component to a schema3 writer (``writer_selection_unavailable``).

Refusal codes a caller can see here: ``writer_route_unavailable`` (route not
a selected candidate), ``writer_selection_unavailable`` (unknown format or
capability, pending bundle), ``writer_plan_binding_changed`` (install/ledger
binding, also an ``upgrading`` ledger fence), ``writer_plan_store_changed``
(schema3 store node, path, incarnation, repository, worktree binding or
policy differs from the record), ``writer_plan_changed`` (a revalidated plan
differs, e.g. after a reader CAS), ``writer_windows_not_prepared``.
"""
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import weakref

import laya_install_journal as J
import laya_journal_reader as R
import laya_reader_lease as L
import laya_reader_windows as W

_issued = weakref.WeakKeyDictionary()
_proofs = {}


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


def _schema3_store(record, state, decision):
    """Match the selected schema3 record against its store, read-only.

    One query_only connection reads ``store_meta`` and the main worktree row;
    ``laya_blob_store`` validates header, schema and worktree evidence. The
    store is never the route's v6 ``dbPath``.
    """
    import laya_blob_store as blobs
    import laya_blob_values as values
    from laya_checkout_view import _digest
    path = Path(record['db_path'])
    R._need(path.is_absolute() and str(path) != decision['dbPath'], 'writer_plan_store_changed')
    node = R._file(path, state)
    R._need(node == record['db_node'], 'writer_plan_store_changed')
    with R._readonly(path, state) as db:
        db.row_factory = sqlite3.Row
        try:
            row = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
            R._need(row is not None, 'writer_plan_store_changed')
            repository = values.repository(values.parse_canonical(row[0]))
            meta = blobs._header(db, repository, record['scope'])
            worktree = db.execute('SELECT * FROM worktrees WHERE worktree_id=?',
                                  (record['worktree_id'],)).fetchone()
            R._need(worktree is not None, 'writer_plan_store_changed')
            blobs._worktree_record(worktree, repository, meta['settings_policy_digest'])
        except values.BlobStoreError as exc:
            raise J.JournalEvidenceError('writer_plan_store_changed') from exc
    R._need(meta['incarnation'] == record['incarnation']
            and _digest(repository) == record['repository_digest']
            and worktree['binding_digest'] == record['worktree_binding']
            and meta['settings_policy_digest'] == record['settings_policy_digest'],
            'writer_plan_store_changed')
    return dict(path=str(path), node=node, incarnation=meta['incarnation'],
                worktree_id=record['worktree_id'], repository=repository,
                repository_digest=record['repository_digest'],
                worktree_binding=record['worktree_binding'],
                policy_digest=meta['settings_policy_digest'])


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
            attestation, attestation_sha = J._attestation(path)
            R._need(attestation is not None and attestation_sha == bundle['provider_attestation_sha'],
                    'writer_plan_attestation_changed')
            if attestation_sha != record['attestation_sha256']:
                R._successor_chain(db, selection.preferred_endpoint, record, attestation_sha,
                                   bundle['selected'], max_epoch=bundle['epoch'])
        store_format = record['store_format']
        R._need(bundle['state'] in ('active', 'restored_old') and bundle['pending'] is None
                and ((store_format == 'v6' and record['capability'] == 'v6-ro-v1')
                     or (store_format == 'schema3' and record['capability'] == 'schema3-ro-v1'
                         and record['checkout_policy'] == 'selected-main-schema3-v1')),
                'writer_selection_unavailable')
        selected = bundle['selected']
        R._need(type(selected) is dict and selected['bundleKind'] == 'full_plugin'
                and selected['pluginRoot'] == str(root) == decision['pluginRoot'],
                'writer_bundle_unavailable')
        R._need(record['install_id'] == selected['installId'] == decision['installId']
                and J.ID.fullmatch(record['install_id']) is not None
                and record['scope'] == bundle['scope_id'] == decision['scopeId']
                and (store_format != 'v6' or record['incarnation'] == decision['dbIncarnation'])
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
        R._need(route['dbPath'] == decision['dbPath']
                and route['queuePath'] == decision['queuePath'], 'writer_plan_binding_changed')
        for key in ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256', 'vectorProfile'):
            R._need(attestation[key] == selected[key], 'writer_plan_attestation_changed')
        # A reader may legally select an equivalent staged base. Writer paths
        # come from the active install ledger/build, independently of that CAS.
        import laya_fresh_wiki_origin as fresh
        import laya_initial_build as build
        ledger = fresh._load(state / fresh.LEDGER)
        built = build.read(state)
        R._need(built is not None and ledger['phase'] == 'active'
                and ledger['projectRoot'] == str(project) and ledger['journalPath'] == str(path)
                and ledger['routeSha256'] == digest
                and ledger['attestationSha256'] == attestation_sha
                and ledger['providerFileIdentitySha256'] == R._sha(R._raw(bundle['provider_file_identity']))
                and ledger['scope'] == decision['scopeId']
                and ledger['candidateIncarnation'] == decision['dbIncarnation']
                and ledger['vectorProfileId'] == decision['vectorProfile']['profileId']
                and ledger['candidateDb'] == decision['dbPath']
                and ledger['queueDb'] == decision['queuePath'], 'writer_plan_binding_changed')
        identity = built['identity']
        for key, expected in (('projectRoot', str(project)), ('stateRoot', str(state)),
                ('installId', record['install_id']), ('scopeId', decision['scopeId']),
                ('candidateDb', ledger['candidateDb']), ('queueDb', ledger['queueDb']),
                ('catalogDb', ledger['catalogDb']), ('bindingStore', ledger['bindingStore']),
                ('dbIncarnation', decision['dbIncarnation']),
                ('vectorProfileId', ledger['vectorProfileId'])):
            R._need(identity[key] == expected, 'writer_plan_build_changed')
        components = {}
        fields = (('base', 'candidateDb', 'candidateIdentity'),
                  ('queue', 'queueDb', 'queueIdentity'),
                  ('catalog', 'catalogDb', 'catalogIdentity'),
                  ('binding', 'bindingStore', 'bindingStoreIdentity'))
        for role, field, node_field in fields:
            location = Path(ledger[field])
            node = R._file(location, state)
            R._need(node == ledger[node_field], 'writer_plan_component_changed')
            components[role] = dict(path=str(location), node=node)
        from laya_incremental_docs import FILE
        # A schema3 record names no v6 sidecar; the install's own path is it.
        sidecar = Path(record['sidecar_path']) if store_format == 'v6' else state / FILE
        R._need(sidecar == state / FILE, 'writer_plan_component_changed')
        components['sidecar'] = dict(path=str(sidecar), node=(
            R._file(sidecar, state) if os.path.lexists(sidecar) else None))
        R._need(len({row['path'] for row in components.values()}) == len(components),
                'writer_plan_component_changed')
        store = _schema3_store(record, state, decision) if store_format == 'schema3' else None
        manifest = _manifest(record['install_id'])
        snapshot = dict(project=str(project), executing_root=str(root), executing_root_node=R._node(root),
                        decision=decision, ledger=ledger, build_identity=identity,
                        bundle=bundle, endpoint=record, reader_epoch=selection.epoch,
                        endpoint_id=selection.preferred_endpoint, components=components,
                        manifest=manifest, attestation_sha=attestation_sha)
        if store is not None:
            # Only schema3 adds a key: v6 snapshots stay byte-identical.
            snapshot['store'] = store
        return snapshot


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


def plan_store(plan):
    """Return the issued schema3 store description, or None for v6.

    Keys: ``path`` (absolute store file in the state root), ``node``
    ([dev, ino]), ``incarnation`` (store_meta), ``worktree_id`` (main
    checkout), ``repository`` (store_meta repository JSON, for
    ``ViewStore.open``), ``repository_digest``, ``worktree_binding`` and
    ``policy_digest``. This is a description, not write authority: the
    caller still needs a writer scope and ``validate_writer_plan``.
    """
    _packed, snapshot = _plan_record(plan)
    store = snapshot.get('store')
    return None if store is None else json.loads(R._raw(store))


@dataclass(frozen=True, eq=False, init=False)
class WriterProofWindow:
    """An active lock window, without a content-open or writer capability."""
    def __init__(self):
        raise ValueError('writer_proof_window_not_issued')

    def __reduce__(self):
        raise TypeError('writer_proof_window_not_serializable')


def _proof_window_record(window):
    R._need(type(window) is WriterProofWindow and window in _proofs,
            'writer_proof_window_not_issued')
    proof = _proofs[window]
    R._need(proof['pid'] == os.getpid() and proof['thread'] is threading.current_thread(),
            'writer_proof_window_expired')
    R._need(not proof['stack'].expired and not proof['stack'].invalid and not proof['stack'].released,
            'writer_proof_window_expired')
    return proof


def validate_writer_proof_window(window):
    """Validate live owner and original locks before a later proof operation."""
    from laya_index_sync import validate_sync_owner
    proof = _proof_window_record(window)
    plan = proof['plan']
    validate_sync_owner(proof['queue_owner'], plan.queue_path)
    validate_writer_plan(plan)
    _packed, snapshot = _plan_record(plan)
    lifetime = proof['lifetime']
    lifetime.check()
    R._need(W._manifest(plan.install_id, lifetime.parent, lifetime.parent_fd,
                        lifetime.parent_info).decode('utf8') == snapshot['manifest'],
            'writer_proof_windows_changed')
    for role, fd in proof['physical'].items():
        W._named(plan.install_id, lifetime.parent, lifetime.parent_fd, lifetime.parent_info,
                 role + '-physical.lock', fd)
    if time.monotonic() >= proof['deadline']:
        raise W.ReaderWindowBusy('writer_proof_deadline')


class _WriterProofContext:
    """Explicit lock ownership; no generator finalizer releases native owners."""
    def __init__(self, plan, queue_owner, deadline):
        self.plan, self.queue_owner, self.deadline = plan, queue_owner, deadline
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.entered = self.finished = False
        self.stack = self.window = None

    def __enter__(self):
        from laya_index_sync import validate_sync_owner
        from laya_writer_proof_reader import _ProofExitStack
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_owner_changed')
        R._need(not self.entered, 'writer_proof_window_reentered')
        self.entered = True
        try:
            W._deadline(self.deadline)
            validate_writer_plan(self.plan)
            validate_sync_owner(self.queue_owner, self.plan.queue_path)
            _packed, snapshot = _plan_record(self.plan)
            nodes = json.loads(snapshot['manifest'])['locks']
            self.stack = stack = _ProofExitStack()
            pin = self.queue_owner.pin()
            stack.callback(pin.close)
            lifetime = W._Lifetime(self.plan.install_id)
            stack.callback(lifetime.close)
            physical = {}
            for role in sorted(snapshot['components']):
                name = role + '-physical.lock'
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=lifetime.parent_fd)
                stack.callback(os.close, fd)
                L._private_file(os.fstat(fd))
                while True:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise W.ReaderWindowBusy('writer_proof_physical_busy')
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(.005, remaining))
                W._named(self.plan.install_id, lifetime.parent, lifetime.parent_fd,
                         lifetime.parent_info, name, fd)
                info = os.fstat(fd)
                R._need(nodes[name] == [info.st_dev, info.st_ino], 'writer_proof_windows_changed')
                physical[role] = fd
            self.window = window = object.__new__(WriterProofWindow)
            record = dict(pid=os.getpid(), thread=threading.current_thread(), plan=self.plan,
                          queue_owner=self.queue_owner, lifetime=lifetime, physical=physical,
                          deadline=self.deadline, window=window, stack=stack, context=self)
            stack.record = record
            _proofs[window] = record
            validate_writer_proof_window(window)
            return window
        except BaseException:
            self.finished = True
            if self.window is not None:
                _proofs.pop(self.window, None)
            if self.stack is not None:
                self.stack.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_owner_changed')
        if not self.entered or self.finished:
            return False
        self.finished = True
        try:
            if exc_type is None:
                validate_writer_proof_window(self.window)
        finally:
            _proofs.pop(self.window, None)
            self.stack.__exit__(exc_type, exc, traceback)
        return False


def writer_proof_windows(plan, *, queue_owner, deadline):
    """Hold lifetime SH and sorted physical EX; issue no normal writer authority."""
    return _WriterProofContext(plan, queue_owner, deadline)
