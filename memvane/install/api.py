"""Public installation operations."""
from __future__ import annotations


def cancel_wake(project_raw, revision, backup_dir, *, queue=None):
    from memvane.install.runtime_wake_cancel import cancel as implementation
    return implementation(project_raw, revision, backup_dir, queue=queue)


def wake_status(project_raw, *, queue=None):
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


def build_create_v1(identity):
    from memvane.install.initial_build import create_v1 as implementation
    return implementation(identity)


def build_register_project(identity):
    from memvane.install.initial_build import register_project as implementation
    return implementation(identity)


def build_read_v1(state_root):
    from memvane.install.initial_build import read_v1 as implementation
    return implementation(state_root)


def build_advance_v1(record, *, phase, reason=None):
    from memvane.install.initial_build import advance_v1 as implementation
    return implementation(record, phase=phase, reason=reason)


def build_retire_project_pointer(identity):
    from memvane.install.initial_build import retire_project_pointer as implementation
    return implementation(identity)


def ledger_read_json(path):
    from memvane.install.ledger_common import _read_json as implementation
    return implementation(path)


def ledger_write(path, value, *, replace):
    from memvane.install.ledger_common import _write as implementation
    return implementation(path, value, replace=replace)


def ledger_fsync_dir(path):
    from memvane.install.ledger_common import _fsync_dir as implementation
    return implementation(path)


def plan_fresh_wiki_origin(project_root):
    from memvane.install.application.onboarding_workflow import plan_fresh_wiki_origin as implementation
    return implementation(project_root)


def review_local_provider(*, plugin_root, state_root, model_path=None, dimension=None,
                          runtime_generation=None):
    from memvane.install.application.onboarding_workflow import review_local_provider as implementation
    return implementation(plugin_root=plugin_root, state_root=state_root, model_path=model_path,
                          dimension=dimension, runtime_generation=runtime_generation)


def prepare_fresh_wiki_origin(*, project_root, plugin_root, state_root, expected_proposal_sha256,
                              install_id, accepted_provider_sha256, model_path=None,
                              dimension=None):
    from memvane.install.application.onboarding_workflow import prepare_fresh_wiki_origin as implementation
    return implementation(project_root=project_root, plugin_root=plugin_root,
                          state_root=state_root,
                          expected_proposal_sha256=expected_proposal_sha256,
                          install_id=install_id,
                          accepted_provider_sha256=accepted_provider_sha256,
                          model_path=model_path, dimension=dimension)


def initial_build_status(state_root):
    from memvane.install.application.onboarding_workflow import initial_build_status as implementation
    return implementation(state_root)


def request_initial_build_rescan(state_root):
    from memvane.install.application.onboarding_workflow import request_initial_build_rescan as implementation
    return implementation(state_root)


def restore_origin_selection(*, project_root, state_root):
    from memvane.install.application.onboarding_restore import restore_origin_selection as implementation
    return implementation(project_root=project_root, state_root=state_root)


def restore_selection_locked(project, state):
    """Restore under an install lock the caller already holds."""
    from memvane.install.application.onboarding_restore import _restore_v1_locked as implementation
    return implementation(project, state)


def refuse(reason):
    """Raise ``InstallBlocked(reason)``."""
    from memvane.install.application.onboarding_workflow import _fail as implementation
    return implementation(reason)


def unique_keys(pairs):
    """``json`` object hook that refuses a duplicate key."""
    from memvane.install.application.onboarding_workflow import _unique as implementation
    return implementation(pairs)


def checked_project(project_root):
    from memvane.install.application.onboarding_workflow import _checked_project as implementation
    return implementation(project_root)


def private_parent(path):
    from memvane.install.application.onboarding_workflow import _private_parent as implementation
    return implementation(path)


def read_private(path, limit):
    from memvane.install.application.onboarding_workflow import _read_private as implementation
    return implementation(path, limit)


def write_or_verify_private(path, raw):
    from memvane.install.application.onboarding_workflow import _write_or_verify_private as implementation
    return implementation(path, raw)


def project_pointers_present(project_root):
    from memvane.install.application.onboarding_workflow import _project_pointers_present as implementation
    return implementation(project_root)


def request_build_wake(project_root):
    from memvane.install.application.onboarding_workflow import _request_build_wake as implementation
    return implementation(project_root)
