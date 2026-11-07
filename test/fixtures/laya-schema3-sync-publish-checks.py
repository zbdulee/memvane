#!/usr/bin/env python3
"""U12c: a selected schema3 install's sync pass publishes view-store generations.

Seeded from the U12b schema3 writer-plan fixture: the v5 install, v6 index,
journal images, Git checkout and view store are actual; the schema3 record is
the U10 seal, selected with ``mutate_image`` (no loaded-bridge CAS). The pass
runs the actual ``laya_product_sync_worker.run``; recall reads through the
actual persisted schema3 ``query_endpoint``. No model or provider starts.
"""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_sync_seed', ROOT / 'test/fixtures/laya-writer-schema3-plan-checks.py')
seed_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(seed_module)


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True, default=str), flush=True)


class Schema3SyncPublishChecks(seed_module.Schema3WriterPlanChecks):
    def setUp(self):
        super().setUp()
        self.worker = importlib.import_module('laya_product_sync_worker')
        self.views = importlib.import_module('laya_view_store')
        self.lease = importlib.import_module('laya_reader_lease')
        self.dispatch = importlib.import_module('laya_reader_dispatch')
        root = mock.patch.object(self.worker, '_writer_executing_root', return_value=self.case.plugin)
        root.start(); self.addCleanup(root.stop)
        # warm() writes a one-time v6 sentinel; hold it once for the whole check.
        warm = self.F.warm(); warm.__enter__()
        self.addCleanup(warm.__exit__, None, None, None)
        self.wiki = self.case.auto / 'wiki'
        self.decision = self.J.select_route(str(self.case.project), action='update')
        self.raw_collection = next(c['name'] for c in self.decision['collections'] if c['role'] == 'raw')

    # -- helpers --------------------------------------------------------------
    def card(self, relative, token):
        """A card that passes the reader's verified-frontmatter rule (trusted)."""
        import yaml_scalars
        raw = ('Synthetic source for ' + token + '.\n').encode('utf8')
        source = self.case.project / 'notes' / (token + '.md')
        source.parent.mkdir(exist_ok=True); source.write_bytes(raw)
        revision = dict(kind='file', path='notes/' + token + '.md', collection=self.raw_collection,
                        sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        mtimeNs=source.stat().st_mtime_ns)
        line = yaml_scalars.dump_source_revisions([revision])[0]
        front = (f'title: "{token} card"\nstatus: verified\ncreatedBy: memvane\n'
                 f'sourceRevisions:\n  - {line}\n')
        text = '---\n' + front + '---\n' + f'# {token} card\n{token} verified body\n'
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + yaml_scalars.card_body_hash(text) + '\n', 1)
        path = self.wiki / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf8')

    def head(self):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT gen_id,epoch FROM view_heads').fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def members(self, gen_id):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return {path: (blob, eligible) for path, blob, eligible in db.execute(
                'SELECT path,blob_sha,eligible FROM wiki_members WHERE gen_id=?', (gen_id,))}

    def card_sources(self, gen_id, path):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute('SELECT count(*) FROM card_sources WHERE gen_id=? AND wiki_path=?',
                              (gen_id, path)).fetchone()[0]

    def run_pass(self):
        """One sync pass; returns (result, ingest_attested_card call count)."""
        original = self.views.ViewStore.ingest_attested_card
        calls = []
        def counted(store, *args, **kwargs):
            calls.append(args[1] if len(args) > 1 else kwargs.get('path'))
            return original(store, *args, **kwargs)
        def no_service(*_args, **_kwargs):
            raise AssertionError('a schema3 lexical pass started a provider service')
        with self.seed.authority(), \
                mock.patch.object(self.views.ViewStore, 'ingest_attested_card', new=counted):
            result = self.worker.run(self.case.project, start_service=no_service)
        return result, calls

    def recall(self, prompt):
        with self.seed.authority(), self.J.read_only_journals():
            with self.lease.resolve_endpoint(self.case.project, action='recall',
                                             host_root=self.case.plugin) as endpoint:
                self.assertEqual(endpoint.record['store_format'], 'schema3')
                # U11: the persisted schema3 reader binds to the reader's own decision.
                decision = self.R._recheck(endpoint)[2]['readerDecision']
                return self.dispatch.query_endpoint(endpoint, checkout=self.case.project, prompt=prompt,
                                                    query_vector=None, deadline=time.monotonic() + 20,
                                                    decision=decision)

    # -- checks ---------------------------------------------------------------
    def test_01_pass_publishes_edited_card_and_persisted_recall_reads_it(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        files = self.v6_files()
        journal_before = self.J.inspect_journal(self.case.journal)
        first, first_calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        self.assertEqual(first['storeFormat'], 'schema3')
        published = self.head()
        self.assertEqual(published, (first['generation'], start[1] + 1))
        members = self.members(published[0])
        self.assertEqual(sorted(first_calls), sorted(members))
        self.assertEqual(members['concepts/kestrel-perch.md'][1], 1)
        # D3: a trusted card's source spans are the generation's location rows.
        self.assertGreaterEqual(self.card_sources(published[0], 'concepts/kestrel-perch.md'), 1)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], journal_before['epoch'] + 1)
        self.assertEqual(self.v6_files(), files)
        hit = self.recall('kestrelprobe')
        self.assertEqual((hit.store_format, hit.data_generation), ('schema3', published[0]))
        self.assertIn(self.case.WIKI + '/concepts/kestrel-perch.md', [row['path'] for row in hit.rows])

        # An unchanged pass writes nothing, not even the journal fence.
        idle, idle_calls = self.run_pass()
        self.assertEqual((idle['state'], idle_calls, self.head()), ('unchanged', [], published))
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], journal_before['epoch'] + 1)

        # Editing one card ingests exactly that card; recall reads the new bytes.
        self.card('concepts/kestrel-perch.md', 'falconprobe')
        second, second_calls = self.run_pass()
        self.assertEqual(second['state'], 'indexed', second)
        self.assertEqual(second_calls, ['concepts/kestrel-perch.md'])
        self.assertEqual(self.head(), (second['generation'], published[1] + 1))
        after = self.members(second['generation'])
        self.assertEqual({p: m for p, m in after.items() if p != 'concepts/kestrel-perch.md'},
                         {p: m for p, m in members.items() if p != 'concepts/kestrel-perch.md'})
        hit = self.recall('falconprobe')
        self.assertEqual(hit.data_generation, second['generation'])
        self.assertIn(self.case.WIKI + '/concepts/kestrel-perch.md', [row['path'] for row in hit.rows])
        self.assertNotIn(self.case.WIKI + '/concepts/kestrel-perch.md',
                         [row['path'] for row in self.recall('kestrelprobe').rows])
        self.assertEqual(self.v6_files(), files)
        emit('schema3-sync-publish', firstIngests=len(first_calls), editIngests=len(second_calls),
             epochs=[start[1], published[1], self.head()[1]], v6BytesUnchanged=True)

    def test_02_settings_change_defers_without_writes_and_restored_bytes_publish(self):
        self.select_schema3()
        settings = importlib.import_module('project_dir').settings_path(self.case.project)
        original = settings.read_bytes()
        start = self.head()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        # Same policy, different bytes: the store's immutable worktree binding changes.
        settings.write_bytes(original.rstrip(b'\n') + b'\n\n')
        try:
            held, calls = self.run_pass()
        finally:
            settings.write_bytes(original)
        self.assertEqual((held['state'], held['reason'], held['detail']),
                         ('deferred', 'schema3_binding_changed', 'wiki_binding_changed'))
        self.assertEqual((calls, self.head()), ([], start))
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        resumed, calls = self.run_pass()
        self.assertEqual(resumed['state'], 'indexed', resumed)
        self.assertIn('concepts/heron-watch.md', calls)
        self.assertEqual(self.head(), (resumed['generation'], start[1] + 1))
        emit('schema3-sync-binding-held', reason=held['reason'], detail=held['detail'],
             headUnchanged=True, journalUnchanged=True, restoredPublishes=True)

    def test_03_interrupted_publish_keeps_old_head_and_next_pass_resumes(self):
        self.select_schema3()
        self.card('concepts/wren-path.md', 'wrenprobe')
        start = self.head()
        original = self.views.ViewStore.publish_generation
        def before(store, *args, **kwargs):
            raise RuntimeError('injected stop before publish')
        with mock.patch.object(self.views.ViewStore, 'publish_generation', new=before):
            with self.assertRaisesRegex(RuntimeError, 'injected stop before publish'):
                self.run_pass()
        self.assertEqual(self.head(), start)
        resumed, _calls = self.run_pass()
        self.assertEqual(resumed['state'], 'indexed', resumed)
        self.assertEqual(self.head(), (resumed['generation'], start[1] + 1))

        self.card('concepts/wren-path.md', 'robinprobe')
        published = []
        def after(store, handle, gen_id, **kwargs):
            published.append(original(store, handle, gen_id, **kwargs))
            raise RuntimeError('injected stop after publish')
        with mock.patch.object(self.views.ViewStore, 'publish_generation', new=after):
            with self.assertRaisesRegex(RuntimeError, 'injected stop after publish'):
                self.run_pass()
        moved = self.head()
        self.assertEqual(moved[1], start[1] + 2)
        replay, calls = self.run_pass()
        self.assertEqual((replay['state'], calls, self.head()), ('unchanged', [], moved))
        self.assertIn(self.case.WIKI + '/concepts/wren-path.md', [r['path'] for r in self.recall('robinprobe').rows])
        emit('schema3-sync-interrupted-publish', beforeKeepsHead=True, afterIsUnchangedReplay=True)

    def test_05_ingest_cap_publishes_progress_as_pending_then_completes(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        with mock.patch.object(self.worker, 'SCHEMA3_MAX_INGEST', 2):
            first, first_calls = self.run_pass()
            self.assertEqual((first['state'], first['ingested'], first['pending'], len(first_calls)),
                             ('pending', 2, 2, 2), first)
            self.assertEqual(self.head(), (first['generation'], start[1] + 1))
            self.assertEqual(sorted(self.members(first['generation'])), sorted(first_calls))
            second, second_calls = self.run_pass()
        self.assertEqual((second['state'], second['ingested'], second['pending']), ('indexed', 2, 0), second)
        self.assertEqual(set(first_calls) & set(second_calls), set())
        self.assertEqual(self.head(), (second['generation'], start[1] + 2))
        self.assertEqual(sorted(self.members(second['generation'])), sorted(first_calls + second_calls))
        emit('schema3-sync-ingest-cap', firstPass=first_calls, secondPass=second_calls)

    def test_06_removed_card_leaves_generation_and_mass_removal_is_held(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.card('concepts/wren-path.md', 'wrenprobe')
        full, _calls = self.run_pass()
        self.assertEqual(full['state'], 'indexed', full)
        members = sorted(self.members(full['generation']))
        self.assertGreaterEqual(len(members), 5)
        (self.wiki / 'concepts/kestrel-perch.md').unlink()
        one, calls = self.run_pass()
        self.assertEqual((one['state'], one['removed'], calls), ('indexed', 1, []), one)
        self.assertEqual(sorted(self.members(one['generation'])),
                         [p for p in members if p != 'concepts/kestrel-perch.md'])
        held_head = self.head()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        for path in members:
            if path != 'concepts/kestrel-perch.md':
                (self.wiki / path).unlink()
        held, calls = self.run_pass()
        self.assertEqual((held['state'], held['reason'], held['removed'], calls),
                         ('deferred', 'schema3_mass_removal_held', len(members) - 1, []), held)
        self.assertEqual(self.head(), held_head)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        emit('schema3-sync-removal', removedOne=1, heldRemoval=held['removed'], headUnchanged=True)

    def test_04_v6_selection_keeps_the_v6_sync_path(self):
        self.assertEqual(self.worker._selected_store_format(self.decision), 'v6')
        with mock.patch.object(self.worker, '_run_schema3',
                               side_effect=AssertionError('schema3 path on a v6 selection')), \
                mock.patch.object(self.worker, '_capacity_paths',
                                  side_effect=RuntimeError('v6 path reached')):
            with self.seed.authority():
                with self.assertRaisesRegex(RuntimeError, 'v6 path reached'):
                    self.worker.run(self.case.project, start_service=lambda *a, **k: None)
        self.select_schema3()
        self.assertEqual(self.worker._selected_store_format(self.decision), 'schema3')
        emit('schema3-sync-v6-unchanged', v6Branch=True)


# Only this unit's checks run here; the seed's own checks run in their file.
for _name in dir(seed_module.Schema3WriterPlanChecks):
    if _name.startswith('test') and _name not in vars(Schema3SyncPublishChecks):
        setattr(Schema3SyncPublishChecks, _name, None)


if __name__ == '__main__':
    unittest.main()
