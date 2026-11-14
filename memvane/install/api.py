"""Public installation operations."""
from __future__ import annotations


def wake_status(project_raw, *, queue=None):
    """``{wake, overflow, cancellations}`` of the project's ``laya-sync`` wake row (read-only)."""
    from memvane.install.runtime_wake_cancel import status as implementation
    return implementation(project_raw, queue=queue)


def install_lock(parent):
    from memvane.install.locks import install_lock as implementation
    return implementation(parent)


def build_lookup(project_root):
    from memvane.install.initial_build import lookup as implementation
    return implementation(project_root)


def build_owner(state_root):
    from memvane.install.initial_build import owner as implementation
    return implementation(state_root)


def build_advance_v1(record, *, phase, reason=None):
    from memvane.install.initial_build import advance_v1 as implementation
    return implementation(record, phase=phase, reason=reason)


def plan_fresh_wiki_origin(project_root):
    from memvane.install.application.onboarding_workflow import plan_fresh_wiki_origin as implementation
    return implementation(project_root)


def review_local_provider(*, plugin_root, state_root, model_path=None, dimension=None,
                          runtime_generation=None, provider=None):
    from memvane.install.application.onboarding_workflow import review_local_provider as implementation
    return implementation(plugin_root=plugin_root, state_root=state_root, model_path=model_path,
                          dimension=dimension, runtime_generation=runtime_generation,
                          provider=provider)


def prepare_fresh_wiki_origin(*, project_root, plugin_root, state_root, expected_proposal_sha256,
                              install_id, accepted_provider_sha256, model_path=None,
                              dimension=None, provider=None):
    """``provider``: a fixture provider port (``provider.api.fixture_port``) for review and proofs."""
    from memvane.install.application.onboarding_workflow import prepare_fresh_wiki_origin as implementation
    return implementation(project_root=project_root, plugin_root=plugin_root,
                          state_root=state_root,
                          expected_proposal_sha256=expected_proposal_sha256,
                          install_id=install_id,
                          accepted_provider_sha256=accepted_provider_sha256,
                          model_path=model_path, dimension=dimension, provider=provider)


def index_origin_status(state_root):
    """``{status: 'inspected', journal, routeFile[, initialBuild]}`` of one install state root."""
    from memvane.install.application.onboarding_workflow import index_origin_status as implementation
    return implementation(state_root)


def initial_build_status(state_root, *, project_root=None):
    """The build's ``{status, phase, reason, epoch}``; ``project_root`` must own the build when given."""
    from memvane.install.application.onboarding_workflow import initial_build_status as implementation
    return implementation(state_root, project_root=project_root)


def request_initial_build_rescan(state_root, *, project_root=None):
    from memvane.install.application.onboarding_workflow import request_initial_build_rescan as implementation
    return implementation(state_root, project_root=project_root)


def finalize_initial_build(*, project_root, state_root, resolve_sync_target):
    """Seal a ready build; ``resolve_sync_target(project)`` is the caller's sync dispatch check."""
    from memvane.install.application.onboarding_activation import finalize_initial_build as implementation
    return implementation(project_root=project_root, state_root=state_root,
                          resolve_sync_target=resolve_sync_target)


def activate_origin_selection(project_root, journal_path, route_bytes):
    """Publish the proved selection; the caller holds the state root's ``install_lock``."""
    from memvane.install.application.onboarding_activation import activate_origin_selection as implementation
    return implementation(project_root, journal_path, route_bytes)


def recover_origin_selection(project_root, journal_path, route_bytes):
    """Finish a started activation or abandon; the caller holds the ``install_lock``."""
    from memvane.install.application.onboarding_activation import recover_origin_selection as implementation
    return implementation(project_root, journal_path, route_bytes)


def select_origin(*, project_root, state_root, route_file=None, recover=False):
    """Activate (``recover=True``: recover) from a state root under its install lock."""
    from memvane.install.application.onboarding_activation import select_origin as implementation
    return implementation(project_root=project_root, state_root=state_root,
                          route_file=route_file, recover=recover)


def restore_origin_selection(*, project_root, state_root):
    from memvane.install.application.onboarding_restore import restore_origin_selection as implementation
    return implementation(project_root=project_root, state_root=state_root)
