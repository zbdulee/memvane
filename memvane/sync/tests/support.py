"""Sync test seeds: a selected 1.0 install and its format 4 store rows.

``SyncCase`` onboards one project through ``tests.support.install_builder``
(the install use cases and the fixture provider: one fixed vector per chunk,
lexical recall) with ``BASE_CARDS`` verified cards, each citing its own raw
note, and settles the sync passes. Passes run ``sync.api.run`` through
``tests.support.sync_harness``; every check reads store rows, journal epochs
and pass results, never a wall clock. Row reads are read-only SQL on the
install's store (``Install.db``).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from unittest import mock

from memvane.sync import product_sync_worker as worker
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

BASE_CARDS = (('concepts/alder-grove.md', 'alderprobe'), ('concepts/birch-row.md', 'birchprobe'),
              ('concepts/cedar-hill.md', 'cedarprobe'), ('concepts/kestrel-perch.md', 'kestrelprobe'),
              ('concepts/wren-path.md', 'wrenprobe'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Coordinator:
    """A runtime coordinator recording provider lanes; ``refuse`` defers every lane."""

    root = None

    def __init__(self, refuse=None, on_activity=None):
        self.refuse, self.on_activity, self.lanes, self.active = refuse, on_activity, [], False

    @contextmanager
    def activity(self, lane, policy, report):
        from memvane.kernel.resource_deferred import ResourceDeferred
        self.lanes.append(lane)
        if self.refuse:
            raise ResourceDeferred(self.refuse, {'ready': False, 'reason': self.refuse})
        self.active = True
        try:
            if self.on_activity:
                self.on_activity(lane)
            yield 1
        finally:
            self.active = False

    def check(self, policy, report):
        return {'ready': True}


class SyncCase(FixtureCase):
    """A selected, settled install with ``BASE_CARDS``."""

    NAME = 'syncfix'
    CARDS = BASE_CARDS
    PROVIDER = None  # FixtureProvider keyword arguments, e.g. {'query_admission': 'admit'}

    def setUp(self):
        super().setUp()
        if self.PROVIDER is not None:
            from tests.support.fake_provider import FixtureProvider
            self.fixture_provider = FixtureProvider(2, **self.PROVIDER).install(self)
        self.fixture = make_project(self.home, name=self.NAME)
        self.project = self.fixture.root
        self.wiki = self.fixture.wiki
        for rel, token in self.CARDS:
            self.card(rel, token)
        self.fixture.commit('fixture')
        self.target = build_install(self, self.fixture)
        self.target.settle()

    # -- files -------------------------------------------------------------------
    def card(self, rel, token, source_dir='notes', project=None):
        """A verified card citing ``<source_dir>/<token>.md`` (written with it); its path."""
        project = project or self.fixture
        note = project.write_note(source_dir + '/' + token + '.md', 'Synthetic source for ' + token + '.\n')
        return project.write_card(rel, title=token + ' card', body=token + ' verified body\n', sources=[note])

    # -- passes ------------------------------------------------------------------
    def run_pass(self, coordinator=None):
        """One ``sync.api.run`` pass (the harness coordinator unless one is given)."""
        if coordinator is None:
            return self.target.run_sync()
        from memvane.sync import api as sync
        return sync.run(self.project, coordinator=coordinator, report={},
                        start_service=self.target.provider.start_service)

    @contextmanager
    def provider_calls(self):
        """Record every embedding call of the reviewed provider handle: ``[(kind, path or title)]``."""
        calls = []
        real = worker.ReviewedLocalQmdProvider

        class Counted:
            def __init__(self, manifest):
                self._handle = real(manifest)

            def embed_document_chunks(self, body, path, **kwargs):
                calls.append(('chunks', path))
                return self._handle.embed_document_chunks(body, path, **kwargs)

            def embed_document(self, body, title=None):
                calls.append(('document', title))
                return self._handle.embed_document(body, title)

            def __getattr__(self, name):
                return getattr(self._handle, name)
        with mock.patch.object(worker, 'ReviewedLocalQmdProvider', Counted):
            yield calls

    @contextmanager
    def store_methods(self, **replacements):
        """Each pass's opened store with methods replaced: ``name=lambda real, *a, **k: ...``."""
        from memvane.store import api as store_api
        real_open = store_api.open_store

        class Wrapped:
            def __init__(self, store):
                self._store = store

            def __getattr__(self, name):
                value = getattr(self._store, name)
                if name in replacements:
                    return lambda *args, **kwargs: replacements[name](value, *args, **kwargs)
                return value
        with mock.patch('memvane.store.api.open_store',
                        lambda *args, **kwargs: Wrapped(real_open(*args, **kwargs))):
            yield

    def epoch(self):
        return self.target.epoch()

    def recall(self, prompt):
        """The selected recall's injected context ('' when none)."""
        return self.target.recall(prompt)

    # -- store rows --------------------------------------------------------------
    def db(self):
        return self.target.db()

    @staticmethod
    def at(db, gen_id):
        row = db.execute('SELECT view_id,ordinal FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
        assert row is not None, gen_id
        return row[0], row[1]

    def view_heads(self):
        """``[(view_id, gen_id, epoch)]`` of every view head."""
        with self.db() as db:
            return sorted(tuple(row) for row in db.execute(
                'SELECT h.view_id,g.gen_id,h.epoch FROM s_heads h JOIN s_generations g '
                'ON g.view_id=h.view_id AND g.ordinal=h.ordinal'))

    def head(self, view_id=None):
        """``(gen_id, epoch)`` of the only view head, or of ``view_id``'s."""
        heads = [(gen, epoch) for view, gen, epoch in self.view_heads() if view_id in (None, view)]
        self.assertEqual(len(heads), 1, heads)
        return heads[0]

    def view_of(self, gen_id):
        with self.db() as db:
            return self.at(db, gen_id)[0]

    def members(self, gen_id):
        """``{path: (blob_sha, eligible)}`` of one generation."""
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            return {path: (blob, eligible) for path, blob, eligible in db.execute(
                'SELECT path,blob_sha,eligible FROM s_cards WHERE view_id=? AND gen_from<=? '
                'AND (gen_to IS NULL OR gen_to>?)', (view_id, ordinal, ordinal))}

    def member_trust(self, gen_id, card):
        """``(trust_digest, eligible)`` of one member, or None."""
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            row = db.execute('SELECT trust_digest,eligible FROM s_cards WHERE view_id=? AND path=? '
                             'AND gen_from<=? AND (gen_to IS NULL OR gen_to>?)',
                             (view_id, card, ordinal, ordinal)).fetchone()
            return None if row is None else tuple(row)

    def card_sources(self, gen_id, card):
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            return db.execute('SELECT count(*) FROM s_card_sources WHERE view_id=? AND card_path=? '
                              'AND gen_from<=? AND (gen_to IS NULL OR gen_to>?)',
                              (view_id, card, ordinal, ordinal)).fetchone()[0]

    def generation_sources(self, gen_id):
        """``{path: observed path}`` of one generation's source rows."""
        with self.db() as db:
            view_id, ordinal = self.at(db, gen_id)
            return {path: observed for path, observed in db.execute(
                'SELECT s.path,o.path FROM s_sources s JOIN s_source_observations o '
                'ON o.observation_id=s.observation_id WHERE s.view_id=? AND s.gen_from<=? '
                'AND (s.gen_to IS NULL OR s.gen_to>?)', (view_id, ordinal, ordinal))}

    def attested_revisions(self, gen_id, card):
        """``[(path, source_sha, byte_length)]`` of the attestation one member's trust names."""
        trust = (self.member_trust(gen_id, card) or (None,))[0]
        self.assertIsNotNone(trust, card)
        with self.db() as db:
            return sorted(tuple(row) for row in db.execute(
                'SELECT f.path,f.source_sha,f.byte_length FROM attestation_sources a '
                'JOIN source_fingerprints f ON f.fingerprint_id=a.fingerprint_id WHERE a.trust_digest=?',
                (trust,)))

    def coverage(self, gen_id):
        """``(spec or None, coverage)`` one generation seals."""
        with self.db() as db:
            recipe, profile, coverage = db.execute(
                'SELECT recipe_id,profile_id,coverage FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
        return (None if coverage != 'complete' else {'recipe_id': recipe, 'profile_id': profile}), coverage

    def embedding_sets(self):
        with self.db() as db:
            return db.execute('SELECT count(*) FROM embedding_sets').fetchone()[0]

    def changed_paths(self, before, after):
        """Member paths whose blob differs between two ``members`` maps (added ones included)."""
        return sorted(path for path, value in after.items()
                      if path not in before or before[path][0] != value[0])

    def compile_files(self):
        """``{relative path: bytes}`` under the project's compile workspace."""
        root = self.project / '.memvane' / 'compile'
        if not root.is_dir():
            return {}
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}

    def events(self, name=None):
        return [event for event in self.target.events() if name in (None, event.get('event'))]


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))
