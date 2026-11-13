"""Public journal selection and worker proof operations."""
from __future__ import annotations
from importlib import import_module as _import_module

class _JournalOperations:
    """Read and journal operations kept behind the journal domain API."""

    def __getattr__(self, name):
        for module in ('journal_reader', 'reader_lease', 'journal_paths'):
            target = _import_module('memvane.journal.' + module)
            if hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)

class _InstallJournalOperations:
    """Install journal operations kept behind the journal domain API."""

    def __getattr__(self, name):
        return getattr(_import_module('memvane.journal.install_journal'), name)

_reader = _JournalOperations()
_install_journal = _InstallJournalOperations()

def _call(module, name, *args, **kwargs):
    return getattr(_import_module('memvane.journal.' + module), name)(*args, **kwargs)

def _api_attestation(*a, **k): return _call('install_journal', '_attestation', *a, **k)
def _api_route(*a, **k): return _call('install_journal', '_route', *a, **k)
def _api_v1_provider_manifest(*a, **k): return _call('install_journal', '_v1_provider_manifest', *a, **k)
def _api_stage_v1_attestation(*a, **k): return _call('install_journal', 'stage_v1_attestation', *a, **k)
def _api_create_v1_journal(*a, **k): return _call('install_journal', 'create_v1_journal', *a, **k)
def _api_prepare_lifetime(*a, **k): return _call('reader_lease', '_prepare_lifetime', *a, **k)
def _api_reader_lifetime(*a, **k): return _call('reader_lease', 'reader_lifetime', *a, **k)
def _api_open(*a, **k): return _call('install_journal', '_open', *a, **k)
def _api_inspect_v1_row(*a, **k): return _call('install_journal', '_inspect_v1_row', *a, **k)
def _api_journal_directory(*a, **k): return _call('journal_paths', 'journal_directory', *a, **k)
def _api_resettle_selection(*a, **k): return _call('journal_reader', 'resettle_selection', *a, **k)

def __getattr__(name):
    try:
        return getattr(_install_journal, name)
    except AttributeError:
        return getattr(_reader, name)

def inspect_journal(*args, **kwargs):
    from memvane.journal.install_journal import inspect_journal as implementation
    return implementation(*args, **kwargs)

def cas_mark_new_write(*args, **kwargs):
    from memvane.journal.install_journal import cas_mark_new_write as implementation
    return implementation(*args, **kwargs)

def verify_selected_route(*args, **kwargs):
    from memvane.journal.install_journal import verify_selected_route as implementation
    return implementation(*args, **kwargs)

def verify_selected_route_for_write(*args, **kwargs):
    from memvane.journal.install_journal import verify_selected_route_for_write as implementation
    return implementation(*args, **kwargs)

def owned_selected_proof(*args, **kwargs):
    from memvane.journal.application.selected_proof import owned_selected_proof as implementation
    return implementation(*args, **kwargs)

def selected_project_proof(*args, **kwargs):
    from memvane.journal.application.selected_proof import selected_project_proof as implementation
    return implementation(*args, **kwargs)

def verify_dispatch_target(*args, **kwargs):
    from memvane.journal.install_journal import verify_dispatch_target as implementation
    return implementation(*args, **kwargs)

def verify_notification_target(*args, **kwargs):
    from memvane.journal.install_journal import verify_notification_target as implementation
    return implementation(*args, **kwargs)

def verify_preview_worker_attestation(*args, **kwargs):
    from memvane.journal.install_journal import verify_preview_worker_attestation as implementation
    return implementation(*args, **kwargs)

def read_only_journals(*args, **kwargs):
    from memvane.journal.install_journal import read_only_journals as implementation
    return implementation(*args, **kwargs)

def reader_deadline(*args, **kwargs):
    from memvane.journal.install_journal import reader_deadline as implementation
    return implementation(*args, **kwargs)

def journal_version(*args, **kwargs):
    from memvane.journal.install_journal import journal_version as implementation
    return implementation(*args, **kwargs)

def legacy_identity_reads(*args, **kwargs):
    from memvane.journal.install_journal import legacy_identity_reads as implementation
    return implementation(*args, **kwargs)

def recheck_reader(*args, **kwargs):
    from memvane.journal.journal_reader import _recheck as implementation
    return implementation(*args, **kwargs)

def pin_schema3(*args, **kwargs):
    from memvane.journal.journal_reader import _schema3_pin as implementation
    return implementation(*args, **kwargs)

def settings_resettle_pending(*args, **kwargs):
    from memvane.journal.journal_reader import settings_resettle_pending as implementation
    return implementation(*args, **kwargs)

def resolve_endpoint(*args, **kwargs):
    from memvane.journal.reader_lease import resolve_endpoint as implementation
    return implementation(*args, **kwargs)

def service_worker_attestation(*args, **kwargs):
    from memvane.journal.application.service_proof import service_worker_attestation as implementation
    return implementation(*args, **kwargs)

class _BuildOperations:
    def canonical(self, *args, **kwargs):
        from memvane.journal.build_record import _canon as implementation
        return implementation(*args, **kwargs)

    def private_dir(self, *args, **kwargs):
        from memvane.journal.build_record import _private_dir as implementation
        return implementation(*args, **kwargs)

    def read_json(self, *args, **kwargs):
        from memvane.journal.build_record import _read_json as implementation
        return implementation(*args, **kwargs)

    def read_v1(self, *args, **kwargs):
        from memvane.journal.build_record import read_v1 as implementation
        return implementation(*args, **kwargs)

    def lookup(self, *args, **kwargs):
        from memvane.journal.initial_build import lookup as implementation
        return implementation(*args, **kwargs)

    def owner(self, *args, **kwargs):
        from memvane.journal.initial_build import owner as implementation
        return implementation(*args, **kwargs)

    def create_v1(self, *args, **kwargs):
        from memvane.journal.initial_build import create_v1 as implementation
        return implementation(*args, **kwargs)

    def register_project(self, *args, **kwargs):
        from memvane.journal.initial_build import register_project as implementation
        return implementation(*args, **kwargs)

    def advance_v1(self, *args, **kwargs):
        from memvane.journal.initial_build import advance_v1 as implementation
        return implementation(*args, **kwargs)

    def registered_pointer_path(self, *args, **kwargs):
        from memvane.journal.initial_build import registered_pointer_path as implementation
        return implementation(*args, **kwargs)

    @property
    def V1_BUILD_FILE(self):
        from memvane.journal.types import V1_BUILD_FILE
        return V1_BUILD_FILE

    @property
    def V1_BUILD_SCHEMA(self):
        from memvane.journal.types import V1_BUILD_SCHEMA
        return V1_BUILD_SCHEMA

build = _BuildOperations()

def ledger_private_file(*args, **kwargs):
    from memvane.journal.ledger_io import _private_file as implementation
    return implementation(*args, **kwargs)

def ledger_read_json(*args, **kwargs):
    from memvane.journal.ledger_io import _read_json as implementation
    return implementation(*args, **kwargs)

def ledger_write(*args, **kwargs):
    from memvane.journal.ledger_io import _write as implementation
    return implementation(*args, **kwargs)

def reader_state(*args, **kwargs):
    from memvane.journal.journal_reader import _state as implementation
    return implementation(*args, **kwargs)

def select_notification_route(*args, **kwargs):
    from memvane.journal.notification_route import select_notification_route as implementation
    return implementation(*args, **kwargs)

def verify_preview_route(*args, **kwargs):
    from memvane.journal.preview_proof import verify_preview_route as implementation
    return implementation(*args, **kwargs)

def select_route(*args, **kwargs):
    from memvane.journal.route_selection import select_route as implementation
    return implementation(*args, **kwargs)

class _WriterOperations:
    def build_writer_scope(self, *args, **kwargs):
        from memvane.journal.writer_scope import build_writer_scope as implementation
        return implementation(*args, **kwargs)

    def selected_v1_writer_scope(self, *args, **kwargs):
        from memvane.journal.writer_scope import selected_v1_writer_scope as implementation
        return implementation(*args, **kwargs)

    def admission_proof(self, *args, **kwargs):
        from memvane.journal.writer_scope import admission_proof as implementation
        return implementation(*args, **kwargs)

    def writer_store(self, *args, **kwargs):
        from memvane.journal.writer_scope import writer_store as implementation
        return implementation(*args, **kwargs)

    def mark_new_write(self, *args, **kwargs):
        from memvane.journal.writer_scope import mark_new_write as implementation
        return implementation(*args, **kwargs)

    def validate_normal_writer(self, *args, **kwargs):
        from memvane.journal.writer_scope import validate_normal_writer as implementation
        return implementation(*args, **kwargs)

writer = _WriterOperations()
