"""Journal test seeds: a selected 1.0 install, its reader endpoint and sync owner.

Every install goes through ``tests.support.install_builder.build_install``
(the install use cases and the fixture provider). The journal is written here
only to forge a row the way a same-UID process could (``forge_*``); the
product never takes those paths.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT, build_install
from tests.support.project import make_project

NOTE = 'The emerald heron nests beside the north pond.\n'
CARD = 'heron.md'


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)


def thaw(value):
    """A plain copy of an issued (frozen) endpoint record."""
    if hasattr(value, 'items'):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def file_state(path):
    """``(bytes, inode, mtime_ns)`` of a file, or None when it is absent."""
    path = Path(path)
    if not path.exists():
        return None
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mtime_ns


class InstallCase(FixtureCase):
    """A project with a note and a verified card, onboarded up to ``UPTO``.

    A ``selected`` install is settled: its main view has a published lexical
    and vector generation and the owner has re-sealed the reader.
    """

    UPTO = 'selected'

    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', NOTE)
        self.fixture.write_card(CARD, title='Emerald heron', body=NOTE, sources=[note])
        self.fixture.commit('fixture')
        self.project = self.fixture.root
        self.install = build_install(self, self.fixture, upto=self.UPTO)
        self.state = self.install.state
        self.decision = None
        if self.UPTO == 'selected':
            self.install.settle()
            self.decision = self.install.select()

    # -- refusals ------------------------------------------------------------------
    def refused(self, reason, call, *args, **kwargs):
        """``call`` raises a refusal whose text is ``reason``; returns the exception."""
        with self.assertRaises(Exception) as caught:
            call(*args, **kwargs)
        self.assertEqual(str(caught.exception), reason, repr(caught.exception))
        return caught.exception

    # -- journal ---------------------------------------------------------------------
    def row(self):
        from memvane.journal import api as journal
        return journal.inspect_journal(self.install.journal)

    def record(self):
        return dict(self.row()['reader'])

    def forge(self, sql, args=()):
        """One statement on the journal as a same-UID process could run it."""
        with closing(sqlite3.connect(str(self.install.journal), isolation_level=None)) as db:
            db.execute(sql, args)

    def forge_record(self, record):
        self.forge('UPDATE reader SET record=? WHERE id=1', (canon(record),))

    # -- reader ----------------------------------------------------------------------
    @contextmanager
    def endpoint(self, project=None):
        from memvane.journal import api as journal
        with journal.resolve_endpoint(project or self.project, action='recall',
                                      host_root=ROOT) as endpoint:
            yield endpoint

    def endpoint_id(self):
        with self.endpoint() as endpoint:
            return endpoint.endpoint_id

    def head(self, record=None, **kwargs):
        """``(view_id, epoch)`` of the head ``record`` (default: the selected reader) pins."""
        from memvane.journal import api as journal
        with journal.pin_schema3(dict(record or self.record()), deadline=time.monotonic() + 30,
                                 pin_view=False, **kwargs) as (_store, _handle, head):
            return head['view_id'], head['epoch']

    def pin(self, record=None, **kwargs):
        """``(view_id, epoch, vector spec or None)`` of a full pin of ``record``."""
        from memvane.journal import api as journal
        from memvane.store import api as store_api
        with journal.pin_schema3(dict(record or self.record()), deadline=time.monotonic() + 30,
                                 **kwargs) as (store, _handle, pin):
            vector = store_api.pinned_vector(store, pin)
            spec = None if vector is None else (vector['profile_id'], vector['recipe_id'])
            return pin.view_id, pin.epoch, spec

    # -- writer ----------------------------------------------------------------------
    @contextmanager
    def owner(self, store_path=None):
        """The UID-wide sync owner of the install's store."""
        from memvane.runtime import api as runtime
        with runtime.sync_owner(Path(store_path or self.install.store_path), wait=False) as owner:
            yield owner

    @contextmanager
    def writer(self, decision=None):
        from memvane.journal import api as journal
        with self.owner() as owner:
            with journal.selected_v1_writer_scope(decision or self.decision, queue_owner=owner) as writer:
                yield writer

    def lock_path(self):
        from memvane.journal import api as journal
        return journal.journal_directory('reader-locks', self.install.install_id) / 'reader-lifetime.lock'


def private(path, mode=0o600):
    os.chmod(path, mode)
    return path


__all__ = ('CARD', 'InstallCase', 'NOTE', 'ROOT', 'canon', 'file_state', 'private', 'thaw')
