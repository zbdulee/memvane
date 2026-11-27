"""1.0.x-3: an unchanged raw observation reuses its ``source_fingerprints`` row.

State table (each row is a test; ``+n`` counts ``binding_state='observed'`` rows,
the rows only the sync pass writes in this fixture):

| history                                   | expected                                  |
|-------------------------------------------|-------------------------------------------|
| card edits, no raw change                 | +0 per published pass                     |
| raw A -> B -> A                           | +1, then +0 (A's row is reused)           |
| HEAD moves, no raw change                 | +0, a new generation at the new HEAD      |
| tracked -> untracked -> tracked           | +1, then +0                               |
| ingest's legacy_unbound rows, same bytes  | never cited by a generation's sources     |
| v1.1.4 store write between 1.1.5 passes   | 1.1.4 writes its own rows and verifies    |
|                                           | 1.1.5 heads; 1.1.5 reuses them: +0        |

Passes run the real writers (``sync.api.run``); the v1.1.4 side is the
release tag's own store code, extracted with ``git archive`` and run as a child.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

from memvane.sync.tests.support import SyncCase
from tests.support.case import require_fixture_env
from tests.support.install_builder import ROOT

TAG = 'v1.1.4'
_RELEASED = {}
RELEASED_CHILD = r'''
import json, sys, time, uuid
args = json.loads(sys.argv[1])
sys.path.insert(0, args['root'])
import memvane
assert memvane.__file__.startswith(args['root']), memvane.__file__
from memvane.store import api as views
store = views.open_store(args['db'], repository=args['repository'], scope_id=args['scope'])
handle = store.capture_view(args['worktree'])
if args['action'] == 'verify':
    pin = store.pin_view(handle)
    out = {'hits': sorted(row['path'] for row in store.search(pin, args['query'], 10))}
    store.release(pin)
    store.verify()
else:
    # The store writer a released sync pass runs for an unchanged checkout: its own scan's
    # fingerprints, the head's members, the head's vector spec.
    head = store.head(handle)
    members = [dict(path=r['path'], blob_sha=r['blob_sha'], display_title=r['title'], trust_digest=r['trust_digest'])
               for r in store._rows_open(store._db, handle.view_id)]
    coverage = store._db.execute('SELECT g.coverage,g.recipe_id,g.profile_id FROM s_heads h JOIN s_generations g '
                                 'ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=?',
                                 (handle.view_id,)).fetchone()
    spec = dict(recipe_id=coverage[1], profile_id=coverage[2]) if coverage[0] == 'complete' else None
    scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
    sources = views.put_source_fingerprints(store, handle, scan, {p: tuple(v) for p, v in args['observed'].items()})
    manifest = dict(scan_id=scan['id'], policy_digest=handle.policy_digest, revocation_epoch=0, complete=True,
                    sources=sources, members=members)
    gen = store.prepare_generation(handle, manifest, expected_head=head, vector_spec=spec)
    out = {'generation': store.publish_generation(handle, gen, expected_head=head)['gen_id']}
store.close()
print(json.dumps(out))
'''


def released_root():
    """The extracted ``TAG`` tree (``memvane`` and ``package.json``), once per process."""
    if TAG not in _RELEASED:
        dest = Path(tempfile.mkdtemp(prefix='rel-', dir=str(require_fixture_env()))).resolve()
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', TAG, 'memvane', 'package.json'],
                                 capture_output=True)
        if archive.returncode != 0:
            raise AssertionError('git archive %s failed: %s' % (TAG, archive.stderr.decode('utf8', 'replace')))
        subprocess.run(['tar', '-x', '-C', str(dest)], input=archive.stdout, check=True)
        if 'v' + json.loads((dest / 'package.json').read_text(encoding='utf8'))['version'] != TAG:
            raise AssertionError('extracted tree is not ' + TAG)
        _RELEASED[TAG] = dest
    return _RELEASED[TAG]


class FingerprintReuse(SyncCase):
    def observed(self):
        with self.db() as db:
            return db.execute("SELECT count(*) FROM source_fingerprints WHERE binding_state='observed'").fetchone()[0]

    def rows_for(self, path):
        with self.db() as db:
            return db.execute("SELECT count(*) FROM source_fingerprints WHERE binding_state='observed' AND path=?",
                              (path,)).fetchone()[0]

    def indexed(self):
        result = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        return result['generation']

    def edit_card(self, rel, token):
        self.edits = getattr(self, 'edits', 0) + 1
        self.fixture.write_card(rel, title=token + ' card', body='%s edited body %d\n' % (token, self.edits),
                                sources=[self.project / 'notes' / (token + '.md')])

    def test_card_edits_add_no_observed_rows(self):
        before = self.observed()
        self.assertGreaterEqual(before, len(self.CARDS))
        for round_ in range(2):
            self.edit_card('concepts/alder-grove.md', 'alderprobe')
            self.indexed()
            self.assertEqual(self.observed(), before, round_)

    def test_raw_revert_reuses_the_earlier_row(self):
        note = self.project / 'notes/birchprobe.md'
        original = note.read_bytes()
        path = 'notes/birchprobe.md'
        self.assertEqual(self.rows_for(path), 1)
        note.write_bytes(b'Changed source for birchprobe.\n')
        self.indexed()
        self.assertEqual(self.rows_for(path), 2)
        note.write_bytes(original)
        self.indexed()
        self.assertEqual(self.rows_for(path), 2)

    def test_head_move_alone_adds_no_rows(self):
        before, head = self.observed(), self.head()
        self.fixture.commit('empty', everything=False)
        generation = self.indexed()
        self.assertNotEqual(generation, head[0])
        self.assertEqual(self.observed(), before)
        self.edit_card('concepts/cedar-hill.md', 'cedarprobe')
        self.indexed()
        self.assertEqual(self.observed(), before)

    def test_tracked_state_change_writes_then_reuses(self):
        path = 'notes/wrenprobe.md'
        self.assertEqual(self.rows_for(path), 1)
        self.fixture.git('rm', '-q', '--cached', path)
        self.fixture.commit('untrack', everything=False)
        self.indexed()
        self.assertEqual(self.rows_for(path), 2)
        self.fixture.git('add', path)
        self.fixture.commit('track again', everything=False)
        self.indexed()
        self.assertEqual(self.rows_for(path), 2)

    def test_generation_sources_cite_only_observed_rows(self):
        self.edit_card('concepts/kestrel-perch.md', 'kestrelprobe')
        generation = self.indexed()
        with self.db() as db:
            unbound = db.execute("SELECT count(*) FROM source_fingerprints WHERE binding_state='legacy_unbound' "
                                 "AND path='notes/kestrelprobe.md'").fetchone()[0]
            view_id, ordinal = self.at(db, generation)
            states = {row[0] for row in db.execute(
                'SELECT o.binding_state FROM s_sources s JOIN s_source_observations o '
                'ON o.observation_id=s.observation_id WHERE s.view_id=? AND s.gen_from<=? '
                'AND (s.gen_to IS NULL OR s.gen_to>?)', (view_id, ordinal, ordinal))}
        self.assertGreater(unbound, 0)
        self.assertEqual(states, {'observed'})

    # -- the released 1.1.4 store writer and reader on the same store ------------------
    # A route is bound to the plugin root it was installed from, so the released tree
    # cannot select this install: the child drives the 1.1.4 store API (the shared
    # persistent contract) with the observation and handle of this install's last pass.
    def recorded_pass(self):
        """One indexed pass; the main view's store arguments and raw observation."""
        from memvane.store import api as store_api
        seen, real = {}, store_api.put_source_fingerprints

        def record(store, handle, scan, observed):
            seen.setdefault('main', dict(db=str(store._path), repository=store._repository, scope=store._scope,
                                         worktree=handle.worktree_id, observed=observed))
            return real(store, handle, scan, observed)
        with mock.patch.object(store_api, 'put_source_fingerprints', record):
            self.indexed()
        return seen['main']

    def released(self, action, **args):
        payload = json.dumps(dict(args, action=action, root=str(released_root())))
        child = subprocess.run([sys.executable, '-B', '-I', '-c', RELEASED_CHILD, payload],
                               capture_output=True, text=True, timeout=300)
        self.assertEqual(child.returncode, 0, child.stderr[-4000:])
        return json.loads(child.stdout.strip().splitlines()[-1])

    def test_released_store_verifies_and_writes_between_passes(self):
        self.edit_card('concepts/alder-grove.md', 'alderprobe')
        args = self.recorded_pass()
        observed = args.pop('observed')
        self.assertEqual(self.released('verify', query='alderprobe', **args)['hits'], ['concepts/alder-grove.md'])
        before = self.observed()
        generation = self.released('write', observed=observed, **args)['generation']
        self.assertEqual(self.head()[0], generation)
        written = self.observed() - before
        self.assertEqual(written, len(observed))  # 1.1.4 writes a row per source per scan
        self.edit_card('concepts/cedar-hill.md', 'cedarprobe')
        self.indexed()
        self.assertEqual(self.observed(), before + written)
        self.assertEqual(self.released('verify', query='cedarprobe', **args)['hits'], ['concepts/cedar-hill.md'])
