"""Context-learning evidence from a selected Laya route; never reads QMD.

A project whose installer selected the memvane-db index serves recall, edits and
index publishes from memvane-db only. Learning reads the same evidence:

* corpus fingerprint: the published, sealed head generation of the route's
  scope (eligible, non-excluded membership rows and their content hashes);
* capture: the verified rows the query-time selector already scored (no
  source file re-read);
* readiness: an installer-verified route, instead of a QMD store pointer.

``selected`` distinguishes a legacy project (``None``) from a selected route
that cannot be verified (``ValueError``); the latter never falls back to QMD.
"""
from __future__ import annotations

from contextlib import closing
import os
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
    from laya_index_host_route import select_route
    root = Path(project_root).resolve(strict=True)
    decision = select_route(str(root), action=action)
    state = decision.get('state') if isinstance(decision, dict) else None
    if state == 'legacy':
        return None
    if (state != 'candidate' or decision.get('projectRoot') != str(root)
            or 'collections' not in decision):
        raise ValueError('laya_learning_route_unavailable')
    from laya_install_journal import verify_selected_route
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
    from laya_index_host_route import wiki_index_roots
    return tuple(sorted(wiki_index_roots(decision['collections'])))


def _open(decision):
    from laya_incremental_docs_query import _readonly
    db = _readonly(Path(decision['dbPath']))
    incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
    if incarnation != (decision['dbIncarnation'],):
        db.close()
        raise ValueError('laya_learning_incarnation_changed')
    return db


def _sealed_head(db, scope):
    head = db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
    if head is None:
        raise ValueError('laya_learning_head_missing')
    generation = db.execute('SELECT state FROM generations WHERE gen=? AND scope=?',
                            (head[0], scope)).fetchone()
    # Same rule as LayaIndexA.view_seal: never read an unsealed prototype view.
    sealed = db.execute('SELECT 1 FROM candidate_seals WHERE gen=? AND scope=?',
                        (head[0], scope)).fetchone()
    if generation != ('published',) or sealed is None:
        raise ValueError('laya_learning_head_unsealed')
    return head[0]


def corpus_snapshot(project_root, config, *, decision=None):
    """Deterministic candidate-corpus fingerprint of the sealed Laya head.

    The fingerprint covers the learning policy, the scope, the vector profile
    and every eligible, non-excluded ``[collection, relative, sha256]`` row of
    the head generation. A republish with identical rows keeps the
    fingerprint; the generation id is reported beside it, not inside it.
    Returns ``None`` (never a guessed hash) when evidence is unavailable.
    """
    from laya_index_a import _NOT_EXCLUDED
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
        with closing(_open(decision)) as db:
            generation = _sealed_head(db, scope)
            docs = db.execute(
                'SELECT m.path,m.hash FROM membership m WHERE m.gen=? AND m.scope=? '
                'AND m.eligible=1 AND ' + _NOT_EXCLUDED + ' ORDER BY m.path COLLATE BINARY',
                (generation, scope)).fetchmany(MAX_DOCUMENTS + 1)
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


def _log(project_root, status):
    log_path = os.environ.get('QMD_RECALL_LOG')
    if not log_path:
        return
    try:
        import wiki_card
        wiki_card.set_log_project_root(project_root)
        wiki_card.log_recall_event(log_path, 'learning_capture', route='laya',
                                event_override='qmd_context_capture', status=status)
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
    _log(decision.get('projectRoot') if isinstance(decision, dict) else None, status)
    return status
