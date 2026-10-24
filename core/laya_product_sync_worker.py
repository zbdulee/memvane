"""Off-hook, journal-bound full-scan sync for a selected Laya Index route.

Runtime wakes contain only a project path. Every attempt rereads the selected
route, installer journal, provider attestation and DB/queue incarnation. A
failed or deferred attempt leaves the wake for the runtime dispatcher to retry.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from resource_deferred import ResourceDeferred
from context_learning.resource_coordination import Coordinator
from context_learning.shared import resource_report
from laya_index_a import IndexContractError
from laya_index_host_route import select_route, wiki_index_roots
from laya_index_runtime import (BINDING_STORE_SUFFIX, CATALOG_SUFFIX, LayaIndexRuntime,
                                _collection_roots, capacity_store_paths)
from laya_index_sync import prepare_candidate_batch_off_hook, sync_full, sync_step
from laya_install_journal import (cas_mark_new_write, inspect_journal,
                                  preflight_product_candidate, verify_selected_route,
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


def _higher_priority_wake(controller):
    """Yield between documents so older QMD index/update work can dispatch."""
    from runtime_work import WakeQueue
    try:
        rows = WakeQueue(controller.root).pending()
        return any(row['kind'] in ('index', 'update') and row['due'] <= time.time()
                   for row in rows)
    except (OSError, ValueError, sqlite3.Error):
        return True


def _capacity_paths(decision):
    return capacity_store_paths(decision['dbPath'])


def _selected(project):
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise IndexContractError('selected_project_unavailable')
    decision = select_route(str(project), action='update')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        raise IndexContractError('selected_route_unavailable')
    proof = verify_selected_route(decision)
    if proof.get('productRouteAllowed') is not True or not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('selected_provider_unavailable')
    return decision, proof


def _route_source(decision):
    """Choose the runtime source binding before any provider or model start.

    v1 passes ``sourceRoot`` unchanged. v2 passes ``collectionRoots`` only
    after the runtime's own disjoint-root check and a match against the
    route's declared collections. LayaIndexRuntime checks the roots again.
    """
    v2 = 'collections' in decision or 'collectionRoots' in decision
    if v2 and 'sourceRoot' in decision:
        raise IndexContractError('selected_route_source_ambiguous')
    if not v2:
        if 'sourceRoot' not in decision:
            raise IndexContractError('selected_route_source_missing')
        return decision['sourceRoot']
    roots = decision.get('collectionRoots')
    bound = _collection_roots(roots) if isinstance(roots, Mapping) else None
    if bound is None:
        raise IndexContractError('selected_collection_roots_invalid')
    declared = decision.get('collections')
    if (not isinstance(declared, list) or len(declared) != len(bound)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                   for item in declared)
            or {item['name']: item.get('sourceRoot') for item in declared} != dict(bound)):
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
    """
    try:
        decision, proof = _selected(project)
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
    controller = coordinator or Coordinator()
    observed = report if report is not None else resource_report(EMBED_POLICY, detailed=True)
    if capacity is not None:
        result, proof = _run_capacity(decision, proof, source_root, capacity, provider,
                                      controller, observed, start_service)
        return _after_sync(decision, proof, grants, result)
    try:
        with controller.activity('provider', EMBED_POLICY, observed):
            # The hook's sealed stat receipt is a fast freshness fence. This
            # off-hook worker must rehash the full native closure before load.
            validated_manifest = verify_worker_attestation(decision)
            if validated_manifest != proof['providerManifest'] or verify_selected_route(decision) != proof:
                raise IndexContractError('selected_provider_proof_changed')
            def admit():
                # The service verifies its exact model/runtime files first.
                # Re-sample afterward, immediately before its model load.
                fresh = resource_report(EMBED_POLICY, detailed=True)
                return controller.check(EMBED_POLICY, fresh)['ready']

            service = start_service(validated_manifest, admit=admit)
            try:
                if verify_selected_route(decision) != proof:
                    raise IndexContractError('selected_proof_changed')
                class AdmittedProvider:
                    manifest = provider.manifest
                    profile = provider.profile

                    def embed_document_chunks(self, body, path):
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        return provider.embed_document_chunks(body, path)

                with LayaIndexRuntime(source_root=source_root,
                                      scope=decision['scopeId'], db_path=decision['dbPath'],
                                      queue_path=decision['queuePath'],
                                      db_incarnation=decision['dbIncarnation']) as runtime:
                    def before_publish():
                        nonlocal proof
                        if verify_selected_route(decision) != proof:
                            raise IndexContractError('selected_proof_changed')
                        marked = cas_mark_new_write(decision['journalPath'],
                                                    proof['selectionEpoch'])
                        if (marked.get('epoch') != proof['selectionEpoch'] + 1 or
                                marked.get('newWriteSeen') is not True):
                            raise IndexContractError('new_write_intent_unconfirmed')
                        fresh = verify_selected_route(decision)
                        if (fresh.get('selectionEpoch') != marked['epoch'] or
                                fresh.get('productRouteAllowed') is not True):
                            raise IndexContractError('new_write_proof_unconfirmed')
                        proof = fresh

                    batch = prepare_candidate_batch_off_hook(runtime, AdmittedProvider())
                    if verify_selected_route(decision) != proof:
                        raise IndexContractError('selected_proof_changed')
                    try:
                        if qa is not None:
                            # The batch is accepted; nothing is enqueued yet.
                            qa.hold(batch['source_manifest'])
                        if _higher_priority_wake(controller):
                            raise ResourceDeferred('laya_sync_yield_priority')
                        result = sync_full(runtime, before_publish=before_publish,
                                           vector_batch=batch, require_vector=True)
                    except BaseException as exc:
                        if qa is not None:
                            qa.finish(error=exc)
                        raise
                    if qa is not None:
                        qa.finish(result)
                if verify_selected_route(decision) != proof:
                    raise IndexContractError('selected_proof_changed')
            finally:
                service.stop()
    except ResourceDeferred as exc:
        return {'state': 'deferred', 'reason': str(exc)}
    if result.get('state') not in ('indexed', 'unchanged', 'deferred'):
        raise IndexContractError('invalid_sync_result')
    return _after_sync(decision, proof, grants, result)


def _after_sync(decision, proof, grants, result):
    """Wiki discovery after a completed sync, on both sync paths.

    After receipt and ACK, with the model service stopped. A raised error or
    a retry keeps this wake; the published head is not touched. ``proof`` is
    the one the sync ended with (the capacity path refreshes it at its
    fence). A blocked run, a burst skipped over the pending cap, or a run
    that started the cursor (earlier changes are not discovered) is reported
    as ``wikiDiscovery``.
    """
    if grants is None or result.get('state') not in ('indexed', 'unchanged'):
        return result
    import laya_wiki_discovery

    def recheck():
        if verify_selected_route(decision) != proof:
            raise IndexContractError('selected_proof_changed')
    outcome = laya_wiki_discovery.drain_after_sync(decision, grants, recheck=recheck)
    if outcome['state'] == 'retry':
        return {'state': 'deferred', 'reason': 'wiki_discovery_' + outcome['reason'],
                'generation': result.get('generation')}
    if (outcome['state'] == 'blocked' or outcome.get('reason') == 'pending_over_cap_skipped'
            or outcome.get('cursorStarted') is True):
        result = {**result, 'wikiDiscovery': outcome}
    return result


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
    state = {'proof': proof, 'service': None, 'fenced': False}

    def proof_ok():
        return verify_selected_route(decision) == state['proof']

    def before_publish():
        if not proof_ok():
            raise IndexContractError('selected_proof_changed')
        if state['fenced']:
            return
        current = state['proof']
        marked = cas_mark_new_write(decision['journalPath'], current['selectionEpoch'])
        if (marked.get('epoch') != current['selectionEpoch'] + 1 or
                marked.get('newWriteSeen') is not True):
            raise IndexContractError('new_write_intent_unconfirmed')
        fresh = verify_selected_route(decision)
        if (fresh.get('selectionEpoch') != marked['epoch'] or
                fresh.get('productRouteAllowed') is not True):
            raise IndexContractError('new_write_proof_unconfirmed')
        state['proof'] = fresh
        state['fenced'] = True

    try:
        with ExitStack() as stack:
            stack.enter_context(controller.activity('provider', EMBED_POLICY, observed))

            def ensure_service():
                if state['service'] is not None:
                    return
                # The full native closure is rehashed before any model load.
                validated = verify_worker_attestation(decision)
                if validated != state['proof']['providerManifest'] or not proof_ok():
                    raise IndexContractError('selected_provider_proof_changed')

                def admit():
                    fresh = resource_report(EMBED_POLICY, detailed=True)
                    return controller.check(EMBED_POLICY, fresh)['ready']
                service = start_service(validated, admit=admit)
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
                    return provider.embed_document_chunks(body, path)

            with LayaIndexRuntime(source_root=source_root, scope=decision['scopeId'],
                                  db_path=decision['dbPath'], queue_path=decision['queuePath'],
                                  db_incarnation=decision['dbIncarnation'],
                                  catalog_path=capacity[0],
                                  binding_store_path=capacity[1]) as runtime:
                result = _sync_capacity(runtime, AdmittedProvider(), before_publish,
                                        controller, lambda: not proof_ok())
            if not proof_ok():
                raise IndexContractError('selected_proof_changed')
    except ResourceDeferred as exc:
        return {'state': 'deferred', 'reason': str(exc)}, state['proof']
    return _checked(result), state['proof']


def _sync_capacity(runtime, provider, before_publish, controller, proof_changed):
    """Bounded sync_step / vector_fill_step rounds for one wake.

    The wake is the change signal (edits, Bash and SessionStart request it),
    so it first asks the catalog for a scan; a pending scan is marked to run
    once more. ``sync_step`` never loads a model; it asks for
    ``vector_backlog`` until the fill has cached every member, then seals and
    publishes. Chunk sets already filled stay cached when this wake stops.
    """
    from laya_vector_capacity import provider_profile_id, vector_fill_step
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
                           vector_profile_id=profile_id)
        if result['state'] != 'pending':
            return result
        if result.get('reason') == 'vector_backlog':
            fill = vector_fill_step(runtime, provider, should_yield=should_yield)
            if fill['state'] in ('deferred', 'blocked'):
                return {key: fill[key] for key in ('state', 'reason', 'path') if key in fill}
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
    if record['phase'] == 'retired':
        # An explicitly abandoned, never-started build: nothing to step; ACK.
        return {'state': 'unchanged', 'reason': 'initial_build_retired',
                'initialBuild': 'retired'}
    if record['phase'] == 'bound':
        return {'state': 'unchanged', 'reason': 'initial_build_bound', 'initialBuild': 'bound'}
    if record['phase'] == 'sealed':
        from laya_qmd_cutover import inspect_bound_cutover
        if inspect_bound_cutover(identity['journalPath']) is not None:
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
        if record is None or record['identity'] != identity:
            raise IndexContractError('initial_build_identity_changed')
        if record['phase'] == 'retired':
            return {'state': 'unchanged', 'reason': 'initial_build_retired',
                    'initialBuild': 'retired'}
        if record['phase'] == 'bound':
            return {'state': 'unchanged', 'reason': 'initial_build_bound',
                    'initialBuild': 'bound'}
        if record['phase'] == 'sealed':
            from laya_qmd_cutover import inspect_bound_cutover
            if inspect_bound_cutover(identity['journalPath']) is not None:
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
                stack.enter_context(controller.activity('provider', EMBED_POLICY, observed))

                def ensure_service():
                    if state['service'] is not None:
                        return
                    if not proof_ok():
                        raise IndexContractError('initial_build_proof_changed')
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
                        return provider.embed_document_chunks(body, path)

                with LayaIndexRuntime(source_root=roots, scope=identity['scopeId'],
                                      db_path=identity['candidateDb'],
                                      queue_path=identity['queueDb'],
                                      db_incarnation=identity['dbIncarnation'],
                                      catalog_path=identity['catalogDb'],
                                      binding_store_path=identity['bindingStore']) as runtime:
                    result = _build_rounds(runtime, AdmittedProvider(), before_publish,
                                           controller, lambda: not proof_ok(), admitted,
                                           deadline=clock() + BUILD_WAKE_SECONDS, clock=clock)
                    sealed = None
                    if result['state'] in ('indexed', 'unchanged'):
                        sealed = initial_build.evidence(runtime, result.get('generation'),
                                                        identity['vectorProfileId'])
            if not proof_ok():
                raise IndexContractError('initial_build_proof_changed')
        except ResourceDeferred as exc:
            result, sealed = {'state': 'deferred', 'reason': str(exc)}, None
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
                  *, deadline, clock):
    """Bounded sync_step / vector_fill_step rounds of one initial-build wake.

    Unlike a selected wake this never requests a scan: the catalog bootstraps
    one itself, and every extra request would chain another full rescan
    (and a re-read of every member for the fill) into a long build. A file
    over one step's byte budget is hashed by one single-file step sized to it
    (streamed, never buffered), after a fresh resource sample; under pressure
    the wake defers and the scan stays pending at that file.
    """
    from laya_vector_capacity import provider_profile_id, vector_fill_step
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
            fill = vector_fill_step(runtime, provider, should_yield=should_yield)
            result = {**result, 'fill': {key: fill[key] for key in
                                         ('state', 'reason', 'embedded', 'cached', 'bytes',
                                          'cursor', 'path', 'detail', 'rows') if key in fill}}
            if fill['state'] in ('deferred', 'blocked'):
                return {**{key: fill[key] for key in ('state', 'reason', 'path', 'detail')
                           if key in fill}, 'fill': result['fill'],
                        'catalog': result.get('catalog')}
    return result if result is not None else {'state': 'pending', 'reason': 'wake_budget'}
