"""Installation facade for journal-owned initial-build records."""
from memvane.journal.api import build as _build

SCHEMA = 'laya-initial-build-v1'
POINTER_SCHEMA = 'laya-initial-build-pointer-v1'
RECORD = 'initial-build.json'
LOCK = 'initial-build.lock'
POINTERS = 'initial-builds'
RETIRED_POINTER = 'retired-build-pointer.json'


def lookup(*args, **kwargs):
    return _build.lookup(*args, **kwargs)


def owner(*args, **kwargs):
    return _build.owner(*args, **kwargs)


def create_v1(*args, **kwargs):
    return _build.create_v1(*args, **kwargs)


def register_project(*args, **kwargs):
    return _build.register_project(*args, **kwargs)


def read_v1(*args, **kwargs):
    return _build.read_v1(*args, **kwargs)


def advance_v1(*args, **kwargs):
    return _build.advance_v1(*args, **kwargs)


def registered_pointer_path(*args, **kwargs):
    return _build.registered_pointer_path(*args, **kwargs)


V1_BUILD_FILE = _build.V1_BUILD_FILE
V1_BUILD_SCHEMA = _build.V1_BUILD_SCHEMA
