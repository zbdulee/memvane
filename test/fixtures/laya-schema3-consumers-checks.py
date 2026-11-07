#!/usr/bin/env python3
"""U14: v6 direct consumers on a schema3-selected (switched) install.

Seeded from the U12c schema3 sync fixture: the v5 install, v6 index, journal
images, Git checkout and view store are actual and the schema3 record is the
U10 seal selected with ``mutate_image``. The sync pass, discovery cursor,
external-edit inbox, compile retrieval, dedup scan and source-revision seam
are the actual modules. Only wake requests are recorded instead of queued.
"""
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_consumers_seed',
                                              ROOT / 'test/fixtures/laya-schema3-sync-publish-checks.py')
seed_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(seed_module)


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True, default=str), flush=True)


class Schema3ConsumerChecks(seed_module.Schema3SyncPublishChecks):
    def setUp(self):
        super().setUp()
        self.D = importlib.import_module('laya_wiki_discovery')
        self.X = importlib.import_module('laya_external_edits')
        self.wakes = []
        work = importlib.import_module('runtime_work')

        def request(kind, target='', **_kwargs):
            self.wakes.append((kind, target))
            return {'status': 'queued'}
        patch = mock.patch.object(work, 'request', side_effect=request)
        patch.start(); self.addCleanup(patch.stop)

    def raw_file(self, name, text):
        path = self.case.project / 'notes' / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf8')
        return path

    def selected(self):
        selected = importlib.import_module('wiki_selected_laya')
        with self.seed.authority():
            return selected.selected(self.case.project)

    # -- checks ---------------------------------------------------------------
    def test_01_schema3_pass_drains_external_edit_hints(self):
        self.select_schema3()
        self.card('concepts/heron-perch.md', 'heronprobe')
        source = self.raw_file('outside-edit.md', 'An edit an agent made outside the project.\n')
        blob, _revision = self.X._revision(source)
        inbox = self.X._inbox_path(self.decision)
        row = {'path': 'notes/outside-edit.md', 'blob': blob, 'host': 'codex'}
        self.D._write(inbox, {**self.X._binding(self.decision), 'edits': [row], 'seen': [],
                              'tokens': {row['path']: '1' * 32}})
        files = self.v6_files()
        result, _calls = self.run_pass()
        self.assertEqual((result['state'], result['storeFormat']), ('indexed', 'schema3'), result)
        box = self.X._read_box(inbox, self.decision)
        self.assertEqual((box['edits'], box['tokens']), ([], {}))
        self.assertEqual(self.v6_files(), files)
        emit('schema3-external-edits-drained', processed=1, remaining=len(box['edits']),
             compileWakes=sum(1 for kind, _ in self.wakes if kind == 'laya-compile'))

    def test_02_schema3_pass_runs_discovery_on_the_view_head(self):
        state_path, _grants, _lock = self.D._paths(self.decision)
        # A cursor seeded before the switch names a v6 generation.
        self.D._write(state_path, self.D._state_doc(self.decision, 'v6-pre-switch-head', {}, {}))
        self.select_schema3()
        self.raw_file('discovered.md', 'A raw note discovery must see.\n')
        grant = self.D.record_grant(self.decision, 'sync', engine='codex')
        self.assertTrue(self.D.activate_grant(self.decision, 'sync', grant))
        files = self.v6_files()
        result, _calls = self.run_pass()
        self.assertIn(result['state'], ('indexed', 'unchanged', 'pending'), result)
        self.assertNotEqual(result.get('wikiDiscovery', {}).get('state'), 'blocked', result)
        state = self.D._read(state_path)
        self.assertEqual(state['baseGen'], self.head()[0])
        self.assertIn(self.raw_collection + '/notes/discovered.md', state['rawSnapshot'])
        self.assertEqual(self.v6_files(), files)
        emit('schema3-discovery-view-head', baseGen='view_head', rawSeen=True)

    def test_03_compile_retrieval_reads_the_published_view(self):
        self.select_schema3()
        self.card('concepts/osprey-perch.md', 'ospreyprobe')
        first, _calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        retrieval = importlib.import_module('wiki_compile_retrieval')
        selected = importlib.import_module('wiki_selected_laya')
        dispatch = importlib.import_module('laya_reader_dispatch')
        captured, queries = [], []
        original_search = retrieval.selected_search
        original_query = dispatch.query_endpoint

        @contextmanager
        def spy(bound, text):
            with original_search(bound, text) as found:
                captured.append(found)
                yield found

        def counted(endpoint, **kwargs):
            queries.append(endpoint.record['store_format'])
            return original_query(endpoint, **kwargs)
        files = self.v6_files()
        with self.seed.authority(), mock.patch.object(retrieval, 'selected_search', new=spy), \
                mock.patch.object(dispatch, 'query_endpoint', new=counted):
            rows = selected.wiki_matches(self.case.project, self.wiki,
                                         'ospreyprobe verified body', 5, 2000)
        self.assertEqual(queries, ['schema3'])
        published, exclusions, _name, mode = captured[0]
        self.assertEqual((exclusions, mode), ((frozenset(), frozenset()), 'bound'))
        sha = self.members(first['generation'])['concepts/osprey-perch.md'][0]
        self.assertIn(('concepts/osprey-perch.md', sha, None), published)
        self.assertIn(self.wiki / 'concepts/osprey-perch.md', [path for path, _score, _body in rows])
        self.assertEqual(self.v6_files(), files)
        emit('schema3-compile-retrieval', indexedCandidates=len(published), endpoint='schema3')

    def test_04_dedup_scan_skips_with_fixed_reason_and_reads_no_v6(self):
        self.select_schema3()
        self.card('concepts/egret-perch.md', 'egretprobe')
        self.run_pass()
        scan = importlib.import_module('wiki_dedup_scan')
        learning = importlib.import_module('context_learning.laya_learning')
        config = importlib.import_module('config')
        with self.seed.authority(), \
                mock.patch.object(config, 'compile_active', return_value=True), \
                mock.patch.object(learning, '_open', side_effect=AssertionError('v6 database read')):
            result = scan.run(str(self.case.project))
        self.assertEqual((result['state'], result.get('reason'), result['queued']),
                         ('skipped', 'selected_dedup_schema3_vectors_unavailable', 0), result)
        emit('schema3-dedup-scan', state=result['state'], reason=result['reason'])

    def test_05_source_revision_backfill_is_off_on_schema3_and_on_for_v6(self):
        import yaml_scalars
        retire = importlib.import_module('wiki_source_retire')
        source = self.raw_file('backfill.md', 'Backfill source bytes.\n')
        raw = source.read_bytes()
        revisions = [dict(kind='file', path='notes/backfill.md', collection=self.raw_collection,
                          sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                          mtimeNs=source.stat().st_mtime_ns)]
        entry = yaml_scalars.dump_flow_mapping(dict(kind='file', path='notes/backfill.md',
                                                    collection=self.raw_collection))
        text = ('---\ntitle: "backfill card"\nstatus: generated\ncreatedBy: memvane\n'
                'sources:\n  - ' + entry + '\n---\n# backfill card\n'
                '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\nbody\n<!-- qmd:auto:end -->\n')

        def write(name):
            path = self.wiki / 'concepts' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf8')
            return path
        v6_card = write('backfill-v6.md')
        bound = self.selected()
        with self.seed.authority():
            v6 = retire.record_source_revisions(self.case.project, self.wiki, bound, v6_card, text, revisions)
        self.assertEqual(v6, 'recorded')
        self.assertIn('sourceRevisions:', v6_card.read_text(encoding='utf8'))
        self.select_schema3()
        card = write('backfill-schema3.md')
        bound = self.selected()
        with self.seed.authority():
            outcome = retire.record_source_revisions(self.case.project, self.wiki, bound, card, text, revisions)
        self.assertEqual(outcome, 'schema3_selected')
        self.assertEqual(card.read_text(encoding='utf8'), text)
        emit('schema3-backfill-off', v6=v6, schema3=outcome)

    def test_06_learning_corpus_reads_the_view_head_members(self):
        learning = importlib.import_module('context_learning.laya_learning')
        config = importlib.import_module('config')
        self.card('concepts/plover-perch.md', 'ploverprobe')
        self.select_schema3()
        first, _calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        settings = config.load_project_config(str(self.case.project))
        with self.seed.authority(), \
                mock.patch.object(learning, '_open', side_effect=AssertionError('v6 database read')):
            snapshot = learning.corpus_snapshot(self.case.project, settings, decision=self.decision)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['generation'], first['generation'])
        wiki = learning.wiki_names(self.decision)[0]
        expected = [[wiki, path, blob] for path, (blob, eligible)
                    in sorted(self.members(first['generation']).items()) if eligible]
        self.assertEqual(snapshot['material']['activeDocuments'], expected)
        self.assertIn('concepts/plover-perch.md', [row[1] for row in expected])
        emit('schema3-learning-corpus', documents=snapshot['activeDocuments'])


# Only this unit's checks run here; the seed's own checks run in their files.
for _name in dir(seed_module.Schema3SyncPublishChecks):
    if _name.startswith('test') and _name not in vars(Schema3ConsumerChecks):
        setattr(Schema3ConsumerChecks, _name, None)


if __name__ == '__main__':
    unittest.main()
