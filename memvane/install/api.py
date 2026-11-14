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
