"""Installation facade for journal-owned initial-build records."""
from memvane.journal import api as _journal


def lookup(project_root):
    return _journal.build_lookup(project_root)


def owner(state_root):
    return _journal.build_owner(state_root)


def create_v1(identity):
    return _journal.build_create_v1(identity)


def register_project(identity):
    return _journal.build_register_project(identity)


def read_v1(state_root):
    return _journal.build_read_v1(state_root)


def advance_v1(record, *, phase, reason=None):
    return _journal.build_advance_v1(record, phase=phase, reason=reason)


def registered_pointer_path(project_root):
    return _journal.build_registered_pointer_path(project_root)


def retire_project_pointer(identity):
    return _journal.build_retire_project_pointer(identity)

