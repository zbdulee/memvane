"""A-int flow for an explicit source, index and separate claim queue.

The selected sync service owns the journal/owner fence before calling here.
Separate stores have no shared transaction or power-loss exactly-once guarantee.
A string root is the v1 single source. A Mapping root is a runtime-validated
set of disjoint collection roots published as one full-membership generation.

``process_bound_one`` is the capacity path: the source is a COMPLETE catalog
snapshot, the generation name commits to a ``GenerationSourceBinding`` that
is already in the binding store, stage streams every re-read member, and
publish refuses unless that stored binding matches.
"""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from laya_index_a import IndexContractError
from laya_index_claim_queue import ACK, REPROCESS, DEFER, LEASE_LOST
import laya_index_source_port as source_port


def scan_source(root, scope, index):
    """Dispatch one full scan; v1 string roots keep the original call."""
    if isinstance(root, Mapping):
        return source_port.scan_collections(root, scope, index)
    return source_port.scan(root, scope, index)


def _settle_unpublishable(index, queue, job, token, reason):
    """Never ACK another claim's effect; retain a collided job for replan.

    The index and queue live in separate DBs. Recheck the exact receipt after
    a collision because a competing publish may have committed this claim in
    the meantime. An unreadable or mismatched receipt must remain deferred.
    """
    verdict = queue.conditional_ack(job, token, index)
    if verdict in (ACK, LEASE_LOST):
        return verdict
    if verdict == DEFER:
        return 'deferred'
    return ('replan' if queue.mark_replan(job, token, reason) else LEASE_LOST)


def _run_leased(index, queue, root, job, token, *,
                require_vector=False, prepare_vector=None):
    decision = queue.decide(index, job)
    if decision == ACK:
        if require_vector:
            # A lexical-only receipt cannot satisfy a selected vector job.
            from laya_index_vector import verify_generation_seal
            index.db.execute('BEGIN')
            try:
                verify_generation_seal(index, job['gen'])
                index.db.commit()
            except BaseException:
                index.db.rollback()
                raise
        result = queue.conditional_ack(job, token, index)
        return result if result in (ACK, LEASE_LOST) else 'deferred'
    if decision == DEFER:
        return 'deferred'
    if decision != REPROCESS:
        return 'deferred'
    source = scan_source(root, job['scope'], index)
    if source.manifest != job['source_manifest']:
        return ('replan' if queue.mark_replan(job, token, 'index_stale')
                else LEASE_LOST)
    try:
        # An identical staged seal returns already_staged after a process restart.
        index.stage(job['gen'], job['scope'], source.documents(),
                    base=job['base_gen'], source_manifest=source.manifest,
                    claim_digest=job['claim_digest'], claim_epoch=job['claim_epoch'])
        if require_vector:
            if not callable(prepare_vector):
                return 'deferred'
            prepare_vector(index, source, job)
        publish = (source_port.publish_collections_from_source
                   if isinstance(root, Mapping) else source_port.publish_from_source)
        published = publish(
            index, root, job['scope'], job['base_gen'], job['gen'], job['claim'],
            claim_digest=job['claim_digest'], claim_epoch=job['claim_epoch'],
            require_vector=require_vector)
    except IndexContractError as exc:
        if str(exc) in ('head_moved', 'index_stale'):
            reason = str(exc)
            return ('replan' if queue.mark_replan(job, token, reason)
                    else LEASE_LOST)
        if str(exc) == 'generation_conflict':
            # A globally unique immutable generation belongs to another
            # payload or scope. The retained job needs a fresh generation ID.
            return _settle_unpublishable(index, queue, job, token, 'index_stale')
        return 'deferred'
    if published in ('published', 'replayed'):
        # A publish return is not an ACK. Check the exact durable receipt again.
        result = queue.conditional_ack(job, token, index)
        return result if result in (ACK, LEASE_LOST) else 'deferred'
    if published in ('stale', 'index_stale'):
        reason = 'head_moved' if published == 'stale' else 'index_stale'
        return ('replan' if queue.mark_replan(job, token, reason) else LEASE_LOST)
    if published == 'not_prepared':
        # An identical immutable generation may already be published under a
        # different claim. It has no receipt authorizing this job's ACK.
        return _settle_unpublishable(index, queue, job, token, 'head_moved')
    return 'deferred'


def _run_bound_leased(index, queue, catalog, store, job, token):
    import laya_index_capacity as capacity
    decision = queue.decide(index, job)
    if decision == ACK:
        result = queue.conditional_ack(job, token, index)
        return result if result in (ACK, LEASE_LOST) else 'deferred'
    if decision != REPROCESS:
        return 'deferred'
    try:
        binding = capacity.load_job_binding(store, index, job)
    except IndexContractError as exc:
        if capacity.binding_store_busy(exc):
            return 'deferred'
        # An unreadable stored binding can never authorize a publish; retiring
        # the claim keeps it from deferring every later sync_step.
        binding = None
    if binding is None:
        # An unbound legacy name, or a bound name without its stored binding,
        # can never publish here. Re-capturing would yield another name.
        return ('replan' if queue.mark_replan(job, token, 'index_stale')
                else LEASE_LOST)
    try:
        capacity.stage_bound_streaming(index, catalog, binding, base=job['base_gen'],
                                       claim_digest=job['claim_digest'],
                                       claim_epoch=job['claim_epoch'])
        published = capacity.publish_bound(index, catalog, store, binding, job)
    except IndexContractError as exc:
        if capacity.binding_store_busy(exc):
            # A locked store proves nothing about the binding; it surfaces as
            # index_stale, so it must be classified before the replan branch.
            return 'deferred'
        if str(exc) in ('head_moved', 'index_stale'):
            return ('replan' if queue.mark_replan(job, token, str(exc))
                    else LEASE_LOST)
        if str(exc) == 'generation_conflict':
            return _settle_unpublishable(index, queue, job, token, 'index_stale')
        return 'deferred'
    if published in ('published', 'replayed'):
        result = queue.conditional_ack(job, token, index)
        return result if result in (ACK, LEASE_LOST) else 'deferred'
    if published in ('stale', 'index_stale'):
        reason = 'head_moved' if published == 'stale' else 'index_stale'
        return ('replan' if queue.mark_replan(job, token, reason) else LEASE_LOST)
    if published == 'not_prepared':
        return _settle_unpublishable(index, queue, job, token, 'head_moved')
    return 'deferred'


def _process_leased(queue, claim, recover, run):
    job = queue.lease(claim, recover=recover)
    if job is None:
        return 'unavailable'
    token = job['lease_token']
    result = None
    try:
        result = run(job, token)
    except (IndexContractError, sqlite3.Error, OSError):
        result = 'deferred'
    finally:
        if result not in (ACK, 'replan', LEASE_LOST):
            released = queue.release_for_retry(job, token)
    if result not in (ACK, 'replan', LEASE_LOST) and not released:
        return LEASE_LOST
    return result


def process_one(index, queue, root, claim, *, recover=False,
                require_vector=False, prepare_vector=None):
    """Process one retained job; recover requires external old-owner-death proof.

    Returns ack, replan, deferred, unavailable or lease_lost. Every nonterminal
    return releases this owner's lease; a lost lease never releases another owner.
    """
    return _process_leased(queue, claim, recover, lambda job, token: _run_leased(
        index, queue, root, job, token, require_vector=require_vector,
        prepare_vector=prepare_vector))


def process_bound_one(index, queue, catalog, store, claim, *, recover=False):
    """``process_one`` for a bound catalog claim; same returns and lease rules.

    Lexical only: a vector seal is not prepared on this path. A receipt
    already in A-int is ACKed without re-staging. Otherwise the job's stored
    binding must exist and still describe the catalog's current COMPLETE
    snapshot; a newer published snapshot, changed member, exclusion epoch or
    moved head is ``replan`` and nothing is published. A stored binding that
    cannot be read is ``replan`` too, unless the store is only busy
    (``deferred``).
    """
    return _process_leased(queue, claim, recover, lambda job, token: _run_bound_leased(
        index, queue, catalog, store, job, token))
