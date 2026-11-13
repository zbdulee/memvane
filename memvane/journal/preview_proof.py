"""Read-only proof for an initial build preview."""
from __future__ import annotations

from pathlib import Path
from memvane.route.api import select_route
from memvane.route.types import SCHEMA_V4


def verify_preview_route(decision: dict) -> dict:
    """Reprove a pending v1 build and its provider identity."""
    if not isinstance(decision, dict) or decision.get('state') != 'preview':
        raise ValueError('initial_preview_unselected')
    live = select_route(decision.get('projectRoot', ''), action='recall')
    if live != decision:
        raise ValueError('initial_preview_changed')
    if decision.get('routeSchema') == SCHEMA_V4:
        from memvane.app import runtime_dispatch as dispatch
        from memvane.journal.install_journal import inspect_journal, _attestation, _v1_provider_manifest
        dispatch.resolve('laya-sync', decision['projectRoot'])
        journal = inspect_journal(decision['journalPath'])
        attestation, digest = _attestation(Path(decision['journalPath']))
        if journal['state'] != 'prepared' or digest != journal['attestationSha256']:
            raise ValueError('initial_preview_journal_mismatch')
        manifest = _v1_provider_manifest(attestation, journal['vectorProfile'])
        if select_route(decision['projectRoot'], action='recall') != decision:
            raise ValueError('initial_preview_changed')
        return {'journalEpoch': journal['epoch'], 'vectorProfile': journal['vectorProfile'],
                'providerManifest': manifest}
    raise ValueError('initial_preview_unselected')
