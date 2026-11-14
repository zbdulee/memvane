"""Installation facade for journal-owned initial-build records."""
from memvane.journal import api as _journal
from memvane.journal.types import V1_BUILD_FILE, V1_BUILD_SCHEMA

SCHEMA = 'laya-initial-build-v1'
POINTER_SCHEMA = 'laya-initial-build-pointer-v1'
RECORD = 'initial-build.json'
LOCK = 'initial-build.lock'
POINTERS = 'initial-builds'
RETIRED_POINTER = 'retired-build-pointer.json'


def lookup(*args, **kwargs):
    return _journal.build_lookup(*args, **kwargs)


def owner(*args, **kwargs):
    return _journal.build_owner(*args, **kwargs)


def create_v1(*args, **kwargs):
    return _journal.build_create_v1(*args, **kwargs)


def register_project(*args, **kwargs):
    return _journal.build_register_project(*args, **kwargs)


def read_v1(*args, **kwargs):
    return _journal.build_read_v1(*args, **kwargs)


def advance_v1(*args, **kwargs):
    return _journal.build_advance_v1(*args, **kwargs)


def registered_pointer_path(*args, **kwargs):
    return _journal.build_registered_pointer_path(*args, **kwargs)


def retire_project_pointer(*args, **kwargs):
    return _journal.build_retire_project_pointer(*args, **kwargs)

