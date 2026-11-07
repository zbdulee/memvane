"""Off-hook, journal-bound full-scan sync for a selected memvane-db route.

Runtime wakes contain only a project path. Every attempt rereads the selected
route, installer journal, provider attestation and DB/queue incarnation. A
failed or deferred attempt leaves the wake for the runtime dispatcher to retry.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time

from resource_deferred import ResourceDeferred
from context_learning.resource_coordination import Coordinator
from context_learning.shared import resource_report
from laya_index_a import IndexContractError
from laya_index_host_route import (select_route, selected_collection_roots,
                                   wiki_index_roots)
from laya_index_runtime import (BINDING_STORE_SUFFIX, CATALOG_SUFFIX, LayaIndexRuntime,
                                _collection_roots, capacity_store_paths)
from laya_index_sync import prepare_candidate_batch_off_hook, sync_full, sync_step, sync_owner
from laya_install_journal import (JournalEvidenceError, cas_mark_new_write, inspect_journal,
                                  preflight_product_candidate,
                                  verify_selected_route_for_write as verify_selected_route,
                                  verify_worker_attestation)
from laya_local_embed_provider import ReviewedLocalQmdProvider
from laya_local_embed_service import start_owned_embed_service

# A conservative cold-start proxy, not a measured GGUF/native peak. The
# installed-candidate resource QA must measure and review this envelope.
EMBED_POLICY = {'maxRssMib': 2048, 'minAvailableMib': 768,
                'maxLoadPerCpu': 1, 'targetHeadroomFraction': .35}

# RQA-RACE-01 Part B only. Unset (the product default) reads no file and
# never waits. Set, it names `<fixture>/.qa-laya-sync-barrier.json` beside a
# release QA fixture owner marker; see docs/release-agent-qa.md.
QA_BARRIER_ENV = 'LAYA_QA_SYNC_BARRIER'
QA_BARRIER_ARM = '.qa-laya-sync-barrier.json'
QA_BARRIER_SCHEMA = 'laya-qa-sync-barrier-v1'
QA_FIXTURE_SCHEMA = 'qmd-release-qa-fixture-v1'
QA_BARRIER_MAX_SECONDS = 600

# sync_step/vector_fill_step rounds in one wake; more remaining stays pending
# and the dispatcher retains the wake.
MAX_CAPACITY_ROUNDS = 64
# Wall-clock bound of one initial-build wake (checked between rounds). The
# rest stays durable in the catalog, fill cursor and claim queue.
BUILD_WAKE_SECONDS = 45


def _deferred(exc):
    """Keep the admission criterion (cpu_pressure, memory_reserve, ...) for status."""
    result = {'state': 'deferred', 'reason': str(exc)}
    report = getattr(exc, 'report', None)
    if isinstance(report, dict) and isinstance(report.get('reason'), str):
        result['detail'] = report['reason']
    return result

def _large_initial_document(body):
    """Keep the existing provider recipe; only extend a large build request."""
    return isinstance(body, str) and len(body.encode('utf-8', 'surrogatepass')) > 200_000


def _embed_with_budget(provider, body, path):
    """One document embedding with the large-document call-site budget.

    Initial build and selected sync share it. The 270s budget is below the
    worker's 300s idle limit; the reviewed provider and embedding recipe
    remain byte-identical to existing vectors.
    """
    if _large_initial_document(body):
        return provider.embed_document_chunks(body, path, timeout=270)
    return provider.embed_document_chunks(body, path)


def _qa_invalid():
    raise IndexContractError('qa_sync_barrier_invalid')


def _qa_json(path, limit, *, private):
    """Read one owned, no-follow regular JSON object; None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        _qa_invalid()
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > limit
                or info.st_mode & (0o077 if private else 0o022)):
            _qa_invalid()
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        _qa_invalid()
    if not isinstance(value, dict):
        _qa_invalid()
    return value


class QaSyncBarrier:
    """One deterministic hold after a complete batch, before any index write.

    It holds only the first attempt per nonce. The hold cannot publish, ACK,
    skip a freshness check or change a queue; the attempt that resumes still
    runs every check in sync_full. Evidence files are 0600 in the fixture.
    """
    def __init__(self, root, armed, *, clock=time.monotonic, wall=time.time,
                 sleep=time.sleep):
        self.root, self.armed = root, armed
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.held = False
        self.released_at = None

    def path(self, suffix):
        return self.root / ('.qa-laya-sync-barrier-%s.%s' % (self.armed['nonce'], suffix))

    def _record(self, suffix, value):
        try:
            fd = os.open(self.path(suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            return False
        except OSError:
            _qa_invalid()
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'schema': QA_BARRIER_SCHEMA, 'nonce': self.armed['nonce'],
                       'pid': os.getpid(), **value}, out, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        return True

    def hold(self, source_manifest):
        if not self._record('held', {'sourceManifest': source_manifest,
                                     'heldAt': self.wall()}):
            return False
        self.held = True
        deadline = self.clock() + self.armed['timeoutSeconds']
        release = self.path('release')
        while True:
            try:
                info = os.lstat(release)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    _qa_invalid()
                self.released_at = self.wall()
                return True
            if self.clock() >= deadline:
                raise ResourceDeferred('laya_qa_barrier_timeout')
            self.sleep(min(.1, max(0, deadline - self.clock())))

    def finish(self, result=None, error=None):
        if not self.held:
            return
        result = result if isinstance(result, dict) else {}
        self._record('done', {'releasedAt': self.released_at,
                              'finishedAt': self.wall(),
                              'state': result.get('state'),
                              'generation': result.get('generation'),
                              'reason': result.get('reason') or
                                        (str(error) if error is not None else None),
                              'error': type(error).__name__ if error is not None else None})


def qa_sync_barrier(decision, *, environ=None):
    """Return a barrier only for the exact armed fixture project/scope/DB."""
    raw = (os.environ if environ is None else environ).get(QA_BARRIER_ENV)
    if raw is None:
        return None
    arm = Path(raw)
    if not arm.is_absolute() or arm.name != QA_BARRIER_ARM or '..' in arm.parts:
        _qa_invalid()
    root = arm.parent
    armed = _qa_json(arm, 4096, private=True)
    if armed is None:
        return None
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError:
        _qa_invalid()
    if (resolved != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        _qa_invalid()
    owner = _qa_json(root / '.qa-fixture-owner', 4096, private=False)
    manifest = _qa_json(root / 'MANIFEST.json', 1 << 20, private=False)
    if (owner is None or manifest is None
            or manifest.get('schema') != QA_FIXTURE_SCHEMA
            or manifest.get('root') != str(root)
            or owner.get('fixtureId') != manifest.get('fixtureId')
            or owner.get('uid') != os.getuid()
            or not isinstance(manifest.get('paths'), dict)):
        _qa_invalid()
    if (set(armed) != {'schema', 'fixtureId', 'projectRoot', 'scopeId',
                       'dbIncarnation', 'nonce', 'timeoutSeconds'}
            or armed['schema'] != QA_BARRIER_SCHEMA
            or armed['fixtureId'] != manifest['fixtureId']
            or armed['projectRoot'] != manifest['paths'].get('project')
            or not isinstance(armed['nonce'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', armed['nonce'])
            or type(armed['timeoutSeconds']) is not int
            or not 1 <= armed['timeoutSeconds'] <= QA_BARRIER_MAX_SECONDS):
        _qa_invalid()
    if decision.get('projectRoot') != armed['projectRoot']:
        return None
    if (decision.get('scopeId'), decision.get('dbIncarnation')) != (
            armed['scopeId'], armed['dbIncarnation']):
        _qa_invalid()
    return QaSyncBarrier(root, armed)


# A QMD wake that already failed this many times is retried on its own
# backoff without making selected syncs yield to it. Otherwise one stuck
# legacy index/update row (setup required, fail-closed store) comes due every
# few minutes and no long sync can ever finish.
PRIORITY_YIELD_MAX_ATTEMPTS = 3


def _higher_priority_wake(controller):
    """Yield for due QMD work; preserve an unavailable probe as a distinct deferral."""
    from runtime_work import WakeQueue
    try:
        rows = WakeQueue(controller.root).pending()
        return any(row['kind'] in ('index', 'update') and row['due'] <= time.time()
                   and row['attempts'] < PRIORITY_YIELD_MAX_ATTEMPTS
                   for row in rows)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Keep the fail-closed behavior, but never label a queue access error
        # as an observed higher-priority job. No queue row is recorded.
        raise ResourceDeferred('laya_sync_priority_probe_unavailable') from exc


def _build_attempt_class(result=None, error=None):
    """Non-sensitive outcome for this project's build record, never queue data."""
    if error is not None:
        if (isinstance(error, IndexContractError)
                and str(error) == 'provider_unavailable'
                and isinstance(error.__cause__, (TimeoutError, socket.timeout))):
            return 'provider_timeout'
        if isinstance(error, IndexContractError) and str(error).startswith('provider_'):
            return 'provider_error'
        return 'worker_error'
    reason = result.get('reason') if isinstance(result, dict) else None
    if reason == 'laya_sync_yield_priority':
        return 'higher_priority_due'
    if reason == 'laya_sync_priority_probe_unavailable':
        return 'priority_probe_unavailable'
    if isinstance(reason, str) and reason.startswith('provider_'):
        return 'provider_error'
    return 'other'


_BUILD_PROVIDER_REASONS = frozenset({
    'provider_already_running', 'provider_start_failed',
    'provider_start_timeout', 'provider_socket_invalid',
    'provider_socket_changed', 'provider_identity_mismatch',
    'provider_result_invalid', 'provider_member_refused',
    'provider_lock_invalid', 'provider_unavailable',
})


def _build_attempt_reason(error):
    """Persist only reviewed fixed codes, never an exception's private text."""
    if not isinstance(error, IndexContractError):
        return 'other_error'
    code = str(error)
    if code not in _BUILD_PROVIDER_REASONS:
        return 'other_error'
    if code == 'provider_unavailable':
        cause = error.__cause__
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return 'provider_timeout'
        if isinstance(cause, (ConnectionRefusedError, FileNotFoundError)):
            return 'provider_socket_unavailable'
        if isinstance(cause, OSError):
            return 'provider_io_error'
    return code


def _capacity_paths(decision):
    return capacity_store_paths(decision['dbPath'])


def _writer_executing_root():
    return Path(__file__).resolve().parent.parent


def _selected_v5_proof(project, decision, owner):
    import laya_install_journal as journal
    import laya_writer_open_plan as plan_module
    from laya_reader_windows import ReaderWindowBusy
    try:
        plan = plan_module.capture_writer_plan(project, executing_root=_writer_executing_root())
    except journal.JournalEvidenceError as exc:
        if str(exc) == 'writer_windows_not_prepared':
            raise ResourceDeferred(str(exc)) from exc
        raise
    try:
        with plan_module.writer_proof_windows(plan, queue_owner=owner,
                                              deadline=time.monotonic()+2) as window:
            with journal.writer_identity_reads(window):
                return verify_selected_route(decision)
    except ReaderWindowBusy as exc:
        if str(exc) in ('writer_proof_physical_busy', 'writer_proof_deadline',
                        'reader_lifetime_busy'):
            raise ResourceDeferred(str(exc)) from exc
        raise


def _owned_selected_proof(decision, owner):
    """Reprove a selected worker route with the queue owner already held."""
    import laya_install_journal as journal
    with journal.read_only_journals():
        if journal.journal_version(decision['journalPath']) == 5:
            return _selected_v5_proof(Path(decision['projectRoot']), decision, owner)
        with journal.legacy_identity_reads():
            return verify_selected_route(decision)


def _owned_worker_attestation(decision, owner, expected):
    """Reprove v5 around closure hashing without holding physical windows."""
    import laya_install_journal as journal
    with journal.read_only_journals():
        version = journal.journal_version(decision['journalPath'])
        if version != 5:
            with journal.legacy_identity_reads():
                manifest = verify_worker_attestation(decision)
                if verify_selected_route(decision) != expected:
                    raise IndexContractError('worker_selection_changed')
                return manifest
        project = Path(decision['projectRoot'])
        selected = _selected_v5_proof(project, decision, owner)
        if selected != expected:
            raise IndexContractError('worker_selection_changed')
        manifest = journal._worker_attestation_manifest(selected)
        if _selected_v5_proof(project, decision, owner) != selected:
            raise IndexContractError('worker_selection_changed')
        return manifest


@contextmanager
def _full_worker_runtime(decision, owner, proof, source_root, *, capacity=None):
    """Keep the v5 writer and its pins until the runtime has closed."""
    import laya_install_journal as journal
    from contextlib import ExitStack
    windows = readers = None
    try:
        with ExitStack() as stack:
            writer = None
            options = {}
            roles = ('base', 'queue') if capacity is None else ('base', 'queue', 'catalog', 'binding')
            if capacity is not None:
                options.update(catalog_path=capacity[0], binding_store_path=capacity[1],
                               wiki_only='collections' in decision)
            with journal.read_only_journals():
                if journal.journal_version(decision['journalPath']) == 5:
                    import laya_reader_windows as windows
                    import laya_writer_proof_reader as readers
                    import laya_writer_open_plan as plans
                    import laya_writer_scope as normal
                    plan = plans.capture_writer_plan(Path(decision['projectRoot']),
                                                      executing_root=_writer_executing_root())
                    writer = stack.enter_context(normal.worker_writer_scope(
                        plan, queue_owner=owner, operation_timeout=2))
                    if normal.admission_proof(writer) != proof:
                        raise IndexContractError('selected_proof_changed')
                    options['connection_windows'] = {
                        role: windows.component_for(writer, role) for role in roles}
            runtime = stack.enter_context(LayaIndexRuntime(
                source_root=source_root, scope=decision['scopeId'], db_path=decision['dbPath'],
                queue_path=decision['queuePath'], db_incarnation=decision['dbIncarnation'], **options))
            yield runtime, writer
    finally:
        if windows is not None:
            try:
                windows.drain_pending(deadline=time.monotonic()+2)
            finally:
                readers.drain_proof_readers(deadline=time.monotonic()+2)
            if windows.local_pending_count() or readers.local_pending_proof_readers():
                raise ResourceDeferred('writer_cleanup_unsettled')


@contextmanager
def _discovery_base_component(decision, owner, proof):
    """Bind discovery's base opens to the selected worker owner and drain."""
    import laya_install_journal as journal
    import laya_reader_windows as windows
    import laya_writer_proof_reader as readers
    import laya_writer_open_plan as plans
    import laya_writer_scope as normal
    try:
        with journal.read_only_journals():
            plan=plans.capture_writer_plan(Path(decision['projectRoot']),
                                           executing_root=_writer_executing_root())
        with normal.worker_writer_scope(plan,queue_owner=owner,operation_timeout=2) as writer:
            if normal.admission_proof(writer) != proof:
                raise IndexContractError('selected_proof_changed')
            yield windows.component_for(writer,'base')
    finally:
        try:
            windows.drain_pending(deadline=time.monotonic()+2)
        finally:
            readers.drain_proof_readers(deadline=time.monotonic()+2)
        if windows.local_pending_count() or readers.local_pending_proof_readers():
            raise ResourceDeferred('writer_cleanup_unsettled')


def _selected(project):
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise IndexContractError('selected_project_unavailable')
    import laya_install_journal as journal
    from laya_worker_journal import worker_journal_access
    # Clean legacy installs need no new ledger, lock or recovery permission.
    # A hot image falls through to the narrowly owned recovery admission.
    with journal.read_only_journals():
        decision = select_route(str(project), action='update')
        if decision.get('state') == 'candidate' and decision.get('projectRoot') == str(project):
            try:
                if journal.journal_version(decision['journalPath']) != 5:
                    with journal.legacy_identity_reads():
                        proof = verify_selected_route(decision)
                    if (proof.get('productRouteAllowed') is not True
                            or not isinstance(proof.get('providerManifest'), dict)):
                        raise IndexContractError('selected_provider_unavailable')
                    return decision, proof
            except journal.JournalEvidenceError as exc:
                if str(exc) not in ('journal_unavailable', 'writer_plan_v5_required'):
                    raise
    # Classification/version reads must not recover a legacy hot image. The
    # legacy proof also checks its actual transaction's version, closing the
    # v4-observed/v5-published interval before any raw content connection.
    with worker_journal_access(project) as access:
        for attempt in range(2):
            with journal.read_only_journals():
                decision = select_route(str(project), action='update')
                if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
                    raise IndexContractError('selected_route_unavailable')
                if journal.journal_version(decision['journalPath']) == 5:
                    proof = _selected_v5_proof(project, decision, access.owner)
                else:
                    try:
                        with journal.legacy_identity_reads():
                            proof = verify_selected_route(decision)
                    except journal.JournalEvidenceError as exc:
                        if str(exc) == 'writer_plan_v5_required' and attempt == 0:
                            continue
                        raise
                break
    if proof.get('productRouteAllowed') is not True or not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('selected_provider_unavailable')
    return decision, proof


def _selected_service(decision, manifest, admit, start_service):
    """Borrow the installed warm owner; retain injectable transient fixtures.

    The detached owner cannot acquire the provider admission lane while this
    sync holds it. A cold wake defers here; the next durable wake starts the
    owner outside this lane.
    """
    if start_service is start_owned_embed_service:
        from laya_embed_resident import AttachedService, probe
        # This callback runs with the provider lane held. Starting its new
        # owner here could race that lane on a model failure between prewarm
        # and embedding; the next durable sync wake starts it outside the lane.
        status = probe(manifest)
        if status == 'ready':
            return AttachedService()
        raise ResourceDeferred('laya_service_identity_transition' if status == 'mismatch'
                               else 'laya_service_preparing')
    return start_service(manifest, admit=admit)


def _route_source(decision):
    """Choose the runtime source binding before any provider or model start.

    v1 passes ``sourceRoot`` unchanged. v2 verifies both declared collection
    roots, then passes only its wiki root to the runtime.
    """
    v2 = 'collections' in decision or 'collectionRoots' in decision
    if v2 and 'sourceRoot' in decision:
        raise IndexContractError('selected_route_source_ambiguous')
    if not v2:
        if 'sourceRoot' not in decision:
            raise IndexContractError('selected_route_source_missing')
        return decision['sourceRoot']
    roots = decision.get('collectionRoots')
    declared = decision.get('collections')
    if not isinstance(roots, Mapping) or not roots:
        raise IndexContractError('selected_collection_roots_invalid')
    if (not isinstance(declared, list) or len(declared) != len(roots)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                   for item in declared)):
        raise IndexContractError('selected_collection_roots_mismatch')
    try:
        bound = [{'name': item['name'], 'role': item['role'],
                  'sourceRoot': roots[item['name']]} for item in declared]
        selected_collection_roots(decision['projectRoot'], bound)
    except (KeyError, TypeError, ValueError) as exc:
        raise IndexContractError('selected_collection_roots_invalid') from exc
    if {item['name']: item['sourceRoot'] for item in declared} != dict(roots):
        raise IndexContractError('selected_collection_roots_mismatch')
    try:
        wiki = wiki_index_roots(declared)
    except ValueError as exc:
        raise IndexContractError('selected_wiki_source_invalid') from exc
    return _collection_roots(wiki)


def run(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    """Run one selected sync; call only from the durable runtime wake worker.

    A project that does not select a Laya route but has a registered
    inactive initial build runs one bounded build step instead
    (``run_initial_build``). Without one, the refusal is unchanged.

    A completed sync (``indexed``/``unchanged``) then gives the private
    shadow migration one bounded pass (``shadow_pass``). The pass runs after
    every sync owner is released, and neither its outcome nor its failure
    changes the returned sync result.
    """
    result = _run_sync(project, coordinator=coordinator, report=report, start_service=start_service)
    if isinstance(result, dict) and result.get('state') in ('indexed', 'unchanged'):
        try:
            shadow_pass(project)
        except Exception:
            pass  # shadow_pass records its own outcome; the sync stays done.
    return result


def _run_sync(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    try:
        decision, proof = _selected(project)
    except ResourceDeferred as exc:
        return _deferred(exc)
    except IndexContractError as exc:
        if str(exc) != 'selected_route_unavailable':
            raise
        # Imported here: a selected route never needs it, and an installed
        # bundle that does not ship it yet must keep serving selected wakes.
        import laya_initial_build as initial_build
        build = initial_build.lookup(Path(project))
        if build is None:
            raise
        return run_initial_build(Path(project), build, coordinator=coordinator,
                                 report=report, start_service=start_service)
    # A malformed route source fails before any model start or index write.
    source_root = _route_source(decision)
    if _selected_store_format(decision) == 'schema3':
        # The view store path loads no model and opens no v6 runtime or
        # capacity store; discovery reads the view head, not the v6 lineage.
        if qa_sync_barrier(decision) is not None:
            raise IndexContractError('qa_barrier_schema3_unsupported')
        grants = None
        if not isinstance(source_root, str):
            # Snapshot hook grants before the scan, as on the v6 path.
            import laya_wiki_discovery
            grants = laya_wiki_discovery.observe_grants(decision)
        result, proof = _run_schema3(decision, proof)
        return _after_sync(decision, proof, grants, result, store_format='schema3')
    grants = None
    if not isinstance(source_root, str):
        # v2 only. Snapshot hook grants before the scan; later ones stay live.
        import laya_wiki_discovery
        grants = laya_wiki_discovery.observe_grants(decision)
    # A mis-armed QA barrier fails before any model start or index write.
    qa = qa_sync_barrier(decision)
    # A malformed capacity store fails here, before any model start.
    capacity = _capacity_paths(decision)
    if capacity is not None and qa is not None:
        # The QA race barrier holds a full-scan batch, but host recall would
        # use the capacity path for the same stores: the two would disagree.
        raise IndexContractError('qa_barrier_capacity_unsupported')
    provider = ReviewedLocalQmdProvider(proof['providerManifest'])
    profile = proof['vectorProfile']
    if (provider.profile['profile_id'] != profile['profileId'] or
            provider.profile['model_id'] != profile['modelId'] or
            provider.profile['embedding_fingerprint'] != profile['embeddingFingerprint'] or
            provider.profile['dimension'] != profile['dimension']):
        raise IndexContractError('selected_provider_profile_mismatch')
    if start_service is start_owned_embed_service:
        # Activation's selected wake must warm the model even when the sealed
        # candidate has no changed documents. The bounded dispatcher only
        # starts the independent owner and retries after it becomes ready.
        from laya_embed_resident import ensure_running
        try:
            import laya_install_journal as journal
            with journal.read_only_journals():
                if select_route(str(project), action='update') != decision:
                    raise IndexContractError('selected_provider_proof_changed')
            # This short owner admission requests adoption, never waits for
            # the model. Release it before taking the provider lane below.
            with sync_owner(Path(decision['queuePath'])) as prewarm_owner:
                if prewarm_owner is None:
                    return {'state': 'deferred', 'reason': 'owner_busy'}
                with journal.read_only_journals():
                    if journal.journal_version(decision['journalPath']) == 5:
                        current = _selected_v5_proof(Path(decision['projectRoot']),
                                                     decision, prewarm_owner)
                    else:
                        # Reject a concurrent v4-to-v5 transition before a
                        # legacy proof can open v5 content without a window.
                        with journal.legacy_identity_reads():
                            current = verify_selected_route(decision)
                if current != proof:
                    raise IndexContractError('selected_provider_proof_changed')
                ensure_running(decision['projectRoot'], proof['providerManifest'])
        except ResourceDeferred as exc:
            return _deferred(exc)
    controller = coordinator or Coordinator()
    observed = report if report is not None else resource_report(EMBED_POLICY, detailed=True)
    if capacity is not None:
        result, proof = _run_capacity(decision, proof, source_root, capacity, provider,
                                      controller, observed, start_service)
        _observe_member_block(decision, result)
        return _after_sync(decision, proof, grants, result)
    try:
        with controller.activity('provider', EMBED_POLICY, observed), \
                sync_owner(Path(decision['queuePath'])) as owner:
            if owner is None:
                return {'state': 'deferred', 'reason': 'owner_busy'}
            # The hook's sealed stat receipt is a fast freshness fence. This
            # off-hook worker must rehash the full native closure before load.
            validated_manifest = _owned_worker_attestation(decision, owner, proof)
            if validated_manifest != proof['providerManifest']:
                raise IndexContractError('selected_provider_proof_changed')
            def admit():
                # The service verifies its exact model/runtime files first.
                # Re-sample afterward, immediately before its model load.
                fresh = resource_report(EMBED_POLICY, detailed=True)
                return controller.check(EMBED_POLICY, fresh)['ready']

            service = _selected_service(decision, validated_manifest, admit, start_service)
            try:
                if _owned_selected_proof(decision, owner) != proof:
                    raise IndexContractError('selected_proof_changed')
                class AdmittedProvider:
                    manifest = provider.manifest
                    profile = provider.profile

                    def embed_document_chunks(self, body, path):
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        return _embed_with_budget(provider, body, path)

                with _full_worker_runtime(decision, owner, proof, source_root) as (runtime, writer):
                    def before_publish():
                        nonlocal proof
                        if _owned_selected_proof(decision, owner) != proof:
                            raise IndexContractError('selected_proof_changed')
                        if writer is not None:
                            import laya_writer_scope as normal
                            fresh = normal.mark_new_write(writer)
                            if (fresh.get('selectionEpoch') != proof['selectionEpoch'] + 1 or
                                    fresh.get('productRouteAllowed') is not True):
                                raise IndexContractError('new_write_proof_unconfirmed')
                            proof = fresh
                            from laya_publication_revision import ensure_base_baseline
                            ensure_base_baseline(runtime.index, scope=decision['scopeId'],
                                                 profile_id=profile['profileId'], deadline=time.monotonic()+2)
                            return
                        marked = cas_mark_new_write(decision['journalPath'],
                                                    proof['selectionEpoch'])
                        if (marked.get('epoch') != proof['selectionEpoch'] + 1 or
                                marked.get('newWriteSeen') is not True):
                            raise IndexContractError('new_write_intent_unconfirmed')
                        fresh = _owned_selected_proof(decision, owner)
                        if (fresh.get('selectionEpoch') != marked['epoch'] or
                                fresh.get('productRouteAllowed') is not True):
                            raise IndexContractError('new_write_proof_unconfirmed')
                        proof = fresh

                    batch = prepare_candidate_batch_off_hook(runtime, AdmittedProvider())
                    if _owned_selected_proof(decision, owner) != proof:
                        raise IndexContractError('selected_proof_changed')
                    try:
                        if qa is not None:
                            # The batch is accepted; nothing is enqueued yet.
                            qa.hold(batch['source_manifest'])
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        result = sync_full(runtime, before_publish=before_publish,
                                           vector_batch=batch, require_vector=True, owner=owner)
                    except BaseException as exc:
                        if qa is not None:
                            qa.finish(error=exc)
                        raise
                    if qa is not None:
                        qa.finish(result)
                if _owned_selected_proof(decision, owner) != proof:
                    raise IndexContractError('selected_proof_changed')
            finally:
                service.stop()
    except ResourceDeferred as exc:
        return _deferred(exc)
    if result.get('state') not in ('indexed', 'unchanged', 'deferred'):
        raise IndexContractError('invalid_sync_result')
    return _after_sync(decision, proof, grants, result)


# One schema3 pass: wall budget and card ingests (each a Git capture). A
# large change set to a published view publishes its progress and stays
# ``pending``; a cold view (no published generation) only prepares it.
SCHEMA3_PASS_SECONDS = 45
SCHEMA3_MAX_INGEST = 64
# Ingest stops this long before the pass deadline so prepare/publish of the
# ingested cards always fits; otherwise an over-budget pass would publish
# nothing and the next one would redo the same ingests.
SCHEMA3_PUBLISH_RESERVE = 15
# The store binds the v6 settings identity (collections, roles, roots) and
# the wiki root node in an immutable worktree row; a change refuses reads and
# writes alike, and no in-place rebinding exists. Not transient.
SCHEMA3_BINDING_REASONS = frozenset(('wiki_binding_changed', 'wiki_settings_changed',
                                     'worktree_policy_mismatch', 'wiki_root_changed'))
# A concurrent edit, Git operation or busy store: the next wake retries.
SCHEMA3_TRANSIENT_REASONS = frozenset(('checkout_changed', 'fts_deadline_exceeded', 'candidate_busy',
                                       'wiki_admission_changed', 'wiki_card_changed',
                                       'wiki_member_changed', 'wiki_card_unavailable',
                                       'source_file_changed', 'reader_snapshot_deadline'))
_SCHEMA3_ROOT_META = frozenset(('SCHEMA.md', 'index.md', 'log.md'))


def _selected_store_format(decision):
    """Store format of the journal's preferred reader endpoint (v4: ``v6``)."""
    import laya_install_journal as journal
    import laya_journal_reader as readers
    with journal.read_only_journals():
        if journal.journal_version(decision['journalPath']) != 5:
            return 'v6'
        for attempt in range(3):
            path = readers._journal(decision['journalPath'])
            try:
                with readers._readonly(path, path.parent) as db:
                    return readers._selection(db)[1].get('store_format')
            except JournalEvidenceError as exc:
                if str(exc) != 'reader_store_changed' or attempt == 2:
                    raise


def _run_schema3(decision, proof):
    """One lexical publish of the selected schema3 view store's main view.

    Admission is the sync owner plus a worker writer scope over the current
    plan; the plan must still name a schema3 store. No model or v6 runtime
    starts. Returns ``(result, proof)``: a publish advances the journal
    epoch, so ``_after_sync`` rechecks against the proof the pass ended with.
    """
    import laya_install_journal as journal
    import laya_journal_reader as journal_readers
    import laya_reader_windows as windows
    import laya_writer_proof_reader as readers
    import laya_writer_open_plan as plans
    import laya_writer_scope as normal
    from laya_reader_windows import ReaderWindowBusy
    project = Path(decision['projectRoot'])
    try:
        with sync_owner(Path(decision['queuePath'])) as owner:
            if owner is None:
                return {'state': 'deferred', 'reason': 'owner_busy'}, proof
            # The owner re-seals the selected reader when only the project's
            # recall settings changed; recall never does (it refuses until then).
            try:
                resettle = journal_readers.resettle_selection(Path(decision['journalPath']).parent, project,
                                                              _writer_executing_root())
            except JournalEvidenceError as exc:
                resettle = str(exc)
            try:
                try:
                    with journal.read_only_journals():
                        plan = plans.capture_writer_plan(project, executing_root=_writer_executing_root())
                except JournalEvidenceError as exc:
                    if str(exc) == 'writer_windows_not_prepared':
                        raise ResourceDeferred(str(exc)) from exc
                    raise
                if plans.plan_store(plan) is None:
                    raise IndexContractError('selected_store_format_changed')
                try:
                    with normal.worker_writer_scope(plan, queue_owner=owner, operation_timeout=2) as writer:
                        if normal.admission_proof(writer) != proof:
                            raise IndexContractError('selected_proof_changed')
                        result, proof = _publish_schema3(decision, proof, writer)
                        if resettle != 'unchanged':
                            result = {**result, 'readerResettle': resettle}
                        return result, proof
                except ReaderWindowBusy as exc:
                    if str(exc) in ('writer_proof_physical_busy', 'writer_proof_deadline',
                                    'reader_lifetime_busy'):
                        raise ResourceDeferred(str(exc)) from exc
                    raise
            finally:
                try:
                    windows.drain_pending(deadline=time.monotonic()+2)
                finally:
                    readers.drain_proof_readers(deadline=time.monotonic()+2)
                if windows.local_pending_count() or readers.local_pending_proof_readers():
                    raise ResourceDeferred('writer_cleanup_unsettled')
    except ResourceDeferred as exc:
        return _deferred(exc), proof


def _schema3_wiki_cards(root):
    """Sorted card paths under the bound wiki root: regular ``.md`` files only.

    Dot segments, root meta pages and links are not cards; a file the store
    would refuse to admit (not owned, linked, too large) is counted, not read.
    """
    import laya_blob_values as v
    import laya_view_values as view_values
    cards, skipped = [], 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith('.'))
        relative = os.path.relpath(base, root)
        for name in sorted(files):
            if name.startswith('.') or not name.endswith('.md'):
                continue
            path = name if relative == '.' else relative.replace(os.sep, '/') + '/' + name
            if path in _SCHEMA3_ROOT_META:
                continue
            try:
                info = os.lstat(os.path.join(base, name))
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > v.MAX_BODY):
                skipped += 1
                continue
            cards.append(path)
            if len(cards) > view_values.MAX_MEMBERS:
                raise IndexContractError('schema3_wiki_too_many_cards')
    return sorted(cards), skipped


# Linked git worktrees whose own views one pass may publish after the main
# view (``git worktree list`` order). Later ones wait for the next wake.
SCHEMA3_MAX_LINKED = 16
# Refusals that hold one linked worktree's view (its row, settings or wiki
# root no longer match, or registration is refused) without failing the pass.
SCHEMA3_LINKED_HELD = SCHEMA3_BINDING_REASONS | frozenset(('worktree_binding_conflict',
                                                           'worktree_missing', 'wiki_binding_invalid',
                                                           'candidate_io_failed'))


def _publish_schema3(decision, proof, writer):
    """Main view first, then each linked worktree's own view (design 5.2).

    Changed cards only: head members with the same bytes keep their trust. A
    linked worktree is published only while the main view is complete
    (``indexed``/``unchanged``); its refusals hold that view alone.

    Returns ``(result, proof)``; ``proof`` is the writer's fresh admission
    proof once this pass marked its first write.
    """
    import laya_blob_values as v
    import laya_checkout_view as checkout
    import laya_writer_scope as normal
    from laya_view_store import ViewStore
    info = normal.writer_store(writer)
    deadline = time.monotonic() + SCHEMA3_PASS_SECONDS
    store = ViewStore.open(info['path'], repository=info['repository'], scope_id=decision['scopeId'])
    try:
        if (store._incarnation, store._policy) != (info['incarnation'], info['policy_digest']):
            raise IndexContractError('schema3_store_changed')
        marks = {}
        result = _publish_schema3_view(store, writer, decision, proof, info['worktree_id'],
                                       info['worktree_binding'], deadline, marks)
        if result['state'] not in ('indexed', 'unchanged'):
            return result, marks.get('proof', proof)
        linked = []
        for project in _schema3_linked_projects(decision, deadline):
            if time.monotonic() >= deadline - SCHEMA3_PUBLISH_RESERVE:
                linked.append({'root': str(project), 'state': 'pending', 'reason': 'schema3_pass_budget'})
                continue
            try:
                linked_decision = _linked_decision(decision, project)
            except ValueError:
                linked.append({'root': str(project), 'state': 'held',
                               'reason': 'linked_root_outside_project'})
                continue
            try:
                worktree_id = _schema3_linked_worktree(store, writer, proof, info, project,
                                                       deadline, marks)
                outcome = _publish_schema3_view(store, writer, linked_decision,
                                                proof, worktree_id, None, deadline, marks)
            except checkout.CheckoutUnavailable as exc:
                outcome = {'state': 'held', 'reason': exc.reason}
            except v.BlobStoreError as exc:
                if exc.reason not in SCHEMA3_LINKED_HELD:
                    raise
                outcome = {'state': 'held', 'reason': exc.reason}
            if outcome['state'] == 'deferred' and outcome['reason'] in (
                    'schema3_binding_changed', 'schema3_mass_removal_held'):
                # Retrying cannot clear these; that view keeps its last head.
                outcome = {**outcome, 'state': 'held'}
            linked.append({'root': str(project), **{k: outcome[k] for k in
                           ('state', 'reason', 'generation', 'ingested', 'pending') if k in outcome}})
    finally:
        store.close()
    proof = marks.get('proof', proof)
    if not linked:
        return result, proof
    result = {**result, 'worktrees': linked}
    if result['state'] == 'unchanged' and any(item['state'] == 'indexed' for item in linked):
        result['state'] = 'indexed'
    if any(item['state'] in ('pending', 'deferred') for item in linked):
        result['state'] = 'pending'
    return result, proof


def _schema3_linked_projects(decision, deadline):
    """Linked worktrees of the main checkout that the route selector serves from it.

    Each listed worktree's project must classify as ``worktree`` of exactly
    this main project (same common Git dir, no route of its own, same derived
    settings identity: ``laya_index_host_route._worktree_route``).
    """
    import git_branch_safety
    project = Path(decision['projectRoot'])
    try:
        facts = git_branch_safety.repo_facts(project, deadline=deadline)
        if facts is None or facts['linked']:
            return []
        relative = project.relative_to(facts['toplevel'])
        out = git_branch_safety.run_git(['worktree', 'list', '--porcelain', '-z'], project,
                                        deadline=deadline)
    except (git_branch_safety.GitUnavailable, OSError, ValueError):
        return []
    listed = [field[len('worktree '):] for field in
              out.stdout.decode('utf-8', 'surrogateescape').split('\0')
              if field.startswith('worktree ')]
    found = []
    for root in listed[1:]:
        if len(found) >= SCHEMA3_MAX_LINKED:
            break
        try:
            candidate = Path(root).resolve(strict=True) / relative
            if not candidate.is_dir():
                continue
            route = select_route(str(candidate), action='index')
        except (OSError, RuntimeError, ValueError):
            continue
        main = route.get('mainRoute')
        if (route.get('state') == 'worktree' and isinstance(main, dict)
                and main.get('projectRoot') == str(project)
                and route.get('worktreeProjectRoot') == str(candidate)):
            found.append(candidate)
    return found


def _schema3_linked_worktree(store, writer, proof, info, project, deadline, marks):
    """The linked worktree's registered id, registering it on first sight.

    Registration is a store write, so the pass's journal fence comes first.
    The record is built by ``laya_checkout_view.register_worktree`` and bound
    by ``ViewStore.register_worktree`` (no rebinding of an existing row).
    """
    import laya_checkout_view as checkout
    with store._session() as db:
        rows = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                          (str(project),)).fetchall()
    if len(rows) > 1 or any(row[0] == info['worktree_id'] for row in rows):
        raise IndexContractError('schema3_worktree_rows_invalid')
    if rows:
        return rows[0][0]
    _mark_schema3_write(writer, proof, marks)
    record = checkout.register_worktree(str(project), info['repository'], deadline=deadline)
    return store.register_worktree(str(project), record)


def _linked_decision(decision, project):
    """The main decision with its collection roots rebased onto a linked worktree.

    Same derived settings identity (proved by the selector); only
    ``projectRoot`` and the roots move, as the store's attested-ingest rule
    binds the decision to that worktree row's project and wiki root.
    """
    main = Path(decision['projectRoot'])

    def rebased(root):
        return str(project / Path(root).relative_to(main))
    return {**decision, 'projectRoot': str(project),
            'collections': [{**item, 'sourceRoot': rebased(item['sourceRoot'])}
                            for item in decision['collections']],
            'collectionRoots': {name: rebased(root)
                                for name, root in decision.get('collectionRoots', {}).items()}}


def _mark_schema3_write(writer, proof, marks):
    """The pass's first store write: one journal fence (idempotent after the first).

    ``proof`` is the pass's admission proof; the fresh one is kept in
    ``marks['proof']`` for ``_after_sync``.
    """
    import laya_writer_scope as normal
    fresh = normal.mark_new_write(writer)
    if (fresh.get('selectionEpoch') != proof['selectionEpoch'] + 1
            or fresh.get('productRouteAllowed') is not True):
        raise IndexContractError('new_write_proof_unconfirmed')
    marks['proof'] = fresh


def _publish_schema3_view(store, writer, decision, proof, worktree_id, binding, deadline, marks):
    """One view's pass: changed cards ingested, unchanged members keep their trust.

    ``binding`` is the main worktree's journal-recorded binding (None for a
    linked worktree, whose row the store itself re-observes). A view with no
    head yet (a first-seen worktree or branch) ingests every card: trust is
    never reused from another view (``ViewStore._card``: ``foreign_card_trust``).
    """
    import laya_blob_values as v
    import laya_writer_scope as normal
    import wiki_lifecycle
    from laya_blob_store import _regular
    import laya_view_values as view_values
    from laya_view_store import _skip_patterns
    import uuid
    try:
        handle = store.capture_view(worktree_id, deadline=deadline)
        head = store.head(handle)
        cold = head['gen_id'] is None
        with store._session() as db:
            row = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            # A cold view publishes nothing until complete; its progress is
            # the newest generation it prepared and never published.
            base = (head['gen_id'] if not cold else next(iter(db.execute(
                "SELECT gen_id FROM generations WHERE view_id=? AND state='prepared' "
                'ORDER BY ordinal DESC LIMIT 1', (handle.view_id,)).fetchone() or ()), None))
            old = {} if base is None else {m['path']: m for m in store._stored(db, base)[0]}
        if row is None or (binding is not None and row['binding_digest'] != binding):
            v.fail('wiki_binding_changed')
        root = row['wiki_root']
        found, skipped = _schema3_wiki_cards(root)
        # skipPaths is read each pass, outside the binding: a card it newly
        # covers leaves the generation (not a file removal), one it no longer
        # covers is ingested like a new card.
        skips = _skip_patterns(dict(row))
        paths = [p for p in found if not view_values.skipped(p, skips)]
        removed = sorted(set(old) - set(found))
        excluded = sorted(set(old) & (set(found) - set(paths)))
        current = {}
        for path in paths:
            try:
                raw, _node, _root = _regular(root, path, v.MAX_BODY)
            except OSError:
                v.fail('wiki_card_unavailable')
            current[path] = (v.hash_bytes(raw), raw)
        changed = {p for p in paths if p not in old or old[p]['blob_sha'] != current[p][0]}
        if (len(removed) > wiki_lifecycle.MASS_DELETE_MIN
                and len(removed) > wiki_lifecycle.MASS_DELETE_FRACTION * len(old)):
            return {'state': 'deferred', 'reason': 'schema3_mass_removal_held',
                    'storeFormat': 'schema3', 'removed': len(removed)}
        if not changed and not removed and not excluded and not cold:
            return {'state': 'unchanged', 'storeFormat': 'schema3', 'generation': head['gen_id']}
        # First store write of this pass: the journal fence comes first.
        _mark_schema3_write(writer, proof, marks)
        scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
        members, pending, ingested = {}, 0, 0
        for path in paths:
            sha, raw = current[path]
            if path not in changed:
                members[path] = {k: old[path][k] for k in ('path', 'blob_sha', 'display_title',
                                                            'trust_digest')}
                continue
            if (pending or ingested >= SCHEMA3_MAX_INGEST
                    or time.monotonic() >= deadline - SCHEMA3_PUBLISH_RESERVE):
                # Left for the next wake; the published head keeps its old bytes.
                pending += 1
                if path in old:
                    members[path] = {k: old[path][k] for k in ('path', 'blob_sha', 'display_title',
                                                                'trust_digest')}
                continue
            admitted = store.ingest_attested_card(worktree_id, path, decision=decision,
                                                  provenance_scan=scan)
            ingested += 1
            body = (raw.decode('utf8') if admitted['blob_sha'] == sha
                    else store.document(admitted['blob_sha'])['body'])
            members[path] = dict(path=path, blob_sha=admitted['blob_sha'],
                                 display_title=v.markdown_title(body, admitted['blob_sha']),
                                 trust_digest=admitted['trust_digest'])
        if pending and not ingested and not removed and not excluded:
            return {'state': 'deferred', 'reason': 'schema3_pass_budget', 'storeFormat': 'schema3'}
        manifest = dict(scan_id=scan['id'], policy_digest=handle.policy_digest, revocation_epoch=0,
                        complete=True, sources=[], members=[members[p] for p in sorted(members)])
        generation = store.prepare_generation(handle, manifest, expected_head=head, deadline=deadline)
        normal.validate_normal_writer(writer)
        if cold and pending:
            # No partial first generation: recall answers view_preparing until complete.
            return {'state': 'pending', 'reason': 'schema3_view_preparing', 'storeFormat': 'schema3',
                    'generation': None, 'prepared': generation, 'ingested': ingested,
                    'removed': len(removed), 'pending': pending, 'skipped': skipped}
        store.publish_generation(handle, generation, expected_head=head, deadline=deadline)
    except v.BlobStoreError as exc:
        if exc.reason in SCHEMA3_BINDING_REASONS:
            return {'state': 'deferred', 'reason': 'schema3_binding_changed',
                    'detail': exc.reason, 'storeFormat': 'schema3'}
        if exc.reason in SCHEMA3_TRANSIENT_REASONS:
            return {'state': 'deferred', 'reason': exc.reason, 'storeFormat': 'schema3'}
        raise
    return {'state': 'pending' if pending else 'indexed', 'storeFormat': 'schema3',
            'generation': generation, 'ingested': ingested, 'removed': len(removed),
            'pending': pending, 'skipped': skipped}


def _after_sync(decision, proof, grants, result, *, store_format='v6'):
    """External-edit drain and wiki discovery after a completed sync, on every sync path.

    After receipt and ACK, with the model service stopped. A raised error or
    a retry keeps this wake; the published head is not touched. ``proof`` is
    the one the sync ended with (the capacity path refreshes it at its
    fence). A blocked run, a burst skipped over the pending cap, or a run
    that started the cursor (earlier changes are not discovered) is reported
    as ``wikiDiscovery``.

    A schema3 selection opens no v6 component: discovery takes the view
    head this pass published or found (``result['generation']``) as its
    cursor generation instead of the v6 lineage read.
    """
    if result.get('state') not in ('indexed', 'unchanged'):
        return result
    schema3 = store_format == 'schema3'

    import laya_install_journal as journal
    from contextlib import nullcontext
    with journal.read_only_journals():
        v5 = journal.journal_version(decision['journalPath']) == 5
    try:
        with (sync_owner(Path(decision['queuePath'])) if v5 else nullcontext(None)) as owner:
            if v5 and owner is None:
                return {**result, 'state': 'deferred', 'reason': 'owner_busy'}

            def recheck():
                current = (_owned_selected_proof(decision, owner) if v5
                           else verify_selected_route(decision))
                if current != proof:
                    raise IndexContractError('selected_proof_changed')
            import laya_external_edits
            if v5:
                from wiki_selected_laya import _owner_scope
            with (_owner_scope((decision,proof),owner) if v5 else nullcontext()):
                external = (laya_external_edits.drain(decision, recheck=recheck, owner=owner)
                            if v5 else laya_external_edits.drain(decision, recheck=recheck))
                if isinstance(external, dict) and external.get('remaining', 0):
                    return {**result, 'state': 'pending', 'reason': 'external_edits_pending'}
                if grants is None:
                    return result
                import laya_wiki_discovery
                if schema3:
                    outcome = laya_wiki_discovery.drain_after_sync(
                        decision, grants, recheck=recheck, view_generation=result.get('generation'))
                elif v5:
                    with _discovery_base_component(decision,owner,proof) as component:
                        outcome = laya_wiki_discovery.drain_after_sync(
                            decision,grants,recheck=recheck,coordination=component)
                else:
                    outcome = laya_wiki_discovery.drain_after_sync(decision, grants, recheck=recheck)
    except ResourceDeferred as exc:
        return {**result, **_deferred(exc)}
    if outcome['state'] == 'retry':
        return {'state': 'deferred', 'reason': 'wiki_discovery_' + outcome['reason'],
                'generation': result.get('generation')}
    if (outcome['state'] == 'blocked' or outcome.get('reason') == 'pending_over_cap_skipped'
            or outcome.get('cursorStarted') is True or 'initialBackfill' in outcome):
        result = {**result, 'wikiDiscovery': outcome}
    return result


# Shadow migration driver (design 8.2, U6). After a completed sync of a
# selected main checkout, one pass advances the private schema3 candidate by
# at most SHADOW_STEPS_PER_PASS steps: activation record -> candidate store
# -> begin_migration -> advance_shadow ... -> shadow_verified. The selected
# reader never changes here; reader_ready/switched belong to the
# ``laya_shadow_migration.advance_reader_ready`` entry (U15), called once a
# pass when it exists. Every step is crash-safe on its own: a SIGKILL leaves
# the activation record, the candidate file or the intent, and the next pass
# resumes from whichever exists.
# Provisional bounds (K8, measured by U8). The pass budget must hold one
# lock-free prepare (about 25 s for 3,749 synthetic cards).
SHADOW_STEPS_PER_PASS = 4
SHADOW_PASS_SECONDS = 60.0
# ``off`` stops the driver before it reads or writes anything.
SHADOW_KILL_SWITCH = 'MEMVANE_SHADOW_MIGRATION'
SHADOW_ACTIVATION = 'shadow-migration-activation.json'
SHADOW_CANDIDATE = 'shadow-candidate.sqlite'


def _shadow_gate(project, executing_root, *, prove=True):
    """``(decision, state)`` when the driver may act, else ``(None, reason)``.

    Read-only. Each step reacquires the install and operation locks and
    reproves route, endpoint (bundle included) and intent itself; this gate
    only keeps a pass from starting or continuing on a route it must not
    touch. ``prove=False`` (between steps of one pass) skips the full bundle
    proof the previous step already repeated.
    """
    if os.environ.get(SHADOW_KILL_SWITCH) == 'off':
        return None, 'disabled'
    import project_dir
    if project_dir.layout(project) == 'split':
        return None, 'project_dir_split'
    import laya_install_journal as journal
    with journal.read_only_journals():
        # A linked worktree classifies as ``worktree``, never ``candidate``.
        decision = select_route(str(project), action='recall')
        if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
            return None, 'route_not_selected'
        if journal.journal_version(decision['journalPath']) != 5:
            return None, 'journal_not_v5'
    # An install that already reads a schema3 store is past this migration;
    # its intent, candidate and activation record stay as they are.
    selected = _selected_store_format(decision)
    if selected != 'v6':
        return None, 'reader_schema3_selected' if selected == 'schema3' else 'reader_selection_unproved'
    state = Path(decision['journalPath']).parent
    import laya_ledger_common as ledger
    if ledger.qmd_ledger_present(state):
        return None, ledger.QMD_ORIGIN_DETACH_REQUIRED
    import laya_fresh_wiki_origin as fresh
    if fresh.upgrade_fenced(str(project)):
        return None, 'upgrade_fenced'
    if not prove:
        return (decision, state), None
    try:
        with journal.read_only_journals():
            proof = journal.verify_selected_route(decision)
    except Exception as exc:
        return None, 'bundle_drift' if journal.is_bundle_drift(exc) else 'selected_proof_unavailable'
    if (proof.get('productRouteAllowed') is not True
            or proof.get('executingPluginRoot') != str(executing_root)):
        return None, 'selected_proof_unavailable'
    return (decision, state), None


def _shadow_open(state, project, scope):
    """Open the private candidate; its own store_meta and worktrees are the SSOT."""
    from laya_journal_reader import _readonly
    from laya_view_store import ViewStore
    path = state / SHADOW_CANDIDATE
    with _readonly(path, state) as db:
        meta = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
        rows = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                          (str(project),)).fetchall()
    if meta is None or not isinstance(meta[0], str) or len(rows) != 1:
        raise IndexContractError('migration_candidate_invalid')
    return ViewStore.open(path, repository=json.loads(meta[0]), scope_id=scope), rows[0][0]


def _shadow_step(decision, state, executing_root, store, deadline):
    """Run one driver step; returns ``(stage, migration_state, store, worktree_id)``."""
    import laya_ledger_common as ledger
    import laya_shadow_migration as migration
    project = Path(decision['projectRoot'])
    activation, candidate = state / SHADOW_ACTIVATION, state / SHADOW_CANDIDATE
    if not os.path.lexists(activation):
        ledger._write(activation, {'schema': 'memvane-shadow-activation-v1', 'project': str(project),
                                   'state': str(state), 'candidate': str(candidate),
                                   'activated_ns': time.time_ns()}, replace=False)
        return 'activate', None, store
    record = ledger._read_json(activation)
    if (not isinstance(record, dict) or record.get('project') != str(project)
            or record.get('state') != str(state) or record.get('candidate') != str(candidate)):
        raise IndexContractError('migration_activation_invalid')
    if not os.path.lexists(candidate):
        import laya_checkout_view as checkout
        from laya_view_store import ViewStore
        repository = checkout.register_repository(str(project))
        worktree = checkout.register_worktree(str(project), repository)
        ViewStore.create(candidate, project_root=str(project), repository=repository,
                         worktree=worktree, scope_id=decision['scopeId']).close()
        return 'create', None, store
    if store is None:
        store = _shadow_open(state, project, decision['scopeId'])
    view, worktree_id = store
    if not os.path.lexists(state / migration._INTENT):
        intent = migration.begin_migration(view, worktree_id=worktree_id, project=project,
                                           executing_root=executing_root, deadline=deadline)
        return 'begin', intent['migration_state'], store
    result = migration.advance_shadow(view, project=project, executing_root=executing_root,
                                      deadline=deadline)
    return result['stage'], result['migration_state'], store


def shadow_pass(project, *, executing_root=None):
    """One bounded shadow migration pass for a selected main checkout.

    Returns ``{state, reason, steps, stages, migration_state}``. ``skipped``:
    a guard refused and nothing was read from or written to the migration.
    ``progressed``: the step cap was reached, or the head is verified and the
    reader_ready entry is absent. ``waiting``: a step asked for a later retry,
    the pass budget or a guard stopped it. ``error``: a step raised; only its
    fixed code is kept. A pass past the guard emits one ``migration.driver``
    event.
    """
    import event_log
    import laya_shadow_migration as migration
    project = Path(project)
    executing_root = Path(_writer_executing_root() if executing_root is None else executing_root)
    bound, reason = _shadow_gate(project, executing_root)
    status = {'state': 'skipped', 'reason': reason, 'steps': 0, 'stages': [], 'migration_state': None}
    if bound is None:
        return status
    decision, state = bound
    started = time.monotonic()
    deadline = started + SHADOW_PASS_SECONDS
    status['state'], status['reason'] = 'progressed', None
    store = None
    try:
        while status['steps'] < SHADOW_STEPS_PER_PASS:
            if status['steps']:
                if deadline - time.monotonic() < migration._STEP_SECONDS:
                    status.update(state='waiting', reason='pass_deadline')
                    break
                again, reason = _shadow_gate(project, executing_root, prove=False)
                if again != bound:
                    status.update(state='waiting', reason=reason or 'route_changed')
                    break
            ready = getattr(migration, 'advance_reader_ready', None)
            if status['migration_state'] == 'shadow_verified':
                # The head was re-proved by this pass; U15 continues from it.
                result = ready(store[0], project=Path(decision['projectRoot']),
                               executing_root=executing_root, deadline=deadline)
                stage, current = result['stage'], result['migration_state']
            else:
                stage, current, store = _shadow_step(decision, state, executing_root, store, deadline)
            status['steps'] += 1
            status['stages'].append(stage)
            if current is not None:
                status['migration_state'] = current
            if stage == 'retry':
                status.update(state='waiting', reason='step_retry')
                break
            if stage == 'reader_ready' or (current == 'shadow_verified' and not callable(ready)):
                break
    except Exception as exc:
        status.update(state='error', reason=event_log.code(exc))
    finally:
        if store is not None:
            store[0].close()
        event_log.emit('migration.driver', state_root=state, host='worker', outcome=status['state'],
                       reason=status['reason'], ms=(time.monotonic() - started) * 1000,
                       steps=status['steps'], migration_state=status['migration_state'])
    return status


def _checked(result):
    # The dispatcher completes the wake only on indexed/unchanged
    # (runtime_handlers.laya_sync); pending and blocked are retried.
    if result.get('state') not in ('indexed', 'unchanged', 'deferred', 'pending', 'blocked'):
        raise IndexContractError('invalid_sync_result')
    return result


def _run_capacity(decision, proof, source_root, capacity, provider, controller,
                  observed, start_service):
    """One wake on the capacity path.

    Admission is the same provider lane as the full-scan path, but the model
    service starts only when the fill has a file to embed, so a wake with no
    new content loads no model. The journal new-write fence runs once per
    wake: later publishes in the same wake only re-check the refreshed proof,
    so concurrent hook queries see at most one epoch change.
    """
    from contextlib import ExitStack
    from laya_reader_windows import ReaderWindowBusy
    state = {'proof': proof, 'service': None, 'fenced': False, 'writer': None, 'runtime': None}
    owner = None

    def proof_ok():
        return _owned_selected_proof(decision, owner) == state['proof']

    def before_publish():
        if not proof_ok():
            raise IndexContractError('selected_proof_changed')
        if state['fenced']:
            return
        current = state['proof']
        if state['writer'] is not None:
            import laya_writer_scope as normal
            fresh = normal.mark_new_write(state['writer'])
            if (fresh.get('selectionEpoch') != current['selectionEpoch'] + 1 or
                    fresh.get('productRouteAllowed') is not True):
                raise IndexContractError('new_write_proof_unconfirmed')
            state['proof'] = fresh
            from laya_publication_revision import ensure_base_baseline
            ensure_base_baseline(state['runtime'].index, scope=decision['scopeId'],
                                 profile_id=fresh['vectorProfile']['profileId'], deadline=time.monotonic()+2)
            state['fenced'] = True
            return
        marked = cas_mark_new_write(decision['journalPath'], current['selectionEpoch'])
        if (marked.get('epoch') != current['selectionEpoch'] + 1 or
                marked.get('newWriteSeen') is not True):
            raise IndexContractError('new_write_intent_unconfirmed')
        fresh = _owned_selected_proof(decision, owner)
        if (fresh.get('selectionEpoch') != marked['epoch'] or
                fresh.get('productRouteAllowed') is not True):
            raise IndexContractError('new_write_proof_unconfirmed')
        state['proof'] = fresh
        state['fenced'] = True

    try:
        with ExitStack() as stack:
            stack.enter_context(controller.activity('provider', EMBED_POLICY, observed))
            owner = stack.enter_context(sync_owner(Path(decision['queuePath'])))
            if owner is None:
                return {'state': 'deferred', 'reason': 'owner_busy'}, state['proof']

            def ensure_service():
                if state['service'] is not None:
                    return
                # The full native closure is rehashed before any model load.
                validated = _owned_worker_attestation(decision, owner, state['proof'])
                if validated != state['proof']['providerManifest']:
                    raise IndexContractError('selected_provider_proof_changed')

                def admit():
                    fresh = resource_report(EMBED_POLICY, detailed=True)
                    return controller.check(EMBED_POLICY, fresh)['ready']
                service = _selected_service(decision, validated, admit, start_service)
                stack.callback(service.stop)
                state['service'] = service
                if not proof_ok():
                    raise IndexContractError('selected_proof_changed')

            class AdmittedProvider:
                manifest = provider.manifest
                profile = provider.profile

                def embed_document_chunks(self, body, path):
                    if _higher_priority_wake(controller):
                        raise ResourceDeferred('laya_sync_yield_priority')
                    ensure_service()
                    return _embed_with_budget(provider, body, path)

            with _full_worker_runtime(decision, owner, state['proof'], source_root,
                                      capacity=capacity) as (runtime, writer), ExitStack() as sidecars:
                state['writer'] = writer
                state['runtime'] = runtime
                # request_scan and first sidecar creation both mutate stores.
                if writer is not None:
                    before_publish()
                publish_complete = None
                preserve_previous = None
                if 'collections' in decision:
                    from laya_incremental_docs import (PublishedReader, publish_document,
                                                       publish_needed,
                                                       preserve_previous_head,
                                                       retire_missing_documents)
                    incremental_identity = {
                        'projectRoot': decision['projectRoot'],
                        'stateRoot': str(Path(decision['journalPath']).parent),
                        'installId': decision['installId'],
                        'scopeId': decision['scopeId'],
                        'collections': decision['collections'],
                        'candidateDb': decision['dbPath'],
                        'dbIncarnation': decision['dbIncarnation'],
                        'vectorProfileId': decision['vectorProfile']['profileId'],
                    }

                    coordination = None
                    if writer is not None:
                        from laya_reader_windows import (component_for, _component,
                                                         create_sidecar_owned, close_owned)
                        coordination = component_for(writer, 'sidecar')
                        if _component(coordination)[3] is None:
                            created = create_sidecar_owned(coordination, deadline=time.monotonic()+2)
                            sidecars.callback(lambda: close_owned(created, deadline=time.monotonic()+2))
                            coordination = component_for(writer, 'sidecar')
                        # Seal the preexisting sidecar before catalog progress,
                        # even if this wake never publishes a document.
                        from laya_incremental_docs import _sidecar_open, _sidecar_setup, _close
                        try:
                            baseline_db = _sidecar_open(incremental_identity, coordination)
                        except JournalEvidenceError as exc:
                            if str(exc) != 'normal_writer_sidecar_identity_changed':
                                raise
                            # A crash during first creation can leave a DELETE
                            # sidecar. Its existing recovery validates that it
                            # is empty before converting it to WAL.
                            from laya_sidecar_create import resume_empty_sidecar_owned
                            resumed = resume_empty_sidecar_owned(
                                coordination, identity=incremental_identity, deadline=time.monotonic()+2)
                            sidecars.callback(lambda: close_owned(resumed, deadline=time.monotonic()+2))
                            baseline_db = _sidecar_open(incremental_identity, coordination)
                        # Keep the managed writer alive for this wake. Closing
                        # the last WAL connection here removes its auxiliaries
                        # before PublishedReader's coordinated RO admission.
                        sidecars.callback(_close, baseline_db)
                        _sidecar_setup(baseline_db, incremental_identity, coordination)
                        if not proof_ok():
                            raise IndexContractError('selected_proof_changed')
                    published = PublishedReader(incremental_identity, coordination=coordination)
                    sidecars.callback(published.close)

                    def publish_complete(snapshot, path, sha, title, body, profile_id):
                        # Every wake rescans, so the pending fill offers each
                        # cached member again. A member that would write
                        # nothing (identical row, or an unverified card) skips
                        # the route proof, the journal fence and the sidecar
                        # transaction; the full scan still covers its bytes.
                        if not publish_needed(published, path, sha, title, body,
                                              profile_id, identity=incremental_identity,
                                              require_verified=True):
                            return
                        if not proof_ok():
                            raise IndexContractError('selected_proof_changed')
                        # This is a durable searchable write, even if the full
                        # generation has not published yet. Cross the selected
                        # journal's first-write fence before the sidecar commit.
                        before_publish()
                        publish_document(runtime, incremental_identity, snapshot, path,
                                         sha, title, body, profile_id,
                                         require_verified=True, coordination=coordination)

                    def preserve_previous(index, binding, job):
                        if not proof_ok():
                            raise IndexContractError('selected_proof_changed')
                        preserve_previous_head(index, incremental_identity,
                                               job['base_gen'], job['gen'], coordination=coordination)
                        if not proof_ok():
                            raise IndexContractError('selected_proof_changed')

                result = _sync_capacity(runtime, AdmittedProvider(), before_publish,
                                        controller, lambda: not proof_ok(),
                                        diagnose=_member_diagnosis(decision, provider),
                                        owner=owner,
                                        on_document_complete=publish_complete,
                                        before_head_publish=preserve_previous,
                                        retire_missing=(
                                            lambda: retire_missing_documents(
                                                runtime, incremental_identity,
                                                before_write=before_publish, coordination=coordination)
                                            if publish_complete is not None else None))
            if not proof_ok():
                raise IndexContractError('selected_proof_changed')
    except ResourceDeferred as exc:
        return _deferred(exc), state['proof']
    except ReaderWindowBusy as exc:
        if getattr(exc, 'rollback_pending', False) or str(exc) not in (
                'reader_write_deadline', 'reader_publish_busy',
                'reader_mutation_busy', 'reader_lifetime_busy', 'reader_physical_busy'):
            raise
        return {'state': 'deferred', 'reason': str(exc)}, state['proof']
    return _checked(result), state['proof']


def _observe_member_block(decision, result):
    """Track a fill stuck on one member and log it; never fails the sync.

    ``laya_vector_block`` keeps the streak for the SessionStart notice; each
    wake that ends on a member verdict also logs ``vector.member blocked``
    with the card path, hash, cause code and the streak's wake count.
    """
    try:
        import laya_vector_block
        row = laya_vector_block.observe(laya_vector_block.state_root(decision), result)
        if (row is None or not isinstance(result, dict)
                or result.get('reason') not in laya_vector_block.MEMBER_REASONS):
            return
        import event_log
        event_log.emit('vector.member', outcome='blocked', reason=result.get('reason'),
                       decision=decision, host='worker', session=None,
                       path=row['path'], hash=row['hash'], cause=row.get('cause'),
                       wakes=row['wakes'])
    except Exception:
        pass


def _member_diagnosis(decision, provider):
    """``diagnose(body, path, sha)`` for the fill: a short cause code.

    The diagnosis repeats the refused embedding once in a separate process
    (``laya_vector_diagnose``), within this wake's admission and with the
    same request timeout. The attempt is recorded before the wake ends,
    whatever it found, so a streak embeds its member for diagnosis at most
    once; later wakes reuse the recorded cause.
    """
    def diagnose(body, path, sha):
        import laya_vector_block
        import laya_vector_diagnose
        root = laya_vector_block.state_root(decision)
        known = laya_vector_block.diagnosis_of(root, path, sha)
        if known is not None:
            return known
        try:
            # Record the attempt first: a crash mid-diagnosis must not repeat it.
            laya_vector_block.mark_diagnosed(root, path, sha, 'unknown')
        except Exception:
            return 'unknown'
        cause = laya_vector_diagnose.diagnose(
            provider.manifest, body, path,
            timeout=270 if _large_initial_document(body) else 120)
        try:
            laya_vector_block.mark_diagnosed(root, path, sha, cause)
        except Exception:
            pass
        return cause
    return diagnose


def _sync_capacity(runtime, provider, before_publish, controller, proof_changed,
                   on_document_complete=None, before_head_publish=None,
                   retire_missing=None, diagnose=None, owner=None):
    """Bounded scan, per-document fill, and full-generation rounds for one wake.

    The wake is the change signal (edits, Bash and SessionStart request it),
    so it first asks the catalog for a scan; a pending scan is marked to run
    once more. ``sync_step`` never loads a model; it asks for
    ``vector_backlog`` until the fill has cached every member, then seals and
    publishes. Individually hashed members are also published while the scan
    is pending; those cached chunk sets are reused by the full seal.
    """
    from laya_vector_capacity import (provider_profile_id, vector_fill_step,
                                      pending_vector_fill_step)
    profile_id = provider_profile_id(provider)

    def should_yield():
        return _higher_priority_wake(controller)
    if runtime.request_scan().get('status') == 'busy':
        return {'state': 'deferred', 'reason': 'catalog_busy'}
    result = None
    for _ in range(MAX_CAPACITY_ROUNDS):
        if should_yield():
            raise ResourceDeferred('laya_sync_yield_priority')
        if proof_changed():
            raise IndexContractError('selected_proof_changed')
        result = sync_step(runtime, before_publish=before_publish, require_vector=True,
                           vector_profile_id=profile_id,
                           before_head_publish=before_head_publish, owner=owner)
        if result.get('reason') == 'scan_pending' and on_document_complete is not None:
            partial = pending_vector_fill_step(runtime, provider,
                                               should_yield=should_yield,
                                               before_write=before_publish,
                                               on_document_complete=on_document_complete,
                                               diagnose=diagnose)
            if partial['state'] in ('blocked', 'deferred'):
                return {key: partial[key] for key in
                        ('state', 'reason', 'path', 'detail', 'hash', 'cause')
                        if key in partial}
        if result['state'] != 'pending':
            if (retire_missing is not None and result['state'] in ('indexed', 'unchanged')):
                retire_missing()
            return result
        if result.get('reason') == 'vector_backlog':
            fill = vector_fill_step(runtime, provider, should_yield=should_yield,
                                    on_document_complete=on_document_complete,
                                    before_write=before_publish, diagnose=diagnose)
            if fill['state'] in ('deferred', 'blocked'):
                return {key: fill[key] for key in
                        ('state', 'reason', 'path', 'detail', 'hash', 'cause') if key in fill}
    return result


# -- inactive initial build ---------------------------------------------------

def _inactive_journal(identity):
    """The pending, unselected journal this build was registered against."""
    journal = inspect_journal(identity['journalPath'])
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None
            or journal['pendingRouteSha256'] != identity['routeSha256']
            or journal['newWriteSeen']):
        raise IndexContractError('initial_build_journal_changed')
    return journal['epoch'], journal['pendingRouteSha256']


def _still_unselected(project):
    """The project selects QMD with no route record and no cutover marker."""
    decision = select_route(str(project), action='update')
    return (decision.get('state'), decision.get('reason')) == ('legacy', 'no_route_record')


def run_initial_build(project, record, *, coordinator=None, report=None,
                      start_service=start_owned_embed_service, clock=time.monotonic):
    """One bounded step of an inactive initial build; never selects a route.

    Admission is the provider lane of the selected path, and the model starts
    only when the fill has an uncached member, under a fresh resource sample
    per member. The build writes only its private candidate stores, so the
    publish fence checks that the journal is still the pending, unselected
    one and the project still selects QMD, instead of marking a new write.

    Answers follow the dispatcher contract: ``indexed``/``unchanged`` (ACK)
    only once the build is ``sealed`` (or already ``bound``); ``pending``,
    ``deferred`` and ``blocked`` keep the wake. The phase is recorded in the
    build record under the build owner lock.
    """
    identity = record['identity']
    if record['phase'] == 'upgrading':
        return {'state': 'deferred', 'reason': 'inactive_bundle_upgrade',
                'initialBuild': 'upgrading'}
    if record['phase'] == 'retired':
        # An explicitly abandoned, never-started build: nothing to step; ACK.
        return {'state': 'unchanged', 'reason': 'initial_build_retired',
                'initialBuild': 'retired'}
    if record['phase'] == 'bound':
        return {'state': 'unchanged', 'reason': 'initial_build_bound', 'initialBuild': 'bound'}
    if record['phase'] == 'sealed':
        from laya_ledger_common import qmd_ledger_present
        from laya_fresh_wiki_origin import LEDGER as FRESH_LEDGER
        if (qmd_ledger_present(identity['stateRoot']) or
                (Path(identity['stateRoot']) / FRESH_LEDGER).exists()):
            return _checked({'state': 'deferred', 'reason': 'initial_build_bind_unsettled',
                             'initialBuild': 'sealed'})
    if not _still_unselected(project):
        decision = select_route(str(project), action='update')
        if (record['phase'] == 'sealed' and (decision.get('state'), decision.get('reason'))
                == ('legacy', 'qmd_selection_verified')):
            # finalize bound this sealed candidate (prepared cutover marker,
            # QMD still selected) but stopped before recording ``bound``. The
            # build must not move under that binding; a repeated finalize
            # settles it. Keep the wake without touching any store.
            return _checked({'state': 'deferred', 'reason': 'initial_build_bind_unsettled',
                             'initialBuild': 'sealed'})
        raise IndexContractError('initial_build_project_selected')
    from contextlib import ExitStack
    import laya_initial_build as initial_build
    from laya_vector_capacity import provider_profile_id
    with initial_build.owner(identity['stateRoot']) as owned:
        if not owned:
            return {'state': 'deferred', 'reason': 'initial_build_busy'}
        record = initial_build.read(identity['stateRoot'])
        if record is not None and record['phase'] == 'upgrading':
            return {'state': 'deferred', 'reason': 'inactive_bundle_upgrade',
                    'initialBuild': 'upgrading'}
        if record is None or record['identity'] != identity:
            raise IndexContractError('initial_build_identity_changed')
        if record['phase'] == 'retired':
            return {'state': 'unchanged', 'reason': 'initial_build_retired',
                    'initialBuild': 'retired'}
        if record['phase'] == 'bound':
            return {'state': 'unchanged', 'reason': 'initial_build_bound',
                    'initialBuild': 'bound'}
        if record['phase'] == 'sealed':
            from laya_ledger_common import qmd_ledger_present
            from laya_fresh_wiki_origin import LEDGER as FRESH_LEDGER
            if (qmd_ledger_present(identity['stateRoot']) or
                    (Path(identity['stateRoot']) / FRESH_LEDGER).exists()):
                # Finalize may have written the durable ledger and died before
                # its project marker. The selector still says plain QMD then,
                # but stepping this candidate could invalidate that binding.
                # Repeated finalize settles it; no model or store is touched.
                return _checked({'state': 'deferred',
                                 'reason': 'initial_build_bind_unsettled',
                                 'initialBuild': 'sealed'})
        baseline = _inactive_journal(identity)
        # Bundle identity, attestation and provider approval, before any model.
        approval = preflight_product_candidate(identity['journalPath'],
                                               require_executing=True)
        if (approval['attestationSha256'] != identity['attestationSha256']
                or approval['pendingRouteSha256'] != identity['routeSha256']):
            raise IndexContractError('initial_build_attestation_changed')
        provider = ReviewedLocalQmdProvider(approval['providerManifest'])
        if (provider.profile['profile_id'] != identity['vectorProfileId']
                or provider_profile_id(provider) != identity['vectorProfileId']):
            raise IndexContractError('initial_build_provider_profile_mismatch')
        controller = coordinator or Coordinator()
        observed = report if report is not None else resource_report(EMBED_POLICY,
                                                                      detailed=True)
        state = {'service': None}

        def proof_ok():
            return _inactive_journal(identity) == baseline and _still_unselected(project)

        def before_publish():
            if not proof_ok():
                raise IndexContractError('initial_build_proof_changed')

        def admitted():
            fresh = resource_report(EMBED_POLICY, detailed=True)
            return controller.check(EMBED_POLICY, fresh)['ready']

        roots = wiki_index_roots(identity['collections'])
        try:
            with ExitStack() as stack:
                resident = start_service is start_owned_embed_service

                def start_resident():
                    # The detached, installation-owned model starts outside
                    # the provider lane. A pending preview query can then use
                    # completed documents between finite build wakes.
                    from laya_embed_resident import ensure_running
                    if not proof_ok():
                        raise IndexContractError('initial_build_proof_changed')
                    state['service'] = ensure_running(project, approval['providerManifest'])

                if resident:
                    start_resident()
                stack.enter_context(controller.activity('provider', EMBED_POLICY, observed))

                def ensure_service():
                    if state['service'] is not None:
                        return
                    if not proof_ok():
                        raise IndexContractError('initial_build_proof_changed')
                    if resident:
                        start_resident()
                        if state['service'] is not None:
                            return
                    try:
                        service = start_service(approval['providerManifest'], admit=admitted)
                    except IndexContractError as exc:
                        if str(exc) == 'provider_admission_deferred':
                            raise ResourceDeferred('laya_build_resource_pressure') from exc
                        raise
                    stack.callback(service.stop)
                    state['service'] = service

                class AdmittedProvider:
                    manifest = provider.manifest
                    profile = provider.profile

                    def embed_document_chunks(self, body, path):
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        if not admitted():
                            raise ResourceDeferred('laya_build_resource_pressure')
                        ensure_service()
                        return _embed_with_budget(provider, body, path)

                with LayaIndexRuntime(source_root=roots, scope=identity['scopeId'],
                                      db_path=identity['candidateDb'],
                                      queue_path=identity['queueDb'],
                                      db_incarnation=identity['dbIncarnation'],
                                      catalog_path=identity['catalogDb'],
                                      binding_store_path=identity['bindingStore'],
                                      wiki_only=True) as runtime:
                    from laya_incremental_docs import backfill_cached, publish_document

                    def publish_complete(snapshot, path, sha, title, body, profile_id):
                        if not proof_ok():
                            raise IndexContractError('initial_build_proof_changed')
                        publish_document(runtime, identity, snapshot, path, sha,
                                         title, body, profile_id,
                                         require_verified=True)

                    deadline = clock() + BUILD_WAKE_SECONDS
                    # A bundle upgraded in place may already have a durable
                    # vector-fill cursor beyond documents published by the
                    # newer per-document read path. Reuse complete cached
                    # sets first; this never invokes the embedding model.
                    backfill = backfill_cached(
                        runtime, identity, identity['vectorProfileId'],
                        deadline=min(deadline, clock() + 15), clock=clock,
                        before_publish=before_publish,
                        # Cached vectors need no model lane. Give this
                        # at-most-15s metadata pass progress even while a
                        # due QMD wake makes new embedding work yield.
                        should_yield=lambda: False,
                        require_verified=True)
                    result = (backfill if backfill['reason'] in
                              ('laya_sync_yield_priority',)
                              else _build_rounds(runtime, AdmittedProvider(), before_publish,
                                                 controller, lambda: not proof_ok(), admitted,
                                                 deadline=deadline, clock=clock,
                                                 on_document_complete=publish_complete))
                    if (backfill['state'] == 'pending' and
                            backfill['reason'] == 'incremental_backfill' and
                            result['state'] in ('indexed', 'unchanged')):
                        result = backfill  # retain the wake until old docs are visible
                    sealed = None
                    if result['state'] in ('indexed', 'unchanged'):
                        sealed = initial_build.evidence(runtime, result.get('generation'),
                                                        identity['vectorProfileId'])
            if not proof_ok():
                raise IndexContractError('initial_build_proof_changed')
        except ResourceDeferred as exc:
            result, sealed = {'state': 'deferred', 'reason': str(exc)}, None
        except Exception as exc:
            # An unexpected provider failure previously left the old progress
            # reason intact forever. Preserve that reason and the exception's
            # existing retry semantics, while recording only a fixed class.
            progress = {**(record.get('progress') or {}),
                        'lastAttemptNs': time.time_ns(),
                        'lastAttemptClass': _build_attempt_class(error=exc),
                        'lastAttemptReason': _build_attempt_reason(exc)}
            try:
                initial_build.advance(record, progress=progress)
            except Exception:
                pass  # Diagnostic failure must not replace the real failure.
            raise
        result = {**result, 'lastAttemptNs': time.time_ns(),
                  'lastAttemptClass': _build_attempt_class(result)}
        changes = initial_build.transition(record, result, sealed)
        if changes:
            record = initial_build.advance(record, **changes)
    answer = {key: result[key] for key in ('state', 'reason', 'generation', 'path',
                                           'required', 'detail') if key in result}
    answer['initialBuild'] = record['phase']
    if result['state'] in ('indexed', 'unchanged') and record['phase'] != 'sealed':
        # Published, but a newer scan keeps the candidate unservable: not done.
        answer['state'], answer['reason'] = 'pending', 'initial_build_scan_pending'
    return _checked(answer)


def _build_rounds(runtime, provider, before_publish, controller, proof_changed, admitted,
                  *, deadline, clock, on_document_complete=None):
    """Bounded scan and per-document fill rounds of one initial-build wake.

    Unlike a selected wake this never requests a scan: the catalog bootstraps
    one itself, and every extra request would chain another full rescan
    (and a re-read of every member for the fill) into a long build. A file
    over one step's byte budget is hashed by one single-file step sized to it
    (streamed, never buffered), after a fresh resource sample; under pressure
    the wake defers and the scan stays pending at that file.
    """
    from laya_vector_capacity import (provider_profile_id, vector_fill_step,
                                      pending_vector_fill_step)
    profile_id = provider_profile_id(provider)

    def should_yield():
        return _higher_priority_wake(controller)
    result = None
    large = None
    for _ in range(MAX_CAPACITY_ROUNDS):
        if clock() >= deadline:
            break
        if should_yield():
            raise ResourceDeferred('laya_sync_yield_priority')
        if proof_changed():
            raise IndexContractError('initial_build_proof_changed')
        budget = {}
        if large is not None:
            if not admitted():
                raise ResourceDeferred('laya_build_resource_pressure')
            budget, large = {'max_files': 1, 'max_bytes': large}, None
        result = sync_step(runtime, before_publish=before_publish, require_vector=True,
                           vector_profile_id=profile_id, **budget)
        if result.get('reason') == 'scan_pending' and on_document_complete is not None:
            partial = pending_vector_fill_step(runtime, provider,
                                               should_yield=should_yield,
                                               before_write=before_publish,
                                               on_document_complete=on_document_complete)
            if partial['state'] in ('blocked', 'deferred'):
                return {key: partial[key] for key in ('state', 'reason', 'path', 'detail')
                        if key in partial}
        if (result['state'] == 'deferred' and not budget
                and result.get('reason') == 'file_exceeds_step_bytes'):
            large = result['required']['max_bytes']
            continue
        if result['state'] == 'indexed':
            # Settle once more: a newer scan may still be pending.
            continue
        if result['state'] != 'pending':
            return result
        if result.get('reason') == 'vector_backlog':
            fill = vector_fill_step(runtime, provider, should_yield=should_yield,
                                    on_document_complete=on_document_complete,
                                    before_write=before_publish)
            result = {**result, 'fill': {key: fill[key] for key in
                                         ('state', 'reason', 'embedded', 'cached', 'bytes',
                                          'cursor', 'path', 'detail', 'rows') if key in fill}}
            if fill['state'] in ('deferred', 'blocked'):
                return {**{key: fill[key] for key in ('state', 'reason', 'path', 'detail')
                           if key in fill}, 'fill': result['fill'],
                        'catalog': result.get('catalog')}
    return result if result is not None else {'state': 'pending', 'reason': 'wake_budget'}
