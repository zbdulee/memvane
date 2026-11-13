"""Selected route proof while holding the existing sync owner."""
from __future__ import annotations

from pathlib import Path


def owned_selected_proof(decision, owner):
    """Reprove the selected route while the sync owner is held."""
    from memvane.runtime.api import validate_sync_owner
    from memvane.journal.install_journal import verify_selected_route_for_write
    validate_sync_owner(owner, Path(decision['storePath']))
    proof = verify_selected_route_for_write(decision)
    validate_sync_owner(owner, Path(decision['storePath']))
    return proof
