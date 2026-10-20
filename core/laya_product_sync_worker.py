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
from laya_index_host_route import select_route
from laya_index_runtime import (BINDING_STORE_SUFFIX, CATALOG_SUFFIX, LayaIndexRuntime,
                                _collection_roots, capacity_store_paths)
from laya_index_sync import prepare_candidate_batch_off_hook, sync_full, sync_step
from laya_install_journal import (cas_mark_new_write, verify_selected_route,
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
    return bound


def run(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    """Run one selected sync; call only from the durable runtime wake worker."""
    decision, proof = _selected(project)
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
