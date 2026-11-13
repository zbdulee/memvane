"""Public journal selection and worker proof operations."""
from __future__ import annotations



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
