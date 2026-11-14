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
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time

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
from memvane.journal.api import inspect_journal, selected_project_proof as _selected
# ReviewedLocalQmdProvider(manifest) builds a reviewed provider handle; the
# worker's tests patch this module name.
from memvane.provider.api import reviewed_provider as ReviewedLocalQmdProvider
from memvane.provider.api import start_owned_embed_service

# A conservative cold-start proxy, not a measured GGUF/native peak. The
# installed-candidate resource QA must measure and review this envelope.

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
PRIORITY_YIELD_MAX_ATTEMPTS = 3


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


def _higher_priority_wake(controller):
    """Yield for due QMD work; preserve an unavailable probe as a distinct deferral."""
    from memvane.runtime.api import wake_queue as WakeQueue
    try:
        rows = WakeQueue(controller.root).pending()
        return any(row['kind'] in ('index', 'update') and row['due'] <= time.time()
                   and row['attempts'] < PRIORITY_YIELD_MAX_ATTEMPTS
                   for row in rows)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Keep the fail-closed behavior, but never label a queue access error
        # as an observed higher-priority job. No queue row is recorded.
        raise ResourceDeferred('laya_sync_priority_probe_unavailable') from exc


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


def run(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    """Run one selected sync; call only from the durable runtime wake worker.

    """
    from memvane.install import api as build
    record = build.build_lookup(str(project))
    if record is not None and record.get('schema') == V1_BUILD_SCHEMA:
        decision = select_route(str(project), action='recall')
        if decision.get('state') == 'preview':
            if record['phase'] == 'ready':
                return {'state': 'unchanged', 'buildPhase': 'ready'}
            return _run_cold_build(decision, coordinator=coordinator, report=report,
                                   start_service=start_service)
    return _run_sync(project, coordinator=coordinator, report=report, start_service=start_service)


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
                vectors = _schema3_vector_context(decision, proof, lane, coordinator=coordinator,
                                                   report=report, start_service=start_service)
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
            vector = result.get('vector') or {}
            complete = (result.get('state') in ('indexed', 'unchanged')
                        and vector.get('state') in ('published', 'current'))
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
            if result.get('state') in ('indexed', 'unchanged'):
                return {**result, 'state': 'pending',
                        'reason': vector.get('reason', 'initial_build_vectors_pending')}
            return result
    except ResourceDeferred as exc:
        return _deferred(exc)


def _run_sync(project, *, coordinator=None, report=None, start_service=start_owned_embed_service):
    try:
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
    result, proof = _run_schema3(decision, proof, coordinator=coordinator, report=report,
                                 start_service=start_service, raw_scan=raw_scan)
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
                 start_service=start_owned_embed_service, raw_scan=None):
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
                result, proof = _publish_schema3(decision, proof, writer, vectors)
            from memvane.journal.api import resettle_selection
            status, proof = resettle_selection(decision, owner)
            if status != 'unchanged':
                result = {**result, 'readerResettle': status}
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

    Changed cards only: head members with the same bytes keep their trust. A
    linked worktree is published only while the main view is complete
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
            if outcome['state'] == 'deferred' and outcome['reason'] in (
                    'schema3_binding_changed', 'schema3_mass_removal_held'):
                # Retrying cannot clear these; that view keeps its last head.
                outcome = {**outcome, 'state': 'held'}
            linked.append({'root': str(project), **{k: outcome[k] for k in
                           ('state', 'reason', 'generation', 'ingested', 'pending', 'vector', 'provenance')
                           if k in outcome}})
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


def _publish_schema3_view(store, writer, decision, proof, worktree_id, binding, deadline, marks,
                          vectors=None, *, raw_scan=None, window=False):
    """One view's pass: changed cards ingested, unchanged members keep their trust.

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
    import hashlib
    from memvane.store import api as v
    from memvane.wiki.card.types import MASS_DELETE_FRACTION, MASS_DELETE_MIN
    from memvane.store.api import regular as _regular
    import uuid
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
        found, skipped = _schema3_wiki_cards(root)
        # skipPaths is read each pass, outside the binding: a card it newly
        # covers leaves the generation (not a file removal), one it no longer
        # covers is ingested like a new card.
        paths = v.admitted_paths(dict(row), found)
        removed = sorted(set(old) - set(found))
        excluded = sorted(set(old) & (set(found) - set(paths)))
        current = {}
        for path in paths:
            try:
                raw, _node, _root = _regular(root, path, store_types.MAX_BODY)
            except OSError:
                raise store_types.BlobStoreError('wiki_card_unavailable')
            current[path] = (hashlib.sha256(raw).hexdigest(), raw)
        changed = {p for p in paths if p not in old or old[p]['blob_sha'] != current[p][0]}
        if (len(removed) > MASS_DELETE_MIN
                and len(removed) > MASS_DELETE_FRACTION * len(old)):
            return {'state': 'deferred', 'reason': 'schema3_mass_removal_held',
                    'storeFormat': 'schema3', 'removed': len(removed)}
        scanned = {}
        observed = _schema3_sources(decision, handle, deadline, raw_scan=scanned)
        if raw_scan is not None and 'snapshot' in scanned:
            raw_scan['snapshot'] = scanned['snapshot']
        # Only a completed scan is a current observation; over its bounds no revision is judged.
        current_sources = ({source: (sha, length) for source, (sha, length, _tracked) in observed.items()}
                           if 'snapshot' in scanned else None)
        if (not changed and not removed and not excluded and not cold
                and v.head_same(store, head, baseline, observed, handle)):
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
                        'vector': vector, 'provenance': provenance}
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
            generation = store.prepare_generation(handle, manifest, expected_head=head,
                                                  vector_spec=vectors['spec'], deadline=deadline)
            writer.validate()
            store.publish_generation(handle, generation, expected_head=head, deadline=deadline)
            vector = dict(vector, state='published', publishMs=int((time.monotonic() - started) * 1000))
            return {'state': 'indexed', 'storeFormat': 'schema3', 'generation': generation,
                    'ingested': 0, 'removed': 0, 'pending': 0, 'skipped': skipped,
                    'sources': len(observed), 'vector': vector,
                    'provenance': provenance}
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
        spec = vectors['spec'] if vector['state'] == 'complete' else None
        if not staging:
            # The last fill call may run past the pass deadline (one document):
            # this pass's generation, lexical or vector, gets a fresh reserve.
            deadline = max(deadline, time.monotonic() + SCHEMA3_PUBLISH_RESERVE)
        started = time.monotonic()
        generation = store.prepare_generation(handle, manifest, expected_head=head,
                                              vector_spec=spec, deadline=deadline)
        writer.validate()
        if cold and pending:
            # No partial first generation: recall answers view_preparing until complete.
            return {'state': 'pending', 'reason': 'schema3_view_preparing', 'storeFormat': 'schema3',
                    **({'progressKind': 'ingest'} if ingested else {}),
                    'generation': None, 'prepared': generation, 'ingested': ingested,
                    'removed': len(removed), 'pending': pending, 'skipped': skipped, 'provenance': provenance}
        if staging:
            # Published cards wait for new bytes: the head stays, the prepared
            # progress is the next pass's carry.
            return {'state': 'pending', 'reason': 'schema3_changes_staged', 'storeFormat': 'schema3',
                    **({'progressKind': 'ingest'} if ingested else {}),
                    'generation': head['gen_id'], 'prepared': generation, 'ingested': ingested,
                    'removed': len(removed), 'pending': pending, 'skipped': skipped, 'provenance': provenance}
        store.publish_generation(handle, generation, expected_head=head, deadline=deadline)
        if vector['state'] == 'lexical_first':
            generation, vector = _schema3_rows_vector_publish(store, writer, proof, marks, handle, manifest,
                                                              vectors, deadline)
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
              'provenance': provenance}
    if pending:
        result['reason'] = 'schema3_ingest_backlog'
        if ingested:
            result['progressKind'] = 'ingest'
    return result


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
        missing = v.embedding_sets_missing(store, blobs, spec['recipe_id'], spec['profile_id'])
    except store_types.BlobStoreError as exc:
        if exc.reason not in ('recipe_missing', 'profile_missing'):
            raise
        missing = True
    if missing:
        return {'state': 'lexical_first'}
    return {'state': 'complete', 'filled': 0, 'cacheHits': len(blobs), 'targets': len(manifest['members'])}


def _schema3_rows_vector_publish(store, writer, proof, marks, handle, manifest, vectors, deadline):
    """After a row-format lexical publication: fill the new head and seal its vector generation.

    Returns ``(generation, vector)``; an unfinished fill keeps the lexical head.
    """
    vector = _schema3_vectors(store, writer, proof, marks, handle, None, None, vectors, deadline)
    head = store.head(handle)
    if vector['state'] != 'complete':
        return head['gen_id'], vector
    generation = store.prepare_generation(handle, manifest, expected_head=head, vector_spec=vectors['spec'],
                                          deadline=deadline)
    writer.validate()
    store.publish_generation(handle, generation, expected_head=head, deadline=deadline)
    return generation, vector


class _VectorMemberBlocked(Exception):
    """A document whose provider chunks the view recipe cannot reproduce."""


class _VectorProviderBlocked(Exception):
    """The install's provider cannot be proved (attestation, manifest or profile defect)."""


# Provider proof failures retrying cannot clear: the vector wake ends ``blocked``.
SCHEMA3_PROVIDER_DEFECTS = frozenset(('provider_manifest_invalid', 'selected_provider_profile_mismatch',
                                      'worker_attestation_invalid', 'worker_attestation_changed',
                                      'worker_runtime_files_invalid', 'worker_provider_unapproved'))


def _schema3_vector_context(decision, proof, lane, *, coordinator, report, start_service):
    """Per-pass vector state: the install provider's spec and a lazily admitted provider.

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
            raise ResourceDeferred(context['refused'])
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
            raise

        def call(method, *args):
            # At each provider-call boundary, as the v6 capacity path does
            # between rounds: due QMD work, or another project's initial build
            # waiting on the owner this pass holds. Sets already committed stay
            # (a pass that added some reports progress and is re-offered).
            if (_higher_priority_wake(controller)
                    or _initial_build_waiting(controller, Path(decision['projectRoot']))):
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
            return call()
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


def _schema3_resume(store, targets, cursor, recipe_id, profile_id):
    """Index of the first target from ``cursor`` without its embedding set, when its chunk set is stored."""
    from memvane.store import api as v
    first = v.embedding_coverage(store, targets, recipe_id, profile_id, first_from=cursor)
    return first[0] if first is not None and first[1] else None


def _schema3_vector_detail(result):
    """The main view's vector progress as one fixed code for the wake event, or None.

    ``vec_<state>[_<reason>]`` with ``rem`` (targets left), ``ord`` (first
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
    spec = vectors.get('spec')
    if spec is None:
        return {'state': 'off'}
    if sealed == spec:
        return {'state': 'current'}
    recipe_id, profile_id = spec['recipe_id'], spec['profile_id']
    current = lambda: marks.get('proof', proof)  # noqa: E731 - the proof after this pass's fence
    try:
        # No blobs: only the recipe and profile rows are checked.
        v.embedding_sets_missing(store, (), recipe_id, profile_id)
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
        before = v.embedding_coverage(store, targets, recipe_id, profile_id)
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
            first = v.embedding_coverage(store, targets, recipe_id, profile_id, first_from=at)
            return {'state': 'pending', 'reason': reason, 'filled': filled, 'cacheHits': hits,
                    'remaining': len(targets) - at,
                    'progress': v.embedding_coverage(store, targets, recipe_id, profile_id) - before,
                    'cursorMoved': at - cursor, 'ordinal': None if first is None else first[0],
                    'providerMs': vectors.get('stats', {}).get('providerMs', 0)}
        while not status['complete']:
            left = deadline - SCHEMA3_PUBLISH_RESERVE - time.monotonic()
            first = v.embedding_coverage(store, targets, recipe_id, profile_id, first_from=status['cursor'])
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
            files, seconds = 8, min(SCHEMA3_FILL_CALL_SECONDS, left)
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
            mark = v.embedding_coverage(store, reach, recipe_id, profile_id) if window else None
            try:
                step = store.fill_view(handle, manifest, recipe_id, profile_id, chunker=chunker,
                                       embedder=embedder, write_fence=fence,
                                       expected_cursor=status['cursor'], max_files=files,
                                       max_bytes=store_types.MAX_BODY, deadline_seconds=seconds)
            except store_types.BlobStoreError as exc:
                if exc.reason not in SCHEMA3_FILL_DEADLINES:
                    raise
                # Content sets committed before the deadline stay (cache hits
                # next pass); the lexical publish of this pass goes on.
                if window and v.embedding_coverage(store, reach, recipe_id, profile_id) > mark:
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
        return {'state': 'deferred', 'reason': str(exc)}
    except _VectorMemberBlocked as exc:
        return {'state': 'blocked', 'reason': str(exc)}
    except _VectorProviderBlocked as exc:
        return {'state': 'provider_blocked', 'reason': str(exc)}
    return {'state': 'complete', 'filled': filled, 'cacheHits': hits, 'targets': len(targets),
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
