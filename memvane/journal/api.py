"""Public journal selection and worker proof operations."""


def select_route(cwd, *, action='recall', linked=True):
    from memvane.journal.route_selection import select_route as implementation
    return implementation(cwd, action=action, linked=linked)

def inspect_journal(journal_path):
    from memvane.journal.install_journal import inspect_journal as implementation
    return implementation(journal_path)

def verify_selected_route(decision):
    from memvane.journal.install_journal import verify_selected_route as implementation
    return implementation(decision)

def verify_selected_route_for_write(decision, action='update'):
    from memvane.journal.install_journal import verify_selected_route_for_write as implementation
    return implementation(decision, action)

def owned_selected_proof(decision, owner):
    from memvane.journal.application.selected_proof import owned_selected_proof as implementation
    return implementation(decision, owner)

def selected_project_proof(project, *, owner_wait=False):
    from memvane.journal.application.selected_proof import selected_project_proof as implementation
    return implementation(project, owner_wait=owner_wait)

def dispatch_target(project, *, build):
    from memvane.journal.dispatch_target import dispatch_target as implementation
    return implementation(project, build=build)

def select_notification_route(project):
    from memvane.journal.notification_route import select_notification_route as implementation
    return implementation(project)

def verify_notification_target(decision):
    from memvane.journal.install_journal import verify_notification_target as implementation
    return implementation(decision)

def verify_preview_route(decision):
    from memvane.journal.preview_proof import verify_preview_route as implementation
    return implementation(decision)

def worker_attestation(decision, proof, mode):
    from memvane.journal.application.service_proof import worker_attestation as implementation
    return implementation(decision, proof, mode)

def worker_provider_manifest(decision, proof):
    from memvane.journal.application.service_proof import worker_provider_manifest as implementation
    return implementation(decision, proof)

def read_only_journals(deadline=None):
    from memvane.journal.application.journal_reads import read_only_journals as implementation
    return implementation(deadline)

def journal_version(journal_path):
    from memvane.journal.install_journal import journal_version as implementation
    return implementation(journal_path)

def resolve_endpoint(project, *, action, host_root):
    from memvane.journal.reader_lease import resolve_endpoint as implementation
    return implementation(project, action=action, host_root=host_root)

def recheck_reader(endpoint):
    from memvane.journal.journal_reader import _recheck as implementation
    return implementation(endpoint)

def pin_schema3(record, *, deadline, linked=None, pin_view=True):
    from memvane.journal.journal_reader import _schema3_pin as implementation
    return implementation(record, deadline=deadline, linked=linked, pin_view=pin_view)

def settings_resettle_pending(state, project, executing_root):
    from memvane.journal.journal_reader import settings_resettle_pending as implementation
    return implementation(state, project, executing_root)

def resettle_selection(decision, owner):
    from memvane.journal.journal_reader import resettle_selection as implementation
    return implementation(decision, owner)

def reader_state(state):
    from memvane.journal.journal_reader import _state as implementation
    return implementation(state)

def ledger_private_file(path, limit=65536):
    from memvane.journal.ledger_io import _private_file as implementation
    return implementation(path, limit)

def ledger_read_json(path):
    from memvane.journal.ledger_io import _read_json as implementation
    return implementation(path)

def ledger_write(path, value, *, replace):
    from memvane.journal.ledger_io import _write as implementation
    return implementation(path, value, replace=replace)

def build_writer_scope(decision, *, queue_owner):
    from memvane.journal.writer_scope import build_writer_scope as implementation
    return implementation(decision, queue_owner=queue_owner)

def selected_v1_writer_scope(decision, *, queue_owner):
    from memvane.journal.writer_scope import selected_v1_writer_scope as implementation
    return implementation(decision, queue_owner=queue_owner)

def seal_reader(journal_path, row, sealed, spec, *, fail):
    from memvane.journal.application.selection_transitions import seal_reader as implementation
    return implementation(journal_path, row, sealed, spec, fail=fail)

def commit_selection(journal, row, *, fail):
    from memvane.journal.application.selection_transitions import commit_selection as implementation
    return implementation(journal, row, fail=fail)

def restore_selection(path, row, *, fail):
    from memvane.journal.application.selection_transitions import restore_selection as implementation
    return implementation(path, row, fail=fail)

def publish_plugin_version(decision, owner, running):
    from memvane.journal.application.selection_transitions import publish_plugin_version as implementation
    return implementation(decision, owner, running)

def persisted_schema3(endpoint):
    from memvane.journal.application.reader_checks import persisted_schema3 as implementation
    return implementation(endpoint)

def bind_schema3(endpoint):
    from memvane.journal.application.reader_checks import bind_schema3 as implementation
    return implementation(endpoint)

def reader_selection(endpoint):
    from memvane.journal.application.reader_checks import reader_selection as implementation
    return implementation(endpoint)

def selected_journal_reader(path):
    from memvane.journal.application.reader_checks import selected_journal_reader as implementation
    return implementation(path)

def schema3_reader_repository(decision, record, journal_path):
    from memvane.journal.application.reader_checks import schema3_reader_repository as implementation
    return implementation(decision, record, journal_path)

def schema3_selection_current(decision, endpoint, epoch):
    from memvane.journal.application.reader_checks import schema3_selection_current as implementation
    return implementation(decision, endpoint, epoch)

def schema3_reader_record(state, project, identity, decision, worktree_id, digest, row, spec,
                          head, head_row, *, fail):
    from memvane.journal.application.reader_checks import schema3_reader_record as implementation
    return implementation(state, project, identity, decision, worktree_id, digest, row, spec,
                          head, head_row, fail=fail)

def route_record(raw, journal_path):
    from memvane.journal.install_journal import _route as implementation
    return implementation(raw, journal_path)

def prepare_v1_journal(journal, reviewed_provider, route_raw, *, provider=None):
    from memvane.journal.application.install_setup import prepare_v1_journal as implementation
    return implementation(journal, reviewed_provider, route_raw, provider=provider)

def journal_attestation(path):
    from memvane.journal.install_journal import _attestation as implementation
    return implementation(path)

def provider_manifest(record, profile, *, allow_local_unavailable=False, provider=None):
    from memvane.journal.install_journal import _v1_provider_manifest as implementation
    return implementation(record, profile, allow_local_unavailable=allow_local_unavailable,
                          provider=provider)

def prepare_reader_lifetime(install_id, *, create=True):
    from memvane.journal.reader_lease import _prepare_lifetime as implementation
    return implementation(install_id, create=create)

def reader_lifetime(install_id, *, exclusive):
    from memvane.journal.reader_lease import reader_lifetime as implementation
    return implementation(install_id, exclusive=exclusive)

def journal_directory(*parts, create=False):
    from memvane.journal.journal_paths import journal_directory as implementation
    return implementation(*parts, create=create)

def build_lookup(project_root):
    from memvane.journal.initial_build import lookup as implementation
    return implementation(project_root)

def build_owner(state_root):
    from memvane.journal.initial_build import owner as implementation
    return implementation(state_root)

def build_create_v1(identity):
    from memvane.journal.initial_build import create_v1 as implementation
    return implementation(identity)

def build_register_project(identity):
    from memvane.journal.initial_build import register_project as implementation
    return implementation(identity)

def build_read_v1(state_root):
    from memvane.journal.build_record import read_v1 as implementation
    return implementation(state_root)

def build_advance_v1(record, *, phase, reason=None):
    from memvane.journal.initial_build import advance_v1 as implementation
    return implementation(record, phase=phase, reason=reason)

def build_registered_pointer_path(project_root):
    from memvane.journal.initial_build import registered_pointer_path as implementation
    return implementation(project_root)

def build_retire_project_pointer(identity):
    from memvane.journal.initial_build import retire_project_pointer as implementation
    return implementation(identity)
