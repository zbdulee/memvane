"""Context-learning evidence from a selected MemVane view."""
from __future__ import annotations

from pathlib import Path
import sys
import time

MAX_DOCUMENTS = 50000
SNAPSHOT_SCHEMA = 'laya-corpus-snapshot-v1'
# The query-time selector scores at most this many verified rows
# (``laya_host_response._select_verified_cards``); capture keeps that pool.
CAPTURE_POOL_LIMIT = 15
# Capture runs after the hook output is flushed. Past this point of the
# 22-second recall hook it is skipped rather than risk a host timeout.
CAPTURE_START_BUDGET_SECONDS = 12.0
_POLICY_KEYS = ('collections', 'collectionPaths', 'collectionRoles', 'recallStrategy',
                'minScore', 'rawFallbackMinScore', 'skipPaths', 'lexicalPatterns',
                'topN', 'excludeStatusesFromRecall', 'recallVerifiedOnly',
                'contextLearning')


def selected(project_root, *, action='recall'):
    """``(decision, proof)`` for a verified selected v2 route, ``None`` if legacy."""
    from memvane.route.index_host_route import select_route
    root = Path(project_root).resolve(strict=True)
    decision = select_route(str(root), action=action)
    state = decision.get('state') if isinstance(decision, dict) else None
    if state == 'legacy':
        return None
    if (state != 'candidate' or decision.get('projectRoot') != str(root)
            or 'collections' not in decision):
        raise ValueError('laya_learning_route_unavailable')
    from memvane.journal.install_journal import verify_selected_route
    proof = verify_selected_route(decision)
    if not isinstance(proof, dict) or proof.get('productRouteAllowed') is not True:
        raise ValueError('laya_learning_route_unverified')
    return decision, proof


def ready(project) -> bool:
    """Learning readiness: a verified selected Laya route."""
    try:
        return selected(project) is not None
    except Exception:
        return False


def wiki_names(decision):
    from memvane.route.index_host_route import wiki_index_roots
    return tuple(sorted(wiki_index_roots(decision['collections'])))


# One read of a schema3 view head's members (store open, Git capture, pin).
SCHEMA3_READ_SECONDS = 5


def _schema3_members(root, decision):
    """``(generation, [(collection/relative, sha256)])`` of a schema3 view head.

    The journal-selected reader and its pinned main-view head provide
    member paths and their raw-byte SHA-256 values.
    """
    from memvane.journal import journal_reader as reader
    from memvane.journal import reader_lease as lease
    from memvane.kernel.bundle_paths import plugin_root
    deadline = time.monotonic() + SCHEMA3_READ_SECONDS
    with lease.resolve_endpoint(root, action='recall', host_root=plugin_root()) as endpoint:
        record, _raw, _proof = reader._recheck(endpoint)
        names = [item['name'] for item in record['roots'] if item.get('role') == 'wiki']
        if (record.get('store_format') != 'schema3' or record['scope'] != decision['scopeId']
                or len(names) != 1):
            raise ValueError('laya_learning_schema3_endpoint_changed')
        with reader._schema3_pin(record, deadline=deadline) as (store, _handle, pin):
            with store._reading(deadline):
                # Either store format: the pinned generation's eligible cards in binary path order.
                rows = store._eligible_members(pin.gen_id, limit=MAX_DOCUMENTS + 1)
            generation = pin.gen_id
    return generation, [(names[0] + '/' + path, sha) for path, sha in rows]


def corpus_snapshot(project_root, config, *, decision=None):
    """Deterministic candidate-corpus fingerprint of the sealed Laya head.

    The fingerprint covers the learning policy, the scope, the vector profile
    and every eligible, non-excluded ``[collection, relative, sha256]`` row of
    the head generation. A republish with identical rows keeps the
    fingerprint; the generation id is reported beside it, not inside it.
    Returns ``None`` (never a guessed hash) when evidence is unavailable.
    """
    from .contracts import digest
    from .store import canonical
    try:
        root = Path(project_root).resolve(strict=True)
        if not isinstance(config, dict):
            return None
        if decision is None:
            bound = selected(root)
            if bound is None:
                return None
            decision = bound[0]
        names = set(wiki_names(decision))
        scope = decision['scopeId']
        generation, docs = _schema3_members(root, decision)
        if len(docs) > MAX_DOCUMENTS:
            return None
        active = []
        for path, revision in docs:
            collection, sep, relative = path.partition('/')
            if (not sep or collection not in names or not relative
                    or not isinstance(revision, str) or len(revision) != 64):
                return None
            active.append([collection, relative, revision])
        material = {'schema': SNAPSHOT_SCHEMA, 'projectRoot': str(root),
                    'policy': {key: config.get(key) for key in _POLICY_KEYS},
                    'scopeId': scope,
                    'vectorProfileId': decision['vectorProfile']['profileId'],
                    'activeDocuments': active}
        return {'fingerprint': digest(canonical(material)), 'material': material,
                'activeDocuments': len(active), 'generation': generation}
    except Exception:
        return None


def _log(decision, host, status):
    """One ``learning.capture`` event in the install log; never raises."""
    try:
        from memvane.diagnostics import api as event_log
        event_log.emit('learning.capture', outcome=status, decision=decision, host=host)
    except Exception:
        pass


def capture_recall(host, decision, payload, settings, pool, chosen, *, started=None):
    """Post-output capture of the verified pool the selector scored.

    Failure-silent: returns a fixed status, logs it, and never raises. The
    hook response has already been written when this runs.
    """
    status = 'capture_unavailable'
    try:
        if (started is not None
                and time.monotonic() - started > CAPTURE_START_BUDGET_SECONDS):
            status = 'capture_deadline'
        elif not isinstance(settings, dict) or not isinstance(
                settings.get('contextLearning'), dict) or settings[
                'contextLearning'].get('capture') is not True:
            status = 'disabled'
        else:
            from .seam import observe_published
            status = observe_published(payload, settings, decision['projectRoot'],
                                       list(pool)[:CAPTURE_POOL_LIMIT], list(chosen),
                                       host=host, decision=decision)
    except Exception:
        status = 'capture_unavailable'
    _log(decision if isinstance(decision, dict) else None, host, status)
    return status
