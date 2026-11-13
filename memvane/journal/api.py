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


def verify_dispatch_target(*args, **kwargs):
    from memvane.journal.install_journal import verify_dispatch_target as implementation
    return implementation(*args, **kwargs)


def verify_notification_target(*args, **kwargs):
    from memvane.journal.install_journal import verify_notification_target as implementation
    return implementation(*args, **kwargs)


def verify_preview_worker_attestation(*args, **kwargs):
    from memvane.journal.install_journal import verify_preview_worker_attestation as implementation
    return implementation(*args, **kwargs)


def service_worker_attestation(*args, **kwargs):
    from memvane.journal.application.service_proof import service_worker_attestation as implementation
    return implementation(*args, **kwargs)
