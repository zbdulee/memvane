"""Reader endpoint and reader record checks used by recall, compile and setup."""
from __future__ import annotations

from pathlib import Path


def persisted_schema3(endpoint):
    """Whether ``endpoint`` is an issued, read-only, persisted schema3 endpoint."""
    from memvane.journal import journal_reader as journal
    return (type(endpoint) is journal.VerifiedEndpoint and endpoint in journal._issued
            and endpoint in journal._read_only and endpoint.record.get('store_format')=='schema3')


def bind_schema3(endpoint):
    """Require the issued schema3 endpoint and its live reader scope; returns the scope."""
    from memvane.journal import journal_reader, reader_lease
    return reader_lease._bound(endpoint, capability=journal_reader.SCHEMA3_CAPABILITY)


def reader_selection(endpoint):
    """The reader selection ``resolve_endpoint`` issued with ``endpoint``, or None."""
    from memvane.journal.reader_lease import _reader_selections
    return _reader_selections.get(endpoint)


def selected_journal_reader(path):
    """``(journal_path, endpoint_id, epoch, record)`` of a v1 journal's selected reader."""
    from memvane.journal import journal_reader as reader
    journal_path = reader._journal(path)
    endpoint, epoch, record = reader.selected_reader(journal_path)
    return journal_path, endpoint, epoch, record


def schema3_reader_repository(decision, record, journal_path):
    """Check a selected schema3 reader record against its install and store; returns the store row.

    The row is ``(repository_json,)`` of the store the record binds.
    """
    from memvane.journal import journal_reader as reader
    reader._need(record['install_id'] == decision['installId']
                 and record['scope'] == decision['scopeId']
                 and record['project'] == decision['projectRoot']
                 and record['state'] == str(journal_path.parent), 'reader_install_changed')
    state = Path(record['state'])
    actual = reader._schema3_store(state, Path(record['project']),
                                   {'scopeId': record['scope']}, decision,
                                   record['db_path'], record['worktree_id'])
    for key in ('db_path', 'incarnation', 'repository_digest', 'worktree_id',
                'worktree_binding', 'settings_policy_digest'):
        reader._need(actual[key] == record[key], 'reader_store_changed')
    reader._need(reader.persistent_node_equal(actual['db_node'], record['db_node']),
                 'reader_store_changed')
    header = reader._store_header(Path(record['db_path']), state)
    reader._need(header is not None
                 and reader._repository_digest(header['repository_json']) == record['repository_digest'],
                 'reader_store_changed')
    return (header['repository_json'],)


def schema3_selection_current(decision, endpoint, epoch):
    """Whether the compile route still selects ``endpoint`` at ``epoch`` over the same store."""
    from memvane.journal.api import select_route
    from memvane.journal import install_journal as journal
    from memvane.journal import journal_reader as reader
    if select_route(decision['projectRoot'], action='compile') != decision:
        return False
    with journal.read_only_journals():
        current_endpoint, current_epoch, record = reader.selected_reader(decision['journalPath'])
        if (current_endpoint != endpoint or current_epoch != epoch
                or record.get('store_format') != 'schema3'):
            return False
        current = reader._schema3_store(Path(record['state']), Path(record['project']),
                                        {'scopeId': record['scope']}, decision,
                                        record['db_path'], record['worktree_id'])
        return (all(current[key] == record[key] for key in
                    ('db_path', 'incarnation', 'repository_digest', 'worktree_id',
                     'worktree_binding', 'settings_policy_digest'))
                and reader.persistent_node_equal(current['db_node'], record['db_node']))


def schema3_reader_record(state, project, identity, decision, worktree_id, digest, row, spec,
                          head, head_row, *, fail):
    """The unselected schema3 reader record an initial build seals for ``head``."""
    from memvane.journal import journal_reader as reader
    store_evidence = reader._schema3_store(state, project,
        {'scopeId': identity['scopeId']}, decision,
        identity['storePath'], worktree_id)
    sealed = dict(install_id=identity['installId'], project=str(project),
        project_node=reader._node(project), state=str(state),
        state_node=reader._node(state), scope=identity['scopeId'],
        attestation_sha256=digest, profile=row['vectorProfile']['profileId'],
        settings=reader._sha(reader._raw(decision['recallSettings'])),
        roots=decision['collections'], store_format='schema3',
        capability=reader.SCHEMA3_CAPABILITY, checkout_policy=reader.SCHEMA3_POLICY,
        vector_profile=spec['profile_id'], vector_recipe=spec['recipe_id'],
        head_floor={'view_id': head['view_id'], 'epoch': head['epoch'],
                    'ordinal': head_row[0]},
        creation_epoch=(row['reader']['creation_epoch'] if row['reader'] else row['epoch'] + 1),
        **store_evidence)
    if not reader._schema3_shape(sealed):
        fail('initial_build_reader_invalid')
    return sealed
