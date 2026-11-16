"""Off-hook, journal-bound full-scan sync for a selected memvane-db route.

Runtime wakes contain only a project path. Every attempt rereads the selected
route, installer journal, provider attestation and DB/queue incarnation. A
failed or deferred attempt leaves the wake for the runtime dispatcher to retry.
"""
from __future__ import annotations
from memvane.resident.types import EMBED_POLICY
from memvane.store import types as store_types

from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3
import time
import uuid

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.runtime.api import coordinator as Coordinator
from memvane.runtime.api import resource_report
from memvane.kernel.errors import IndexContractError
from memvane.journal.api import select_route
from memvane.route.api import selected_collection_roots, wiki_index_roots
from memvane.wiki.intake.api import collection_roots as _collection_roots
from memvane.wiki.intake.api import (schema3_sources as _schema3_sources,
                                     schema3_wiki_cards as _schema3_wiki_cards)
from memvane.runtime.api import sync_owner
from memvane.journal.types import JournalEvidenceError, V1_BUILD_SCHEMA
from memvane.journal.api import build_read_v1
from memvane.journal.api import selected_project_proof as _selected
# ReviewedLocalQmdProvider(manifest) builds a reviewed provider handle (a test
# runtime root's fixture provider port answers through provider.api).
from memvane.provider.api import reviewed_provider as ReviewedLocalQmdProvider
from memvane.provider.api import start_owned_embed_service


_PROFILE_STAGES = ('scan', 'plan', 'coverage', 'provider', 'storage', 'publish')


@contextmanager
def _profile_phase(profile, stage):
    if profile is None:
        yield
        return
    started = time.monotonic_ns()
    try:
        yield
    finally:
        profile[stage + 'Calls'] += 1
        profile[stage + 'Ns'] += time.monotonic_ns() - started


def _profile_call(profile, stage, function, *args, **kwargs):
    with _profile_phase(profile, stage):
        return function(*args, **kwargs)


def _profile_read_cards(profile, root, paths, regular):
    current = {}
    with _profile_phase(profile, 'scan'):
        for path in paths:
            try:
                raw, _node, _root = regular(root, path, store_types.MAX_BODY)
            except OSError:
                raise store_types.BlobStoreError('wiki_card_unavailable')
            current[path] = (hashlib.sha256(raw).hexdigest(), raw)
    return current


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


# An initial build is small and bounded, but it takes the same per-user sync
# owner as every selected sync. A selected sync yields to another project's
# initial build only while that build is retrying and its last attempt ended
# normally deferred on a busy owner, recently. A build whose last attempt
# raised (the record then keeps its earlier reason and adds
# ``lastAttemptReason``), a build failing for any other reason, or an old
# record no wake retries never holds a long sync back.
INITIAL_BUILD_YIELD_PHASES = ('registered', 'building')
INITIAL_BUILD_YIELD_RECENT_SECONDS = 600


def _owner_blocked(progress, now_ns):
    attempted = progress.get('lastAttemptNs')
    return (progress.get('state') == 'deferred' and progress.get('reason') == 'owner_busy'
            and progress.get('lastAttemptClass') == 'other'
            and 'lastAttemptReason' not in progress
            and type(attempted) is int
            and 0 <= now_ns - attempted <= INITIAL_BUILD_YIELD_RECENT_SECONDS * 1_000_000_000)


def _initial_build_waiting(controller, project):
    """Another project's initial build is waiting and blocked on the owner.

    On True the waiting build's existing wake is pulled before every due row
    (never re-created), so the yielded owner goes to it rather than to
    whichever row is due next. Selected syncs only, between rounds: initial
    builds never yield to each other.
    """
    from memvane.install import api as initial_build
    from memvane.runtime.api import wake_queue as WakeQueue
    try:
        queue = WakeQueue(controller.root)
        rows = queue.pending()
    except (OSError, ValueError, sqlite3.Error):
        # Only an optimization: an unreadable queue never defers the sync.
        return False
    now_ns = time.time_ns()
    waiting = []
    for row in rows:
        target = row.get('target')
        if (row.get('kind') != 'laya-sync' or row.get('status') != 'pending' or
                not isinstance(target, str) or not target or target == str(project)):
            continue
        try:
            record = initial_build.build_lookup(target)
        except Exception:  # An unreadable pointer is no reason to yield.
            continue
        if not isinstance(record, dict) or record.get('phase') not in INITIAL_BUILD_YIELD_PHASES:
            continue
        progress = record.get('progress')
        if isinstance(progress, dict) and _owner_blocked(progress, now_ns):
            waiting.append(target)
    for target in waiting:
        try:
            queue.pull_first('laya-sync', target)
        except (OSError, ValueError, sqlite3.Error):
            pass
    return bool(waiting)


def _selected_service(decision, manifest, admit, start_service):
    """Borrow the installed warm owner; retain injectable transient fixtures.

    The detached owner cannot acquire the provider admission lane while this
    sync holds it. A cold wake defers here; the next durable wake starts the
    owner outside this lane.
    """
    if start_service is start_owned_embed_service:
        from memvane.resident.api import attached_service, probe
        # This callback runs with the provider lane held. Starting its new
        # owner here could race that lane on a model failure between prewarm
        # and embedding; the next durable sync wake starts it outside the lane.
        status = probe(manifest)
        if status == 'ready':
            return attached_service()
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


def run(project, *, coordinator=None, report=None, start_service=None):
    """Run one selected sync; call only from the durable runtime wake worker.

    Without ``start_service`` the warm embedding service is the installed
    one, or a test runtime root's fixture provider port's.
    """
    from memvane.install import api as build
    if start_service is None:
        from memvane.provider.api import fixture_port
        port = fixture_port()
        start_service = port.start_service if port is not None else start_owned_embed_service
    record = build.build_lookup(str(project))
    if record is not None and record.get('schema') == V1_BUILD_SCHEMA:
        decision = select_route(str(project), action='recall')
        if decision.get('state') == 'preview':
            if record['phase'] == 'ready':
                return {'state': 'unchanged', 'buildPhase': 'ready'}
            result = _run_cold_build(decision, coordinator=coordinator, report=report,
                                     start_service=start_service)
            _record_build_progress(build.build_lookup(str(project)), result)
            return result
    result = _run_sync(project, coordinator=coordinator, report=report, start_service=start_service)
    if record is not None and record.get('schema') == V1_BUILD_SCHEMA:
        _record_build_progress(record, result)
    return result


def _record_build_progress(record, result):
    """Best-effort, bounded status snapshots; build.json remains unchanged."""
    if not isinstance(record, dict):
        return
    ingest = result.get('ingestProgress')
    vector = result.get('vector') or {}
    counts = vector.get('statusProgress')
    vector_state = vector.get('state')
    if record.get('phase') == 'ready' and vector_state == 'current':
        return
    if ingest is None and counts is None and vector_state not in (
            'current', 'published', 'pending', 'deferred', 'blocked', 'provider_blocked'):
        return
    stage = ('complete' if vector_state in ('published', 'current')
             else 'vector' if vector_state not in (None, 'off')
             else 'ready' if result.get('buildPhase') == 'ready' else 'ingest')
    try:
        from memvane.diagnostics import api as diagnostics
        diagnostics.emit('index.build.progress', outcome='progress',
                         state_root=record['identity']['stateRoot'],
                         install=record['identity']['installId'], buildEpoch=record['epoch'],
                         stage=stage, ingest=ingest, vector=counts)
    except Exception:
        pass


def _run_cold_build(decision, *, coordinator=None, report=None,
                    start_service=start_owned_embed_service):
    """Publish the registered main view under the existing build and sync owners."""
    from contextlib import ExitStack
    from memvane.install import api as build
    from memvane.journal.api import build_writer_scope
    from memvane.store import api as store_api
    state = Path(decision['stateRoot'])
    try:
        with sync_owner(Path(decision['storePath']), wait=True) as owner:
            if owner is None:
                return {'state': 'deferred', 'reason': 'owner_busy'}
            with ExitStack() as lane, build_writer_scope(decision, queue_owner=owner) as writer:
                proof = writer.admission_proof()
                expected_build = build.build_lookup(decision['projectRoot'])
                info = writer.store()
                # Publish the first complete lexical head without starting the
                # provider. The selected sync wake fills vectors after activation.
                vectors = {'spec': None}
                store = store_api.open_store(info['path'], repository=info['repository'],
                                             scope_id=decision['scopeId'])
                try:
                    identity = store_api.store_identity(store)
                    if (identity['incarnation'], identity['policy_digest']) != (info['incarnation'],
                                                                                info['policy_digest']):
                        raise IndexContractError('schema3_store_changed')
                    result = _publish_schema3_view(store, writer, decision, proof,
                        info['worktree_id'], info['worktree_binding'],
                        time.monotonic() + SCHEMA3_PASS_SECONDS, {}, vectors, window=True)
                finally:
                    store.close()
            complete = (result.get('state') in ('indexed', 'unchanged')
                        and result.get('generation') is not None)
            with build.build_owner(state) as owned:
                if not owned:
                    raise ResourceDeferred('owner_busy')
                record = build_read_v1(state)
                if record != expected_build:
                    raise IndexContractError('initial_build_identity_changed')
                build.build_advance_v1(record, phase='ready' if complete else 'building',
                                 reason=None if complete else result.get('reason'))
            if complete:
                return {**result, 'buildPhase': 'ready'}
            return result
    except ResourceDeferred as exc:
        return _deferred(exc)


def _prepare_provider_reattest(project):
    """Reattest under the sync owner, after proving the selected installation."""
    from memvane.provider.api import fixture_port
    if fixture_port() is not None:
        return
    from memvane.journal.api import verify_selected_route
    from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
    from memvane.provider import api as reattest
    from memvane.diagnostics import api as diagnostics
    decision = select_route(str(project), action='update')
    if decision.get('state') != 'candidate':
        return
    proof = verify_selected_route(decision)
    state = Path(proof['journalPath']).parent
    require_cached_plugin_for_live_write(state)
    from memvane.kernel.memvane_version import writer_floor_allows
    if not writer_floor_allows(proof['writerFloor']):
        raise IndexContractError('writer_floor')
    with sync_owner(Path(decision['storePath']), wait=True) as owner:
        if owner is None:
            raise ResourceDeferred('owner_busy')
        if verify_selected_route(decision) != proof:
            raise IndexContractError('selected_proof_changed')
        manifest = proof['providerManifest']
        original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
        try:
            current, identity = reattest.reattestation_current(manifest)
            if reattest.reattestation_valid(state, original, current, identity, require_legacy=True):
                return
            reattest.reattestation_update(state, manifest)
            changed = [key for key in ('provider', 'worker') if original[key] != current[key]]
            diagnostics.emit('provider.reattest', outcome='accepted', decision=decision, changed=changed)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            try:
                changed = reattest.reattestation_differences(state, manifest) or ['manifest']
            except (ValueError, KeyError, TypeError, OSError):
                changed = ['manifest']
            diagnostics.emit('provider.reattest', outcome='rejected', decision=decision,
                             reason=str(exc), changed=changed)
            # Existing refusal path remains authoritative.
            if not proof.get('providerAvailable'):
                raise IndexContractError('provider_identity_changed') from exc


def _run_sync(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    try:
        _prepare_provider_reattest(project)
        decision, proof = _selected(project, owner_wait=True)
    except ResourceDeferred as exc:
        return _deferred(exc)
    # A malformed route source fails before any model start or index write.
    source_root = _route_source(decision)
    # The view store path loads no model and opens no v6 runtime or
    # capacity store; discovery reads the view head, not the v6 lineage.
    grants = None
    if not isinstance(source_root, str):
        # Snapshot hook grants before the scan, as on the v6 path.
        from memvane.wiki.intake import api as laya_wiki_discovery
        grants = laya_wiki_discovery.observe_grants(decision)
    # One raw scan per pass: the main view's D2 observation is also the
    # discovery snapshot (laya_wiki_discovery._raw_snapshot, same roots and bounds).
    raw_scan = {}
    profile = {stage + suffix: 0 for stage in _PROFILE_STAGES for suffix in ('Calls', 'Ns')}
    started = time.monotonic_ns()
    token = store_types.FILL_PROFILE.set(profile)
    try:
        result, proof = _run_schema3(decision, proof, coordinator=coordinator, report=report,
                                     start_service=start_service, raw_scan=raw_scan, profile=profile)
    finally:
        store_types.FILL_PROFILE.reset(token)
        from memvane.diagnostics import api as diagnostics
        fields = {stage + 'Calls': profile[stage + 'Calls'] for stage in _PROFILE_STAGES}
        fields.update({stage + 'Ms': round(profile[stage + 'Ns'] / 1000000, 3) for stage in _PROFILE_STAGES})
        diagnostics.emit('sync.profile', decision=decision, outcome='observed',
                         ms=(time.monotonic_ns() - started) / 1000000, **fields)
    result = _after_sync(decision, proof, grants, result, raw_snapshot=raw_scan.get('snapshot'))
    vectors = [item['vector'] for item in [result, *result.get('worktrees', ())] if item.get('vector')]
    waiting = ([v for v in vectors if v['state'] == 'provider_blocked']
               or [v for v in vectors if v['state'] in ('pending', 'deferred')])
    main = result.get('vector') or {}
    progressed = (main.get('state') == 'pending' and main.get('progress', 0) > 0
                  and waiting and waiting[0] is main)
    blocked = [v for v in vectors if v['state'] == 'provider_blocked']
    if (result.get('state') in ('indexed', 'unchanged') and not blocked
            and (progressed or result.get('linkedReoffer'))):
        # The main view's vectors moved this pass (or it published them and
        # a linked view waits for the budget): ACK (not a counted failure)
        # and offer the same wake again shortly, so the fill goes on with a
        # warm worker instead of waiting for the next edit. An offer
        # failure leaves the next natural wake to go on.
        try:
            from memvane.runtime import api as runtime_work
            runtime_work.request('laya-sync', str(project), delay=SCHEMA3_VECTOR_REOFFER_SECONDS)
            result = {**result, 'vectorReoffer': True}
        except Exception:
            result = {**result, 'state': 'pending', 'reason': main.get('reason')}
    elif result.get('state') in ('indexed', 'unchanged') and waiting:
        # The lexical generations are published and discovery ran; the
        # wake stays until the views' vector generations are published,
        # or ends ``blocked`` on a provider defect of the install.
        state = 'blocked' if waiting[0]['state'] == 'provider_blocked' else waiting[0]['state']
        result = {**result, 'state': state, 'reason': waiting[0].get('reason')}
        if state == 'pending' and waiting[0] is main and main.get('progress', 0) > 0:
            result['progressKind'] = 'vector'
    detail = _schema3_vector_detail(result)
    if detail:
        result = {**result, 'detail': detail}
    return result

# One schema3 pass: wall budget and card ingests (each a Git capture). A
# large change set stays ``pending`` over several passes. New cards publish
# as they are ingested; while a published card's new bytes wait for a later
# pass, the pass only prepares its progress (the head keeps serving the old
# bytes) and the next pass carries those prepared members instead of
# ingesting them again. A cold view (no published generation) only prepares.
SCHEMA3_PASS_SECONDS = 45
SCHEMA3_MAX_INGEST = 64
# Ingest stops this long before the pass deadline so prepare/publish of the
# ingested cards always fits; otherwise an over-budget pass would publish
# nothing and the next one would redo the same ingests.
SCHEMA3_PUBLISH_RESERVE = 15
# A pass whose main view has nothing to ingest (unchanged cards and sources)
# pays the pass's fixed cost once and fills vectors for this long (its
# publish reserve included); a pass that ingests keeps the pass budget.
SCHEMA3_VECTOR_SECONDS = 240
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


def _run_schema3(decision, proof, *, coordinator=None, report=None,
                 start_service=start_owned_embed_service, raw_scan=None, profile=None):
    """One publish pass of the selected schema3 view store (main view, then linked views).

    Admission is the sync owner plus a worker writer scope over the current
    plan; the plan must still name a schema3 store. No v6 runtime opens. The
    provider lane is taken only when a view's vector fill has content no
    store set holds yet (``_schema3_vectors``). Returns ``(result, proof)``:
    a publish advances the journal epoch, so ``_after_sync`` rechecks
    against the proof the pass ended with. A pass that published the main
    view's first vector generation re-seals the selected record so recall
    asks for query vectors (``resettle_selection``).
    """
    from contextlib import ExitStack
    from memvane.journal.api import selected_v1_writer_scope
    try:
        with sync_owner(Path(decision['storePath']), wait=True) as owner:
            if owner is None:
                return {'state': 'deferred', 'reason': 'owner_busy'}, proof
            with ExitStack() as lane, selected_v1_writer_scope(decision, queue_owner=owner) as writer:
                if writer.admission_proof() != proof:
                    raise IndexContractError('selected_proof_changed')
                vectors = _schema3_vector_context(decision, proof, lane, coordinator=coordinator,
                                                   report=report, start_service=start_service)
                vectors['rawScan'] = raw_scan
                vectors['profile'] = profile
                result, proof = _publish_schema3(decision, proof, writer, vectors)
            from memvane.journal.api import resettle_selection
            status, proof = resettle_selection(decision, owner)
            if status != 'unchanged':
                result = {**result, 'readerResettle': status}
            if result.get('state') in ('indexed', 'unchanged'):
                try:
                    from memvane.learning.api import request_selector_maintenance
                    request_selector_maintenance(decision, owner=owner, origin='sync')
                except Exception:
                    from memvane.diagnostics import api as diagnostics
                    diagnostics.emit('learning.selector.request', outcome='failed',
                                     reason='sync_retry_failed', decision=decision, origin='sync')
            return result, proof
    except ResourceDeferred as exc:
        return _deferred(exc), proof


# Linked git worktrees whose own views one pass may publish after the main
# view (``git worktree list`` order). Later ones wait for the next wake.
SCHEMA3_MAX_LINKED = 16
# Refusals that hold one linked worktree's view (its row, settings or wiki
# root no longer match, registration is refused, or the store refuses the
# linked decision for its row) without failing the pass. Only the linked
# loop reads this set; the main view's refusals fail the pass as before.
SCHEMA3_LINKED_HELD = SCHEMA3_BINDING_REASONS | frozenset(('worktree_binding_conflict',
                                                           'worktree_missing', 'wiki_binding_invalid',
                                                           'candidate_io_failed',
                                                           'attestation_decision_mismatch'))


def _publish_schema3(decision, proof, writer, vectors=None):
    """Main view first, then each linked worktree's own view (design 5.2).

    Recheck observed source revisions as well as changed card bytes. A linked
    worktree is published only while the main view is complete
    (``indexed``/``unchanged``); its refusals hold that view alone. One
    ``vectors`` context (``_schema3_vector_context``) serves every view of
    the pass, so content two views share is embedded once.

    Returns ``(result, proof)``; ``proof`` is the writer's fresh admission
    proof once this pass marked its first write.
    """
    if vectors is None:
        vectors = {'spec': None}
    from memvane.store import api as store_api
    info = writer.store()
    deadline = time.monotonic() + SCHEMA3_PASS_SECONDS
    store = store_api.open_store(info['path'], repository=info['repository'], scope_id=decision['scopeId'])
    try:
        identity = store_api.store_identity(store)
        if (identity['incarnation'], identity['policy_digest']) != (info['incarnation'], info['policy_digest']):
            raise IndexContractError('schema3_store_changed')
        marks = {}
        result = _publish_schema3_view(store, writer, decision, proof, info['worktree_id'],
                                       info['worktree_binding'], deadline, marks, vectors,
                                       raw_scan=vectors.get('rawScan'), window=True)
        if result['state'] not in ('indexed', 'unchanged'):
            return result, marks.get('proof', proof)
        linked = []
        gc_worktrees = [info['worktree_id']] if result['state'] == 'indexed' else []
        listing = max(deadline, time.monotonic() + SCHEMA3_LINKED_LIST_SECONDS)
        for project in _schema3_linked_projects(decision, listing):
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
                                                proof, worktree_id, None, deadline, marks, vectors)
            except store_types.CheckoutUnavailable as exc:
                outcome = {'state': 'held', 'reason': exc.reason}
            except store_types.BlobStoreError as exc:
                if exc.reason not in SCHEMA3_LINKED_HELD:
                    raise
                outcome = {'state': 'held', 'reason': exc.reason}
            if outcome['state'] == 'deferred' and outcome['reason'] == 'schema3_binding_changed':
                # Retrying cannot clear these; that view keeps its last head.
                outcome = {**outcome, 'state': 'held'}
            if outcome['state'] == 'indexed':
                gc_worktrees.append(worktree_id)
            linked.append({'root': str(project), **{k: outcome[k] for k in
                           ('state', 'reason', 'generation', 'ingested', 'pending', 'vector', 'provenance')
                           if k in outcome}})
        # Keep the existing owner and writer proof until every view succeeds.
        # A live SQLite reader can defer maintenance until a later publication.
        if all(item['state'] in ('indexed', 'unchanged') for item in linked):
            gc_deadline = time.monotonic() + 1.0
            for gc_id in gc_worktrees:
                if time.monotonic() >= gc_deadline:
                    break
                try:
                    handle = store.capture_view(gc_id, deadline=gc_deadline)
                    while time.monotonic() < gc_deadline:
                        writer.validate()
                        removed = store.collect_view_rows(handle, limit=64, deadline=gc_deadline)
                        if not any(removed.values()):
                            break
                except (store_types.BlobStoreError, store_types.CheckoutUnavailable) as exc:
                    if exc.reason not in ('candidate_busy', 'fts_deadline_exceeded',
                                          'checkout_budget_exhausted', 'checkout_changed'):
                        raise
                    break
    finally:
        store.close()
    proof = marks.get('proof', proof)
    if not linked:
        return result, proof
    result = {**result, 'worktrees': linked}
    if result['state'] == 'unchanged' and any(item['state'] == 'indexed' for item in linked):
        result['state'] = 'indexed'
    main = result.get('vector') or {}
    advanced = ((main.get('state') == 'pending' and main.get('progress', 0) > 0)
                or main.get('state') == 'published')
    budget = [item for item in linked if item['state'] == 'pending'
              and item.get('reason') == 'schema3_pass_budget']
    # While the main view's vectors move (or this pass published them), a
    # linked view held only by this pass's budget does not make the pass a
    # failure: the re-offered wake (``_run_sync``) runs the next pass, which
    # reaches it.
    if advanced and budget:
        result['linkedReoffer'] = True
    if any(item['state'] in ('pending', 'deferred') and not (advanced and item in budget) for item in linked):
        result['state'] = 'pending'
        result.setdefault('reason', 'schema3_linked_pending')
        result.pop('progressKind', None)
    return result, proof


def _schema3_linked_projects(decision, deadline):
    """Linked worktrees of the main checkout that the route selector serves from it.

    Each listed worktree's project must classify as ``worktree`` of exactly
    this main project (same common Git dir, no route of its own, same derived
    settings identity: ``laya_index_host_route._worktree_route``).
    """
    from memvane.kernel import paths as git_branch_safety
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

    Registration is a store write, so the pass's journal fence comes first
    (``store.api.linked_worktree_id``).
    """
    from memvane.store import api as store_api
    return store_api.linked_worktree_id(store, info, project, deadline,
                                        lambda: _mark_schema3_write(writer, proof, marks))


def _linked_decision(decision, project):
    """The main decision with its collection roots rebased onto a linked worktree.

    Same derived settings identity (proved by the selector); only
    ``projectRoot`` and the roots move, as the store's attested-ingest rule
    binds the decision to that worktree row's project and wiki root. A root
    under the project folder names the linked worktree's own active folder
    (a branch may keep ``.auto-context`` while main uses ``.memvane``, or the
    reverse): the selector's settings identity treats the two names alike.
    """
    from memvane.settings import types as project_types
    main = Path(decision['projectRoot'])
    folder = project_types.CURRENT

    def rebased(root):
        parts = Path(root).relative_to(main).parts
        if parts and parts[0] in project_types.NAMES:
            parts = (folder, *parts[1:])
        return str(project.joinpath(*parts))
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
    fresh = writer.mark_new_write()
    expected_epoch = proof['selectionEpoch'] + (0 if proof.get('buildPending') else 1)
    if (fresh.get('selectionEpoch') != expected_epoch
            or fresh.get('productRouteAllowed') is not True):
        raise IndexContractError('new_write_proof_unconfirmed')
    marks['proof'] = fresh


def _consume_compile_intents(store, writer, proof, marks, decision, handle, members, current):
    """Consume this view's compile intents without changing its member trust (``compile_intents``)."""
    from memvane.sync import compile_intents
    return compile_intents.consume(
        store, decision, handle, members, current,
        mark=lambda: 'proof' in marks or _mark_schema3_write(writer, proof, marks),
        validate=lambda: writer.validate(),
        ingest_limit=SCHEMA3_MAX_INGEST)


def _schema3_card_source_match(path, raw, decision, current_sources):
    """Return observed match and expected revisions; None means unknown."""
    if current_sources is None:
        return None
    from memvane.wiki.card import api as card
    from memvane.wiki.card.types import FRONTMATTER_RE
    try:
        body = raw.decode('utf8').replace('\r\n', '\n').replace('\r', '\n')
        if card.verified_wiki_locations({'path': path, 'body': body}, decision, frozenset(),
                                        tuple(decision['recallSettings']['skipPaths']),
                                        current_source=False) is None:
            return None
        match = FRONTMATTER_RE.match(body)
        if match is None:
            return None
        revisions = card.frontmatter_source_revisions(match.group(1))
        groups = {}
        for revision in revisions:
            groups.setdefault(revision['path'], set()).add((revision['sha256'], revision['size']))
        if any(tuple(current_sources[p]) not in versions for p, versions in groups.items()
               if p in current_sources):
            return False, None
        if groups and all(p in current_sources for p in groups):
            return True, {(p, *current_sources[p]) for p in groups}
        return None
    except (UnicodeError, ValueError, KeyError, TypeError):
        return None


def _schema3_reclassified(paths, changed, old, current, decision, sources):
    result = {}
    for path in set(paths) - changed:
        verdict = _schema3_card_source_match(path, current[path][1], decision, sources)
        if verdict is not None and verdict[0] != bool(old[path]['trust_digest']):
            result[path] = verdict
    return result


def _staged_vector_result(store, writer, proof, marks, handle, baseline, vectors, deadline,
                          head, generation, ingested, removed, pending, skipped, provenance, window):
    result = {'state': 'pending', 'reason': 'schema3_changes_staged', 'storeFormat': 'schema3',
              **({'progressKind': 'ingest'} if ingested else {}),
              'generation': head['gen_id'], 'prepared': generation, 'ingested': ingested,
              'removed': removed, 'pending': pending, 'skipped': skipped, 'provenance': provenance}
    if window:
        deadline = max(deadline, time.monotonic() + SCHEMA3_VECTOR_SECONDS)
        result['vector'] = _schema3_vectors(store, writer, proof, marks, handle, None,
                                            baseline['vector_spec'], vectors, deadline, window=True)
    return result


def _publish_schema3_view(store, writer, decision, proof, worktree_id, binding, deadline, marks,
                          vectors=None, *, raw_scan=None, window=False):
    """One view's pass: ingest changed cards and recheck observed source revisions.

    ``binding`` is the main worktree's journal-recorded binding (None for a
    linked worktree, whose row the store itself re-observes). A view with no
    head yet (a first-seen worktree or branch) ingests every card: trust is
    never reused from another view (``ViewStore._card``: ``foreign_card_trust``).

    The manifest's ``sources`` are this pass's raw source observation
    (``_schema3_sources``, design D2). With the install provider's vector
    spec (``vectors``), a complete fill publishes the manifest as a vector
    generation; otherwise the lexical generation is published and the
    result's ``vector`` names why the vectors wait. An unchanged lexical head
    gets its vector generation from the same manifest.
    """
    from memvane.store import api as v
    from memvane.store.api import regular as _regular
    if vectors is None:
        vectors = {'spec': None}
    try:
        handle = store.capture_view(worktree_id, deadline=deadline)
        head = store.head(handle)
        cold = head['gen_id'] is None
        row, baseline, _ordinal = v.read_view_baseline(store, handle, head, worktree_id=worktree_id)
        old, carry = baseline['old'], baseline['carry']
        if row is None or (binding is not None and row['binding_digest'] != binding):
            raise store_types.BlobStoreError('wiki_binding_changed')
        root = row['wiki_root']
        from memvane.install.api import ensure_wiki_keeper
        ensure_wiki_keeper(root)
        profile = vectors.get('profile')
        found, skipped = _profile_call(profile, 'scan', _schema3_wiki_cards, root)
        # skipPaths is read each pass, outside the binding: a card it newly
        # covers leaves the generation (not a file removal), one it no longer
        # covers is ingested like a new card.
        paths = v.admitted_paths(dict(row), found)
        removed = sorted(set(old) - set(found))
        excluded = sorted(set(old) & (set(found) - set(paths)))
        current = _profile_read_cards(profile, root, paths, _regular)
        changed = {p for p in paths if p not in old or old[p]['blob_sha'] != current[p][0]}
        scanned = {}
        observed = _profile_call(profile, 'scan', _schema3_sources, decision, handle, deadline, raw_scan=scanned)
        if raw_scan is not None and 'snapshot' in scanned:
            raw_scan['snapshot'] = scanned['snapshot']
        # Only a completed scan is a current observation; over its bounds no revision is judged.
        current_sources = ({source: (sha, length) for source, (sha, length, _tracked) in observed.items()}
                           if 'snapshot' in scanned else None)
        same_observation = not cold and v.head_same(store, head, baseline, observed, handle)
        reclassified = (_schema3_reclassified(paths, changed, old, current, decision, current_sources)
                        if current_sources is not None and not same_observation else {})
        changed.update(reclassified)
        if (not changed and not removed and not excluded and not cold
                and same_observation):
            provenance = _consume_compile_intents(store, writer, proof, marks, decision, handle, old, current)
            if window:
                # The main view has nothing to ingest: its vector fill (and the
                # generation it may publish) gets the vector window. Linked
                # views keep the pass deadline (``_publish_schema3``).
                deadline = max(deadline, time.monotonic() + SCHEMA3_VECTOR_SECONDS)
            sealed = baseline['vector_spec']
            # Row heads fill their own eligible cards; the manifest is built only to publish.
            vector = _schema3_vectors(store, writer, proof, marks, handle, None, sealed, vectors, deadline,
                                      window=window)
            if vector['state'] != 'complete':
                return {'state': 'unchanged', 'storeFormat': 'schema3', 'generation': head['gen_id'],
                        'vector': vector, 'provenance': provenance,
                        'ingestProgress': {'done': len(paths), 'total': len(paths)}}
            # The last fill call may run past the pass deadline: the vector
            # generation it completed gets a fresh publish reserve.
            deadline = max(deadline, time.monotonic() + SCHEMA3_PUBLISH_RESERVE)
            manifest = baseline.get('manifest')
            if manifest is None:
                _mark_schema3_write(writer, proof, marks)
                scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
                manifest = dict(scan_id=scan['id'], policy_digest=handle.policy_digest, revocation_epoch=0,
                                complete=True, sources=v.put_source_fingerprints(store, handle, scan, observed),
                                members=[old[p] for p in sorted(old)])
            started = time.monotonic()
            generation = _profile_call(profile, 'publish', store.prepare_generation, handle, manifest, expected_head=head,
                                       vector_spec=vectors['spec'], deadline=deadline)
            writer.validate()
            _profile_call(profile, 'publish', store.publish_generation, handle, generation, expected_head=head, deadline=deadline)
            vector = dict(vector, state='published', publishMs=int((time.monotonic() - started) * 1000))
            return {'state': 'indexed', 'storeFormat': 'schema3', 'generation': generation,
                    'ingested': 0, 'removed': 0, 'pending': 0, 'skipped': skipped,
                    'sources': len(observed), 'vector': vector,
                    'provenance': provenance, 'ingestProgress': {'done': len(paths), 'total': len(paths)}}
        # First store write of this pass: the journal fence comes first.
        _mark_schema3_write(writer, proof, marks)
        scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
        members, pending, ingested, held = {}, 0, 0, 0
        keys = ('path', 'blob_sha', 'display_title', 'trust_digest')
        for path in paths:
            sha, raw = current[path]
            if path not in changed:
                members[path] = {k: old[path][k] for k in keys}
                continue
            if path in carry and carry[path]['blob_sha'] == sha:
                verdict = reclassified.get(path)
                sources = baseline['carry_sources']
                if (verdict is None or (path in sources and sources[path] == verdict[1]
                                        and bool(carry[path]['trust_digest']) == verdict[0])):
                    members[path] = {k: carry[path][k] for k in keys}
                    continue
            if (pending or ingested >= SCHEMA3_MAX_INGEST
                    or time.monotonic() >= deadline - SCHEMA3_PUBLISH_RESERVE):
                # Left for the next wake. No generation may name its old blob
                # (prepare checks current bytes); while it is in the head, this
                # pass publishes nothing and the head keeps serving it.
                pending += 1
                held += path in old
                continue
            admitted = store.ingest_attested_card(worktree_id, path, decision=decision,
                                                  provenance_scan=scan, current_sources=current_sources)
            ingested += 1
            body = (raw.decode('utf8') if admitted['blob_sha'] == sha
                    else store.document(admitted['blob_sha'])['body'])
            members[path] = dict(path=path, blob_sha=admitted['blob_sha'],
                                 display_title=v.markdown_title(body, admitted['blob_sha']),
                                 trust_digest=admitted['trust_digest'])
        if pending and not ingested and not removed and not excluded:
            return {'state': 'deferred', 'reason': 'schema3_pass_budget', 'storeFormat': 'schema3'}
        provenance = _consume_compile_intents(store, writer, proof, marks, decision, handle, members, current)
        sources = v.put_source_fingerprints(store, handle, scan, observed)
        manifest = dict(scan_id=scan['id'], policy_digest=handle.policy_digest, revocation_epoch=0,
                        complete=True, sources=sources, members=[members[p] for p in sorted(members)])
        staging = pending and (cold or held)
        vector = {'state': 'pending', 'reason': 'schema3_view_preparing'}
        if not staging:
            # A row head fills its own cards: the vector generation follows the
            # lexical one unless every eligible set is already stored.
            vector = _schema3_rows_covered(store, manifest, baseline['vector_spec'], vectors,
                                           {members[p]['blob_sha'] for p in changed if p in members})
            if vector['state'] == 'lexical_first' and baseline['vector_spec'] == vectors.get('spec'):
                vector = _schema3_prefill_changed(store, writer, proof, marks, handle, manifest, head,
                                                  {members[p]['blob_sha'] for p in changed if p in members},
                                                  vectors, deadline)
        spec = vectors['spec'] if vector['state'] == 'complete' else None
        if not staging:
            # The last fill call may run past the pass deadline (one document):
            # this pass's generation, lexical or vector, gets a fresh reserve.
            deadline = max(deadline, time.monotonic() + SCHEMA3_PUBLISH_RESERVE)
        started = time.monotonic()
        generation = _profile_call(profile, 'publish', store.prepare_generation, handle, manifest, expected_head=head,
                                   vector_spec=spec, deadline=deadline)
        writer.validate()
        if cold and pending:
            # No partial first generation: recall answers view_preparing until complete.
            return {'state': 'pending', 'reason': 'schema3_view_preparing', 'storeFormat': 'schema3',
                    **({'progressKind': 'ingest'} if ingested else {}),
                    'generation': None, 'prepared': generation, 'ingested': ingested,
                    'removed': len(removed), 'pending': pending, 'skipped': skipped, 'provenance': provenance,
                    'ingestProgress': {'done': len(members), 'total': len(paths)}}
        if staging:
            # Published cards wait for new bytes: the head stays, the prepared
            # progress is the next pass's carry.
            return _staged_vector_result(store, writer, proof, marks, handle, baseline, vectors, deadline,
                                         head, generation, ingested, len(removed), pending, skipped, provenance, window)
        _profile_call(profile, 'publish', store.publish_generation, handle, generation, expected_head=head, deadline=deadline)
        if vector['state'] == 'lexical_first':
            generation, vector = _schema3_rows_vector_publish(store, writer, proof, marks, handle, manifest,
                                                              vectors, deadline, window=window)
    except store_types.BlobStoreError as exc:
        if exc.reason in SCHEMA3_BINDING_REASONS:
            return {'state': 'deferred', 'reason': 'schema3_binding_changed',
                    'detail': exc.reason, 'storeFormat': 'schema3'}
        if exc.reason in SCHEMA3_TRANSIENT_REASONS:
            return {'state': 'deferred', 'reason': exc.reason, 'storeFormat': 'schema3'}
        raise
    if vector['state'] == 'complete':
        vector = dict(vector, state='published', publishMs=int((time.monotonic() - started) * 1000))
    result = {'state': 'pending' if pending else 'indexed', 'storeFormat': 'schema3',
              'generation': generation, 'ingested': ingested, 'removed': len(removed),
              'pending': pending, 'skipped': skipped, 'sources': len(sources), 'vector': vector,
              'provenance': provenance, 'ingestProgress': {'done': len(members), 'total': len(paths)}}
    if pending:
        result['reason'] = 'schema3_ingest_backlog'
        if ingested:
            result['progressKind'] = 'ingest'
    return result


def _schema3_prefill_changed(store, writer, proof, marks, handle, manifest, head, changed, vectors, deadline):
    """Fill eligible changed content before publishing over a vector-sealed head."""
    from memvane.store import api as v
    spec = vectors['spec']
    if time.monotonic() >= deadline - SCHEMA3_PUBLISH_RESERVE:
        return {'state': 'pending', 'reason': 'schema3_vector_budget', 'progress': 0}
    try:
        targets = _profile_call(vectors.get('profile'), 'plan', v.fill_plan,
                                store, handle, spec['recipe_id'], spec['profile_id'],
                                manifest=manifest, expected_head=head, changed_blobs=changed,
                                deadline=deadline - SCHEMA3_PUBLISH_RESERVE)
    except store_types.BlobStoreError as exc:
        if exc.reason == 'fts_deadline_exceeded':
            return {'state': 'pending', 'reason': 'schema3_vector_budget', 'progress': 0}
        raise
    current = lambda: marks.get('proof', proof)  # noqa: E731
    chunker, embedder = _schema3_chunker(vectors, current)
    filled = hits = 0
    try:
        for blob in targets:
            left = deadline - SCHEMA3_PUBLISH_RESERVE - time.monotonic()
            if left <= 0:
                return {'state': 'pending', 'reason': 'schema3_vector_budget', 'progress': filled,
                        'filled': filled, 'cacheHits': hits, 'remaining': len(targets) - filled - hits}
            def fence():
                _mark_schema3_write(writer, proof, marks)
            result = _profile_call(vectors.get('profile'), 'storage', store.ensure_content,
                                   blob, spec['recipe_id'], spec['profile_id'],
                                   chunker=chunker, embedder=embedder, write_fence=fence,
                                   deadline_seconds=min(SCHEMA3_RESUME_SECONDS, left))
            filled += result['status'] == 'filled'
            hits += result['status'] != 'filled'
    except ResourceDeferred as exc:
        if filled:
            return {'state': 'pending', 'reason': str(exc), 'progress': filled,
                    'filled': filled, 'cacheHits': hits, 'remaining': len(targets) - filled - hits}
        refused = _deferred(exc)
        if refused.get('detail') == refused['reason']:
            del refused['detail']
        return refused
    except _VectorMemberBlocked as exc:
        return {'state': 'blocked', 'reason': str(exc)}
    except _VectorProviderBlocked as exc:
        return {'state': 'provider_blocked', 'reason': str(exc)}
    except store_types.BlobStoreError as exc:
        if exc.reason in SCHEMA3_FILL_DEADLINES or exc.reason == 'content_claim_busy':
            reason = 'schema3_vector_content_busy' if exc.reason == 'content_claim_busy' else 'schema3_vector_deadline'
            return {'state': 'pending', 'reason': reason, 'progress': filled,
                    'filled': filled, 'cacheHits': hits, 'remaining': len(targets) - filled - hits}
        raise
    return {'state': 'complete', 'filled': filled, 'cacheHits': hits, 'targets': len(targets),
            'providerMs': vectors.get('stats', {}).get('providerMs', 0)}


def _schema3_rows_covered(store, manifest, sealed, vectors, changed_blobs):
    """Whether a row-format stage can seal ``vectors['spec']`` now, without a fill.

    ``complete`` when every set it needs is stored (the changed cards' when the
    head already has the spec, else every member's); ``lexical_first`` when
    the lexical generation is published first and the head's own fill follows
    (design: a row head fills its eligible cards).
    """
    from memvane.store import api as v
    spec = vectors.get('spec')
    if spec is None:
        return {'state': 'off'}
    blobs = set(changed_blobs) if sealed == spec else {member['blob_sha'] for member in manifest['members']}
    try:
        missing = _profile_call(vectors.get('profile'), 'coverage', v.embedding_sets_missing,
                                store, blobs, spec['recipe_id'], spec['profile_id'])
    except store_types.BlobStoreError as exc:
        if exc.reason not in ('recipe_missing', 'profile_missing'):
            raise
        missing = True
    if missing:
        return {'state': 'lexical_first'}
    return {'state': 'complete', 'filled': 0, 'cacheHits': len(blobs), 'targets': len(manifest['members'])}


def _schema3_rows_vector_publish(store, writer, proof, marks, handle, manifest, vectors, deadline, *, window=False):
    """After a row-format lexical publication: fill the new head and seal its vector generation.

    Returns ``(generation, vector)``; an unfinished fill keeps the lexical head.
    """
    if window:
        deadline = max(deadline, time.monotonic() + SCHEMA3_VECTOR_SECONDS)
    vector = _schema3_vectors(store, writer, proof, marks, handle, None, None, vectors, deadline, window=window)
    head = store.head(handle)
    if vector['state'] != 'complete':
        return head['gen_id'], vector
    generation = _profile_call(vectors.get('profile'), 'publish', store.prepare_generation, handle, manifest,
                               expected_head=head, vector_spec=vectors['spec'], deadline=deadline)
    writer.validate()
    _profile_call(vectors.get('profile'), 'publish', store.publish_generation, handle, generation, expected_head=head, deadline=deadline)
    return generation, vector


class _VectorMemberBlocked(Exception):
    """A document whose provider chunks the view recipe cannot reproduce."""


class _VectorProviderBlocked(Exception):
    """The install's provider cannot be proved (attestation, manifest or profile defect)."""


# Provider proof failures retrying cannot clear: the vector wake ends ``blocked``.
SCHEMA3_PROVIDER_DEFECTS = frozenset(('provider_manifest_invalid', 'selected_provider_profile_mismatch',
                                      'worker_attestation_invalid', 'worker_attestation_changed',
                                      'worker_runtime_files_invalid', 'worker_provider_unapproved'))


def _schema3_vector_context(decision, proof, lane, *, coordinator, report, start_service,
                            yield_to_builds=True):
    """Per-pass vector state: the install provider's spec and a lazily admitted provider.

    ``yield_to_builds`` is False for an initial build's own pass: only a
    selected sync yields its provider calls to a waiting initial build.

    ``acquire()`` takes the provider lane (``controller.activity``) and the
    warm service on first use and keeps both in ``lane`` until the pass
    ends; a refusal is remembered for the rest of the pass. The spec is None
    when the provider recipe has no view recipe (the view stays lexical).
    """
    from memvane.store.api import provider_spec
    spec = _schema3_vector_spec(proof)
    context = {'spec': spec, 'provider': None, 'refused': None, 'calls': 0, 'stash': {}}
    if spec is None:
        return context

    def acquire(current):
        if context['refused'] is not None:
            if context.get('blocked'):
                raise _VectorProviderBlocked(context['refused'])
            raise ResourceDeferred(context['refused'], context.get('refusedReport'))
        if context['provider'] is not None:
            return context['provider']
        try:
            try:
                provider = ReviewedLocalQmdProvider(current['providerManifest'])
            except IndexContractError as exc:
                raise IndexContractError('provider_manifest_invalid') from exc
            profile = current['vectorProfile']
            if (provider.profile['profile_id'] != profile['profileId']
                    or provider_spec(provider.manifest, profile) != spec):
                raise IndexContractError('selected_provider_profile_mismatch')
            manifest = _schema3_worker_manifest(decision, current)
            if manifest != current['providerManifest']:
                raise IndexContractError('selected_provider_proof_changed')
            if start_service is start_owned_embed_service:
                # Outside the provider lane: adoption is requested, never awaited.
                from memvane.resident.api import ensure_running
                ensure_running(decision['projectRoot'], manifest)
            controller = coordinator or Coordinator()
            observed = report if report is not None else resource_report(EMBED_POLICY, detailed=True)
            lane.enter_context(controller.activity('provider', EMBED_POLICY, observed))

            def admit():
                fresh = resource_report(EMBED_POLICY, detailed=True)
                return controller.check(EMBED_POLICY, fresh)['ready']
            service = _selected_service(decision, manifest, admit, start_service)
            lane.callback(service.stop)
        except (IndexContractError, JournalEvidenceError, OSError) as exc:
            # A provider that cannot be proved or started holds the vectors,
            # never the lexical publish of this pass. A defect of the install
            # itself ends the wake as ``blocked`` (bounded by the wake policy,
            # as v6); anything else is retried by a later wake.
            context['refused'] = 'vector_provider_' + _code(exc)
            if _code(exc) in SCHEMA3_PROVIDER_DEFECTS:
                context['blocked'] = True
                raise _VectorProviderBlocked(context['refused']) from exc
            raise ResourceDeferred(context['refused']) from exc
        except ResourceDeferred as exc:
            context['refused'] = str(exc)
            context['refusedReport'] = exc.report
            raise

        def call(method, *args):
            # At each provider-call boundary, as the v6 capacity path does
            # between rounds: another project's initial build waiting on the
            # owner this pass holds. Sets already committed stay (a pass that
            # added some reports progress and is re-offered).
            if yield_to_builds and _initial_build_waiting(controller, Path(decision['projectRoot'])):
                raise ResourceDeferred('laya_sync_yield_priority')
            context['calls'] += 1
            try:
                return method(*args)
            except IndexContractError as exc:
                if str(exc) == 'provider_member_refused':
                    raise
                # Socket, identity or result failures: retried by a later wake.
                context['refused'] = 'vector_provider_' + _code(exc)
                raise ResourceDeferred(context['refused']) from exc

        class AdmittedProvider:
            def embed_document_chunks(self, body, path):
                return call(_embed_with_budget, provider, body, path)

            def embed_document(self, body, title):
                return call(provider.embed_document, body, title)
        context['provider'] = AdmittedProvider()
        return context['provider']
    context['acquire'] = acquire
    return context


def _code(exc):
    text = str(exc)
    return text if re.fullmatch(r'[a-z0-9_]{1,60}', text) else 'failed'


def _schema3_vector_spec(proof):
    from memvane.store.api import provider_spec
    return provider_spec(proof.get('providerManifest'), proof.get('vectorProfile'))


def _schema3_worker_manifest(decision, proof):
    """The worker attestation's provider manifest for the current proof (closure rehash)."""
    from memvane.journal.api import worker_provider_manifest
    return worker_provider_manifest(decision, proof)


def _schema3_chunker(context, current):
    """View chunker over the provider's own chunking (one provider call per document).

    A document whose provider chunk positions are the provider's first pass
    (``qmd_first_pass``) has those chunks, and the vectors the call returned
    are kept for the embedder (v6 inputs and bytes). A document the provider
    re-split over its token window has no reproducible text here: it gets the
    recipe's fallback windows (``FALLBACK_CHARS``) and keeps no vector, so the
    embedder embeds each window alone.
    """
    import hashlib
    from memvane.store.types import FALLBACK_CHARS
    from memvane.store.api import qmd_first_pass

    stats = context.setdefault('stats', {'providerMs': 0})

    def timed(call):
        started = time.monotonic()
        try:
            return _profile_call(context.get('profile'), 'provider', call)
        finally:
            stats['providerMs'] = max(stats['providerMs'], int((time.monotonic() - started) * 1000))

    def chunker(document, _recipe):
        body = document['body']
        parts = qmd_first_pass(body)
        try:
            rows = timed(lambda: context['acquire'](current()).embed_document_chunks(body, document['path']))
        except IndexContractError as exc:
            if str(exc) == 'provider_member_refused':
                raise _VectorMemberBlocked('provider_member_refused') from exc
            raise
        kept = [row['pos'] for row in rows] == [pos for pos, _text in parts]
        if not kept:
            parts = qmd_first_pass(body, *FALLBACK_CHARS)
        if len(parts) > store_types.MAX_CHUNKS:
            # The store refuses the set; only this card's vectors wait.
            raise _VectorMemberBlocked('chunk_set_too_large')
        if kept:
            title = document['title'] or 'none'
            for (_pos, text), row in zip(parts, rows):
                context['stash']['title: ' + title + ' | text: ' + text] = row['vector']
        return [{'seq': seq, 'position': pos, 'text': text, 'text_sha': hashlib.sha256(text.encode('utf8')).hexdigest()}
                for seq, (pos, text) in enumerate(parts)]

    def embedder(inputs, _profile):
        vectors = []
        for text in inputs:
            vector = context['stash'].pop(text, None)
            if vector is None:
                # A chunk set stored without its embedding set: embed each input
                # whole; the worker formats the same ``title: | text:`` string.
                title, sep, chunk = text[len('title: '):].partition(' | text: ')
                if (not text.startswith('title: ') or not sep or not title or not chunk
                        or 'title: ' + title + ' | text: ' + chunk != text):
                    raise _VectorMemberBlocked('embedding_input_unparsed')
                try:
                    vector = timed(lambda: context['acquire'](current()).embed_document(chunk, title))
                except IndexContractError as exc:
                    if str(exc) == 'provider_member_refused':
                        raise _VectorMemberBlocked('provider_member_refused') from exc
                    raise
            vectors.append(vector)
        return vectors
    return chunker, embedder


# One fill call's soft budget inside the pass's vector budget.
SCHEMA3_FILL_CALL_SECONDS = 30
# The content filler drops an embedding set finished after its deadline
# (laya_blob_fill: an expired callback result is never published), while the
# chunk set stored before the embedder stays. A target whose chunk set is
# stored without its embedding set therefore had its embeddings dropped by an
# earlier deadline: it alone gets the large-document embedding budget
# (``_embed_with_budget``'s 270 s), so a card whose single embeddings outlast
# one fill call completes on its next pass instead of repeating the same
# dropped work every wake.
SCHEMA3_RESUME_SECONDS = 270
# A pass whose vectors moved but did not finish offers its own laya-sync wake
# again this much later (``WakeQueue.offer(delay=)``), within RECLAIM_SECONDS
# so the embedding worker stays warm.
SCHEMA3_VECTOR_REOFFER_SECONDS = 30
# Listing the linked worktrees after a main view whose last fill call ran past
# the pass deadline: their own pass-budget check then holds them for the
# re-offered wake instead of the listing failing on the spent budget.
SCHEMA3_LINKED_LIST_SECONDS = 5
SCHEMA3_FILL_DEADLINES = frozenset(('content_fill_deadline', 'fill_deadline_exceeded'))




def _schema3_vector_detail(result):
    """The main view's vector progress as one fixed code for the wake event, or None.

    ``vec_<state>[_<reason>]`` with a deferral's admission criterion, ``rem`` (targets left), ``ord`` (first
    missing target), ``pms`` (slowest provider call of the pass, ms), and for a
    published vector generation ``pub`` (its prepare+publish ms) and ``res_``
    (the reader reseal). No path or body.
    """
    import re
    vector = result.get('vector') if isinstance(result, dict) else None
    if not isinstance(vector, dict) or vector.get('state') not in ('pending', 'published', 'deferred',
                                                                   'blocked', 'provider_blocked'):
        return None
    reason = str(vector.get('reason') or '')
    for prefix in ('schema3_vector_', 'schema3_'):
        reason = reason[len(prefix):] if reason.startswith(prefix) else reason
    parts = ['vec_' + vector['state'] + ('_' + reason if reason else '')]
    if isinstance(vector.get('detail'), str):
        parts.append(vector['detail'])
    for key, label in (('remaining', 'rem'), ('ordinal', 'ord'), ('providerMs', 'pms'), ('publishMs', 'pub')):
        if type(vector.get(key)) is int:
            parts.append(label + str(vector[key]))
    if isinstance(result.get('readerResettle'), str):
        parts.append('res_' + result['readerResettle'])
    return re.sub(r'[^a-z0-9_.:-]', '_', ':'.join(parts))[:80]


def _schema3_vectors(store, writer, proof, marks, handle, manifest, sealed, vectors, deadline,
                     *, window=False):
    """Fill this manifest's vectors for the pass's spec; ``state`` says what remains.

    ``complete``: every eligible member has the spec's sets (the caller seals
    a vector generation). ``current``: ``sealed`` (the head's vector spec)
    already is the spec. ``off``: no spec. ``pending``: the pass budget ran
    out, a fill call hit its deadline, or a deferral came after this pass
    added embedding sets (cursor kept; committed content sets stay).
    ``deferred``: the provider lane or service refused before this pass
    added any embedding set. ``blocked``: the provider refused a document
    or its chunk set is over the store's limit; the view stays lexical until
    that card changes.
    ``provider_blocked``: the install's provider cannot be proved
    (``SCHEMA3_PROVIDER_DEFECTS``); the wake ends ``blocked``.
    """
    from memvane.store import api as v
    from memvane.store.types import MAX_FILES
    profile = vectors.get('profile')
    def coverage(*args, **kwargs):
        return _profile_call(profile, 'coverage', v.embedding_coverage, *args, **kwargs)
    spec = vectors.get('spec')
    if spec is None:
        return {'state': 'off'}
    if sealed == spec:
        return {'state': 'current'}
    recipe_id, profile_id = spec['recipe_id'], spec['profile_id']
    current = lambda: marks.get('proof', proof)  # noqa: E731 - the proof after this pass's fence
    try:
        # No blobs: only the recipe and profile rows are checked.
        _profile_call(profile, 'coverage', v.embedding_sets_missing, store, (), recipe_id, profile_id)
        registered = True
    except store_types.BlobStoreError as exc:
        if exc.reason not in ('recipe_missing', 'profile_missing'):
            raise
        registered = False
    pending = None
    try:
        if not registered:
            vectors['acquire'](current())
            _mark_schema3_write(writer, proof, marks)
            from memvane.store.api import register_provider_spec
            if register_provider_spec(store, current()['providerManifest'], current()['vectorProfile']) != spec:
                raise store_types.BlobStoreError('vector_spec_mismatch')
        _entry, _saved, _job, targets, stored = v.fill_plan(store, handle, recipe_id, profile_id)
        cursor = 0 if stored is None else stored
        before = coverage(store, targets, recipe_id, profile_id)
        missing = len(targets) - before
        if missing:
            vectors['acquire'](current())
        chunker, embedder = _schema3_chunker(vectors, current)
        status = store.fill_status(handle, manifest, recipe_id, profile_id)
        filled = hits = calls = 0
        cold_used = False

        def pending(reason, at):
            # ``progress``: embedding sets this pass added (store observation,
            # not callback counts); ``ordinal``: the first target still missing.
            first = coverage(store, targets, recipe_id, profile_id, first_from=at)
            return {'state': 'pending', 'reason': reason, 'filled': filled, 'cacheHits': hits,
                    'remaining': len(targets) - at,
                    'progress': coverage(store, targets, recipe_id, profile_id) - before,
                    'cursorMoved': at - cursor, 'ordinal': None if first is None else first[0],
                    'providerMs': vectors.get('stats', {}).get('providerMs', 0),
                    'statusProgress': {'done': at, 'total': len(targets)}}
        while not status['complete']:
            left = deadline - SCHEMA3_PUBLISH_RESERVE - time.monotonic()
            first = coverage(store, targets, recipe_id, profile_id, first_from=status['cursor'])
            span = None if first is None else first[0] - status['cursor'] + 1
            # The first cold target of a pass (no chunk set: its single chunked
            # provider call is the whole work) gets the resume budget, so a card
            # whose call outlasts one fill call is not dropped on every pass.
            # A target whose chunk set is stored keeps that budget as before.
            resumable = span is not None and span <= MAX_FILES and (first[1] or not cold_used)
            if left <= 0 and not (resumable and not calls):
                return pending('schema3_vector_budget', status['cursor'])

            def fence():
                _mark_schema3_write(writer, proof, marks)
            files, seconds = 32, min(SCHEMA3_FILL_CALL_SECONDS, left)
            if resumable:
                files, seconds = span, SCHEMA3_RESUME_SECONDS
                cold_used = cold_used or not first[1]
            elif span is not None and span > MAX_FILES:
                # A run of cache hits ahead of the first missing target: one call.
                files = MAX_FILES
            calls += 1
            # The targets this call can reach: the window compares their sets
            # before and after a call stopped by its own deadline.
            reach = targets[status['cursor']:status['cursor'] + files]
            mark = coverage(store, reach, recipe_id, profile_id) if window else None
            try:
                step = _profile_call(profile, 'storage', store.fill_view, handle, manifest, recipe_id, profile_id, chunker=chunker,
                                       embedder=embedder, write_fence=fence,
                                       expected_cursor=status['cursor'], max_files=files,
                                       max_bytes=store_types.MAX_BODY, deadline_seconds=seconds)
            except store_types.BlobStoreError as exc:
                if exc.reason not in SCHEMA3_FILL_DEADLINES:
                    raise
                # Content sets committed before the deadline stay (cache hits
                # next pass); the lexical publish of this pass goes on.
                if window and coverage(store, reach, recipe_id, profile_id) > mark:
                    # Inside the vector window, a call that added sets before its
                    # own deadline does not end the window: the next call passes
                    # those as cache hits. A call that added none ends it.
                    status = store.fill_status(handle, manifest, recipe_id, profile_id)
                    continue
                return pending('schema3_vector_deadline', status['cursor'])
            filled += step['filled']; hits += step['cache_hits']
            if step['status'] == 'deferred':
                return pending('schema3_vector_content_busy', step['cursor'])
            status = step
    except ResourceDeferred as exc:
        if pending is not None:
            # A deferral (a priority yield before a provider call, a provider
            # failure, a moved selection) after this pass committed embedding
            # sets: those stay, and the pass reports its progress like a
            # deadline stop, so the wake is not counted as a failure.
            stopped = pending(str(exc), status['cursor'])
            if stopped['progress'] > 0:
                return stopped
        # Keeps the lane's admission criterion (memory_reserve, ...) as
        # ``detail`` when it names more than the deferral reason itself.
        refused = _deferred(exc)
        if refused.get('detail') == refused['reason']:
            del refused['detail']
        return refused
    except _VectorMemberBlocked as exc:
        return {'state': 'blocked', 'reason': str(exc)}
    except _VectorProviderBlocked as exc:
        return {'state': 'provider_blocked', 'reason': str(exc)}
    return {'state': 'complete', 'filled': filled, 'cacheHits': hits, 'targets': len(targets),
            'statusProgress': {'done': len(targets), 'total': len(targets)},
            'providerMs': vectors.get('stats', {}).get('providerMs', 0)}


def _after_sync(decision, proof, grants, result, *, raw_snapshot=None):
    """Drain external edits and run wiki discovery after a completed sync."""
    if result.get('state') not in ('indexed', 'unchanged'):
        return result
    try:
        with sync_owner(Path(decision['storePath']), wait=True) as owner:
            if owner is None:
                return {**result, 'state': 'deferred', 'reason': 'owner_busy'}

            def recheck():
                from memvane.journal.api import owned_selected_proof
                if owned_selected_proof(decision, owner) != proof:
                    raise IndexContractError('selected_proof_changed')
            from memvane.wiki.intake import api as laya_external_edits
            from memvane.wiki.workspace.api import owner_scope as _owner_scope
            with _owner_scope((decision, proof), owner):
                external = laya_external_edits.drain(decision, recheck=recheck, owner=owner)
                if isinstance(external, dict) and external.get('remaining', 0):
                    return {**result, 'state': 'pending', 'reason': 'external_edits_pending'}
                if grants is None:
                    return result
                from memvane.wiki.intake import api as laya_wiki_discovery
                outcome = laya_wiki_discovery.drain_after_sync(
                    decision, grants, recheck=recheck, view_generation=result.get('generation'),
                    raw_snapshot=raw_snapshot)
    except ResourceDeferred as exc:
        return {**result, **_deferred(exc)}
    if outcome['state'] == 'retry':
        return {'state': 'deferred', 'reason': 'wiki_discovery_' + outcome['reason'],
                'generation': result.get('generation')}
    if (outcome['state'] == 'blocked' or outcome.get('reason') == 'pending_over_cap_skipped'
            or outcome.get('cursorStarted') is True or 'initialBackfill' in outcome):
        result = {**result, 'wikiDiscovery': outcome}
    return result
