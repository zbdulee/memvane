"""Compare-and-set transitions of the v1 selection and reader rows.

Each transition opens the install journal for write, re-reads the v1 row
under ``BEGIN IMMEDIATE`` and refuses when it differs from the row the
caller inspected. ``fail`` raises the caller's refusal for a reason code.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path


def seal_reader(journal_path, row, sealed, spec, *, fail):
    """Insert or reseal the unselected reader record in one journal CAS."""
    from memvane.journal import install_journal as journal
    from memvane.journal import journal_reader as reader
    path, db = journal._open(journal_path, write=True)
    with closing(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            current_row = journal._inspect_v1_row(db)
            if current_row != {k: v for k, v in row.items() if k != 'journalPath'}:
                fail('initial_build_journal_changed')
            stored = current_row['reader']
            if stored is not None:
                # Record nodes compare by inode: a reboot may renumber st_dev.
                sealed = reader._preserve_node_devices(stored, sealed)
            if stored != sealed:
                # Unselected (the CAS below): a newer ready head reseals the floor.
                if stored is None:
                    db.execute('INSERT INTO reader(id,record) VALUES(1,?)', (journal._canon(sealed),))
                elif db.execute('UPDATE reader SET record=? WHERE id=1',
                                (journal._canon(sealed),)).rowcount != 1:
                    fail('initial_build_journal_changed')
                changed = db.execute("UPDATE selection SET epoch=epoch+1,vector_recipe=? "
                    "WHERE id=1 AND epoch=? AND state='prepared'",
                    (spec['recipe_id'], row['epoch'])).rowcount
                if changed != 1:
                    fail('initial_build_journal_changed')
            journal._inspect_v1_row(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return path


def commit_selection(journal, row, *, fail):
    """Move a prepared selection to ``selected``; a selected one stays. Returns the journal path."""
    from memvane.journal import install_journal as journal_module
    path, db = journal_module._open(journal, write=True)
    with closing(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            current = journal_module._inspect_v1_row(db)
            if current != {k: v for k, v in row.items() if k != 'journalPath'}:
                fail('initial_build_journal_changed')
            if current['state'] == 'prepared':
                if db.execute("UPDATE selection SET state='selected',epoch=epoch+1 "
                              "WHERE id=1 AND epoch=? AND state='prepared'", (row['epoch'],)).rowcount != 1:
                    fail('initial_build_journal_changed')
            journal_module._inspect_v1_row(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return path


def restore_selection(path, row, *, fail):
    """Move a prepared or selected selection to ``restored``."""
    from memvane.journal import install_journal as journal
    _, db = journal._open(path, write=True)
    with closing(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            current = journal._inspect_v1_row(db)
            if current != {k: v for k, v in row.items() if k != 'journalPath'}:
                fail('initial_build_journal_changed')
            if db.execute("UPDATE selection SET state='restored',epoch=epoch+1 "
                          "WHERE id=1 AND state=? AND new_write_seen=? AND epoch=?",
                          (row['state'], int(row['newWriteSeen']), row['epoch'])).rowcount != 1:
                fail('restore_state_invalid')
            journal._inspect_v1_row(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise


def publish_plugin_version(decision, owner, running):
    """Raise the selected journal's highest plugin version to this one under the sync owner."""
    from memvane.journal.install_journal import _open, _inspect_v1_row
    from memvane.kernel.memvane_version import plugin_version, version_key
    from memvane.runtime.api import validate_sync_owner
    path, db = _open(decision['journalPath'], write=True)
    with closing(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            row = _inspect_v1_row(db)
            old = version_key(row['maxPluginVersion'])
            if row['state'] != 'selected' or old is None:
                raise ValueError('selector_version_changed')
            if running > old:
                validate_sync_owner(owner, Path(decision['storePath']))
                if db.execute("UPDATE selection SET max_plugin_version=?,epoch=epoch+1 "
                              "WHERE id=1 AND state='selected' AND epoch=?",
                              (plugin_version(), row['epoch'])).rowcount != 1:
                    raise ValueError('selector_version_changed')
                _inspect_v1_row(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
