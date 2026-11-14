"""Selected worker attestation and journal transition checks."""
from __future__ import annotations

from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.journal.api import select_route
from memvane.journal.install_journal import (verify_selected_route_for_write as verify_selected_route,
                                             verify_worker_attestation)


def service_worker_attestation(decision, expected):
    """Reprove the selected installation around worker attestation."""
    view = _journal_view(decision)
    try:
        selected = verify_selected_route(decision)
        if _without_epoch(selected) != _without_epoch(expected):
            raise ResourceDeferred('worker_selection_changed')
        manifest = _selection_moves_defer(verify_worker_attestation, decision)
        if _without_epoch(verify_selected_route(decision)) != _without_epoch(selected):
            raise ResourceDeferred('worker_selection_changed')
        return manifest
    except ResourceDeferred:
        raise
    except Exception as exc:
        if _selection_moved(decision, view):
            raise ResourceDeferred('worker_selection_changed') from exc
        raise


# Route fields that name one selected installation and its single store.
_SELECTION_KEYS = ('state', 'routeSchema', 'projectRoot', 'stateRoot', 'installId',
                   'journalPath', 'storePath', 'storeIncarnation', 'scopeId',
                   'stateFormat', 'collections', 'vectorProfile')
# Journal fields a sync's write changes without moving the selection.
_JOURNAL_EPOCH_FIELDS = ('epoch', 'newWriteSeen')


def _journal_view(decision):
    """The journal row as a selection, epoch aside; None if unreadable."""
    from memvane.journal import install_journal as journal
    try:
        with journal.read_only_journals():
            info = journal.inspect_journal(decision['journalPath'])
    except Exception:
        return None
    return {key: value for key, value in info.items() if key not in _JOURNAL_EPOCH_FIELDS}


def _selection_moved(decision, view):
    """Whether the route or journal moved away from the accepted selection.

    Read-only: the route classification and the journal row (never a
    content store). The route must still name ``decision``'s installation,
    store and profile in the same state; the journal row, epoch aside, must
    equal ``view``. A read that fails is not evidence of a move: the failure
    stays what it was.
    """
    from memvane.journal import install_journal as journal
    try:
        with journal.read_only_journals():
            current = select_route(str(decision['projectRoot']), action='update')
    except Exception:
        return False
    if any(current.get(key) != decision.get(key) for key in _SELECTION_KEYS):
        return True
    now = _journal_view(decision)
    return now is not None and view is not None and now != view


def _selection_moves_defer(call, argument):
    """``worker_selection_changed`` from the journal is a moved selection: defer."""
    from memvane.journal import install_journal as journal
    try:
        return call(argument)
    except (journal.JournalEvidenceError, IndexContractError) as exc:
        if str(exc) == 'worker_selection_changed':
            raise ResourceDeferred('worker_selection_changed') from exc
        raise


def _without_epoch(proof):
    return {key: value for key, value in proof.items() if key != 'selectionEpoch'}


def worker_provider_manifest(decision, proof):
    """The worker attestation's provider manifest for the current proof (closure rehash)."""
    from pathlib import Path
    from memvane.journal import install_journal as journal
    with journal.read_only_journals():
        if proof.get('buildPending'):
            from memvane.journal.dispatch_target import dispatch_target
            dispatch_target(decision['projectRoot'], build=True)
            row = journal.inspect_journal(decision['journalPath'])
            attestation, digest = journal._attestation(Path(decision['journalPath']))
            if row['state'] != 'prepared' or digest != row['attestationSha256']:
                raise IndexContractError('worker_attestation_changed')
            return journal._v1_provider_manifest(attestation, row['vectorProfile'])
        if journal.journal_version(decision['journalPath']) not in (1, 5):
            raise IndexContractError('writer_plan_v5_required')
        return journal._worker_attestation_manifest(proof)


def worker_attestation(decision, proof, mode):
    """The provider manifest a ``selected`` or ``preview`` worker attestation proves; None otherwise."""
    if mode == 'selected':
        return service_worker_attestation(decision, proof)
    if mode == 'preview':
        from memvane.journal.install_journal import verify_preview_worker_attestation
        return verify_preview_worker_attestation(decision['journalPath'], proof['providerManifest'])
    return None
