"""Selected route proof while holding the existing sync owner."""
from __future__ import annotations

from pathlib import Path
from memvane.kernel.errors import IndexContractError
from memvane.journal.api import select_route


def selected_project_proof(project, *, owner_wait=False):
    """Prove the selected v1 route before acquiring the writer owner."""
    from memvane.journal.api import verify_selected_route_for_write
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise IndexContractError('selected_project_unavailable')
    decision = select_route(str(project), action='update')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        raise IndexContractError('selected_route_unavailable')
    proof = verify_selected_route_for_write(decision)
    if proof.get('productRouteAllowed') is not True or not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('selected_provider_unavailable')
    return decision, proof


def owned_selected_proof(decision, owner):
    """Reprove the selected route while the sync owner is held."""
    from memvane.runtime.api import validate_sync_owner
    from memvane.journal.api import verify_selected_route_for_write
    validate_sync_owner(owner, Path(decision['storePath']))
    proof = verify_selected_route_for_write(decision)
    validate_sync_owner(owner, Path(decision['storePath']))
    return proof
