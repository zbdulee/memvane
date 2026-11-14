"""The shared fixtures do what tests rely on (run through ``support.test.mjs``)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest

from tests.support.case import FixtureCase, require_fixture_env


class Case(FixtureCase):
    def test_each_test_gets_its_own_home_and_runtime_root_and_gives_them_back(self):
        run_home = Path(os.environ['HOME'])  # each probe nests under this test's HOME
        seen = []

        class Probe(FixtureCase):
            def runTest(inner):
                seen.append((os.environ['HOME'], os.environ['MEMVANE_TEST_RUNTIME_ROOT']))

        before = {key: os.environ.get(key) for key in ('HOME', 'MEMVANE_TEST_RUNTIME_ROOT', 'TMPDIR')}
        for _ in range(2):
            result = unittest.TestResult()
            Probe().run(result)
            self.assertEqual((result.errors, result.failures), ([], []))
        self.assertEqual({key: os.environ.get(key) for key in before}, before)
        self.assertEqual(len({home for home, _ in seen}), 2)
        for home, runtime in seen:
            self.assertEqual(Path(home).parent, run_home)
            self.assertEqual(runtime, str(Path(home) / 'rt'))

    def test_the_account_home_is_refused(self):
        import pwd
        account = pwd.getpwuid(os.getuid()).pw_dir
        saved = os.environ['HOME']
        os.environ['HOME'] = account
        try:
            with self.assertRaises(RuntimeError):
                require_fixture_env()
        finally:
            os.environ['HOME'] = saved

    def test_wait_until_returns_the_value_and_times_out_by_count(self):
        calls = []
        self.assertEqual(self.wait_until(lambda: calls.append(1) or (len(calls) >= 3 and 'ready')), 'ready')
        self.assertEqual(len(calls), 3)
        with self.assertRaises(AssertionError):
            self.wait_until(lambda: False, timeout=0.05)


class Project(FixtureCase):
    def test_minimal_settings_cards_and_git(self):
        from memvane.wiki.card import api as card
        from tests.support.project import make_project
        project = make_project(self.home, name='alpha', skip_paths=['drafts/'])
        self.assertEqual(json.loads(project.settings_path.read_text()), {'name': 'alpha', 'skipPaths': ['drafts/']})
        self.assertNotEqual(project.raw_name, project.wiki_name)
        note = project.write_note('notes/a.md', 'The amber lantern glows.\n')
        path = project.write_card('concepts/a.md', title='Amber', body='The amber lantern glows.\n',
                                  sources=[note])
        text = path.read_text()
        meta, _ = card.parse_frontmatter(text)
        self.assertEqual(meta.get('status'), 'verified')
        self.assertEqual(meta.get('verifiedBodyHash'), card.card_body_hash(text))
        generated = project.write_card('concepts/b.md', title='B', body='x\n', status='generated')
        self.assertNotIn('verifiedBodyHash', generated.read_text())
        project.commit('fixture')
        self.assertEqual(project.git('rev-list', '--count', 'HEAD').strip(), '1')
        index = project.root / '.git' / 'index'
        self.assertLess(index.stat().st_mtime, time.time() - 1)
        linked = project.linked_worktree('side', 'side')
        self.assertTrue((linked.root / 'notes' / 'a.md').is_file())
        self.assertEqual(linked.raw_name, project.raw_name)


class Settings(FixtureCase):
    def test_expanded_merges_and_use_config_answers_only_that_root(self):
        from memvane.settings import api as settings
        from tests.support.project import make_project
        from tests.support.settings_fake import expanded, use_config
        project = make_project(self.home, name='beta')
        other = make_project(self.home, name='gamma', dirname='other')
        config = expanded(project, compile={'verify': {'maxPerRun': 3}})
        self.assertEqual(config['compile']['verify']['maxPerRun'], 3)
        self.assertIn('collections', config)
        use_config(self, project, config)
        self.assertEqual(settings.load_project_config(str(project.root / 'notes'))['compile']['verify']['maxPerRun'], 3)
        self.assertEqual(settings.find_project_config(str(project.root))['config'], config)
        self.assertNotEqual(settings.load_project_config(str(other.root)).get('compile', {}).get('verify', {})
                            .get('maxPerRun'), 3)


class Engines(FixtureCase):
    def test_fake_engines_record_every_call_and_fail_like_the_extractor(self):
        from memvane.wiki.engines import api as engines
        from tests.support.engines_fake import FakeEngines, script
        fake = FakeEngines({'verify': {'verdict': 'pass'}, 'dedup': lambda payload, argv: {'same': True}}).install(self)
        self.assertEqual(engines.run_extractor(['codex'], {'task': 'verify'}, 5, self.home),
                         ({'verdict': 'pass'}, None, 0))
        self.assertEqual(engines.run_extractor(['claude'], {'task': 'dedup'}, 5, self.home), ({'same': True}, None, 0))
        self.assertEqual([(c['task'], c['engine']) for c in fake.calls], [('verify', 'codex'), ('dedup', 'claude')])
        self.assertEqual(FakeEngines(returncode=127).run_extractor(['x'], {}, 1, self.home)[2], 127)
        self.assertEqual(FakeEngines(garbage=True).run_extractor(['x'], {}, 1, self.home)[1], 'invalid_extractor_json')
        self.assertEqual(FakeEngines(timeout=True).run_extractor(['x'], {}, 1, self.home)[1], 'extractor_timeout')
        argv = script(self.tmp, {'verdict': 'pass'})
        out = subprocess.run(argv, input='{}', capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(out.stdout), {'verdict': 'pass'})


class HotJournal(FixtureCase):
    def test_a_killed_writer_leaves_a_hot_journal(self):
        from tests.support.sqlite_hot_journal import kill_mid_write
        path = self.tmp / 'hot.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE t(x)')
        _before, journal = kill_mid_write(path)
        self.assertGreater(journal.stat().st_size, 0)


class Provider(FixtureCase):
    def test_query_admission_follows_the_published_spec(self):
        from memvane.kernel.resource_deferred import ResourceDeferred
        from memvane.resident.api import admit_query
        from tests.support.fake_provider import FixtureProvider
        provider = FixtureProvider().install(self)
        refused = []
        for admitted in (False, True, False):
            provider.admit_queries(admitted)
            try:
                admit_query()
            except ResourceDeferred as exc:
                refused.append(str(exc))
            else:
                refused.append(None)
            self.assertEqual(provider.current().query_admission, 'admit' if admitted else 'refuse')
        self.assertEqual(refused, ['fixture_lexical_recall', None, 'fixture_lexical_recall'])

    def test_the_query_child_answers_the_published_query_embedding(self):
        from memvane.provider.api import is_reviewed_provider, query_embed_child_argv
        from tests.support.fake_provider import FixtureProvider
        provider = FixtureProvider(query_embedding='provider_unavailable').install(self)
        manifest = {'approvedProviderId': 'synthetic-provider', 'dimension': 2}
        self.assertTrue(is_reviewed_provider(provider.reviewed_provider(manifest)))
        answers = []
        for embedding in ('provider_unavailable', 'vector'):
            provider.answer_queries(embedding)
            result = subprocess.run(query_embed_child_argv(), text=True, capture_output=True, timeout=60,
                                    input=json.dumps({'manifest': manifest, 'prompt': 'q', 'timeout': 1,
                                                      'allowed': ['provider_unavailable']}))
            answers.append((result.returncode, json.loads(result.stdout)))
        self.assertEqual(answers, [(0, {'error': 'provider_unavailable'}), (0, {'vector': [1.0, 0.0]})])


class Builders(FixtureCase):
    def project(self, name='delta'):
        from tests.support.project import make_project
        project = make_project(self.home, name=name, dirname=name)
        note = project.write_note('notes.md', 'Field notes: the copper falcon nests on the tower.\n')
        project.write_card('concepts/falcon.md', title='Falcon protocol', sources=[note],
                           body='The falcon protocol keeps marker token zqfalconx for recall checks.\n')
        project.commit('fixture')
        return project

    def test_a_selected_install_recalls_in_process_and_through_the_hook_process(self):
        from tests.support.install_builder import build_install
        target = build_install(self, self.project())
        self.assertEqual(target.decision['state'], 'candidate')
        self.assertEqual(target.epoch(), target.epoch())
        self.assertEqual(target.settle().get('state'), 'unchanged')
        self.assertIn('zqfalconx', target.recall('what marker does the falcon protocol keep?'))
        # The hook child finds the fixture provider at the test runtime root (the subprocess seam).
        code, out, err = target.hook('recall', {
            'session_id': 's', 'cwd': str(target.project.root), 'hook_event_name': 'UserPromptSubmit',
            'prompt': 'what marker does the falcon protocol keep?'})
        self.assertEqual(code, 0, err)
        self.assertIn('zqfalconx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        with target.db() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0], 4)

    def test_build_install_stops_at_each_step(self):
        from tests.support.install_builder import build_install
        target = build_install(self, self.project(), upto='prepared')
        self.assertEqual(target.decision, None)
        self.assertTrue(target.journal.exists())
        self.assertEqual(target.build()[-1].get('buildPhase'), 'ready')
        self.assertEqual(target.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(target.activate()['status'], 'selected')

    def test_out_of_project_installs_share_one_home(self):
        from tests.support.install_builder import build_out_of_project
        installs = build_out_of_project(self, 2)
        self.assertEqual([i.install_id for i in installs], ['4' * 32, '5' * 32])
        self.assertEqual({i.decision['state'] for i in installs}, {'candidate'})
        self.assertEqual(len({i.decision['storePath'] for i in installs}), 2)

    def test_store_builder_publishes_and_ingests_through_sync(self):
        from tests.support import store_builder
        project = self.project('epsilon')
        empty_store, _handle, empty_head = store_builder.make_store(self, self.project('zeta'), publish=False)
        self.assertIsNone(empty_head['gen_id'])
        store, handle, head = store_builder.make_store(self, project, cards={'concepts/owl.md': ('Owl', 'Owls.\n')})
        self.assertIsNotNone(head['gen_id'])
        store_builder.ingest(store, handle, 'concepts/wren.md', project.card_text(title='Wren', body='Wrens.\n'))
        _fresh, after = store_builder.publish(store, handle)
        self.assertNotEqual(after['gen_id'], head['gen_id'])


if __name__ == '__main__':
    unittest.main()
