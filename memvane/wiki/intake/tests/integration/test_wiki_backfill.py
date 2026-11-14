"""Frozen initial backfill admission and terminal receipts."""
import hashlib
import json
from unittest.mock import patch

from memvane.wiki.intake import wiki_backfill as backfill
from memvane.wiki.intake import wiki_discovery as discovery
from memvane.wiki.intake import wiki_compile_enqueue as enqueue
from memvane.wiki.workspace import api as selected
from memvane.runtime import api as runtime
from tests.support.case import FixtureCase
from tests.support.project import make_project


class BackfillChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home, git=False)
        self.project = self.fixture.root
        self.wiki = self.fixture.wiki
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.journal = self.state / 'install-journal.sqlite'
        self.journal.touch()
        self.decision = {'scopeId': 'a' * 32, 'storeIncarnation': 'b' * 32,
            'storePath': str(self.state / 'store.sqlite'), 'projectRoot': str(self.project),
            'journalPath': str(self.journal), 'installId': 'c' * 32,
            'recallSettings': {'skipPaths': ['legacy']},
            'collections': [{'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.project)},
                            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
            'collectionRoots': {'docs': str(self.project), 'wiki': str(self.wiki)}}
        self.config = {'indexing': True, 'collections': ['docs', 'wiki'],
            'collectionPaths': {'docs': '.', 'wiki': '.memvane/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'wikiPath': '.memvane/wiki', 'skipPaths': ['legacy'],
            'compile': {'mode': 'auto-wiki', 'triggers': ['post_sync_source', 'session_start_source'],
                        'extractor': {'builtins': ['claude', 'codex', 'hermes']}}}
        self.bound = self.decision, {'productRouteAllowed': True}
        for item in (patch.object(selected, 'selected', return_value=self.bound),
                     patch.object(selected, 'recheck'),
                     patch.object(selected, 'request_compile'),
                     patch.object(selected, 'request_sync', return_value=True),
                     patch.object(runtime, 'request', return_value={'status': 'queued'})):
            item.start()
            self.addCleanup(item.stop)
        self.snapshot = {}

    def add(self, rel, text='A durable fact.'):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.snapshot['docs/' + rel] = sha
        return sha

    def start(self, live=('sync',)):
        return backfill.discover(self.decision, self.snapshot, self.config, live,
                                 {kind: 'codex' for kind in live})

    def dispatch(self, row):
        return backfill.dispatch(self.decision, row, self.snapshot, self.config, recheck=lambda: None)

    def jobs(self):
        path = self.project / '.memvane/compile/source-queue.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def settle(self, job):
        job = {**job, '_processedContentRevision': job['contentRevision']}
        backfill.settle_job(self.bound, job)

    def test_requires_selected_install_and_accepted_enabled_event(self):
        self.add('one.md')
        self.assertIsNone(self.start(()))
        self.config['compile']['triggers'] = ['manual']
        self.assertIsNone(self.start())
        self.config['compile']['triggers'] = ['post_sync_source']
        self.journal.unlink()
        self.assertIsNone(self.start())
        self.assertFalse(backfill._path(self.decision).exists())

    def test_initial_admission_excludes_controls_hidden_and_skip_paths(self):
        for rel in ('one.md', 'AGENTS.md', 'CLAUDE.md', '.nova/rule.md',
                    'legacy/old.md', 'node_modules/readme.md', 'notes.txt'):
            self.add(rel)
        row = self.start()
        self.assertEqual(list(row['items']), ['docs/one.md'])
        self.assertEqual(backfill._path(self.decision).stat().st_mode & 0o777, 0o600)

    def test_more_than_300_sources_progress_without_overflow_or_early_completion(self):
        for i in range(315):
            self.add('docs/%03d.md' % i)
        row = self.start()
        with patch.object(enqueue, 'compile_max_files', return_value=7):
            first = self.dispatch(row)
        self.assertEqual((first['pending'], first['dispatched'], first['settled']), (308, 7, 0))
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(self.dispatch(backfill._load(self.decision)), first)
        jobs = self.jobs()
        for job in jobs:
            self.settle(job)
        second = backfill._load(self.decision)
        self.assertEqual(backfill.summary(second)['settled'], 7)
        with patch.object(enqueue, 'compile_max_files', return_value=7):
            self.assertEqual(self.dispatch(second)['dispatched'], 7)
        self.assertEqual(len(self.jobs()), 14)

    def test_enqueue_then_ack_write_failure_replays_without_duplicate_source_jobs(self):
        self.add('one.md')
        row = self.start()
        real = discovery._write
        def fail_ack(path, data):
            if path == backfill._path(self.decision):
                raise OSError('write failed')
            return real(path, data)
        with patch.object(discovery, '_write', side_effect=fail_ack):
            with self.assertRaises(OSError):
                self.dispatch(row)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['pending'], 1)
        self.dispatch(backfill._load(self.decision))
        self.assertEqual(len(self.jobs()), 1)
        self.settle(self.jobs()[0])
        self.assertEqual(backfill.summary(backfill._load(self.decision))['state'], 'complete')

    def test_frozen_changed_or_deleted_revision_is_skipped_not_reauthorized(self):
        self.add('one.md')
        self.add('two.md')
        self.start()
        self.add('one.md', 'Changed bytes require an ordinary change event.')
        self.snapshot.pop('docs/two.md')
        self.add('new.md')
        row = self.start(())
        self.assertEqual(backfill.summary(row)['skipped'], 2)
        self.assertNotIn('docs/new.md', row['items'])
        self.assertEqual(self.dispatch(row)['state'], 'complete_with_skips')
        self.assertEqual(self.jobs(), [])

    def test_ordinary_job_dedup_can_settle_same_dispatched_revision(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        job.pop('initialBackfill')
        self.settle(job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)

    def test_wrong_binding_or_receipt_cannot_settle_work(self):
        self.add('one.md')
        self.dispatch(self.start())
        job = self.jobs()[0]
        with self.assertRaisesRegex(discovery.DiscoveryBlocked, 'identity_changed'):
            self.settle({**job, 'initialBackfill': 'f' * 32})
        with self.assertRaisesRegex(discovery.DiscoveryBlocked, 'revision_changed'):
            self.settle({**job, 'contentRevision': 'f' * 64})
        other = {**self.decision, 'storeIncarnation': 'd' * 32}
        # An incarnation gets its own state filename and cannot acknowledge this one.
        backfill.settle_job((other, self.bound[1]), job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['dispatched'], 1)

    def test_originating_host_is_durable_and_ambient_engine_cannot_replace_it(self):
        self.add('one.md')
        row = self.start()
        self.assertEqual(row['engine'], 'codex')
        with patch.dict('os.environ', {'QMD_ENGINE': 'claude'}):
            self.dispatch(row)
        self.assertEqual(self.jobs()[0]['engine'], 'codex')
        resumed = backfill.discover(self.decision, self.snapshot, self.config,
                                     ('sync',), {'sync': 'claude'})
        self.assertEqual(resumed['engine'], 'codex')
        self.assertIsNone(backfill.discover({**self.decision, 'storeIncarnation': 'e' * 32},
            self.snapshot, self.config, ('sync',), {}))
        with patch.dict('os.environ', {'QMD_ENGINE': ''}):
            self.assertEqual(enqueue.compile_engine(self.config['compile']), 'unknown')

    def test_hook_grant_host_survives_provisional_activation_and_is_pruned(self):
        grant = discovery.record_grant(self.decision, 'sync', engine='codex')
        first = discovery.observe_grants(self.decision)
        self.assertEqual(first['engines'], {grant: 'codex'})
        self.assertTrue(discovery.activate_grant(self.decision, 'sync', grant))
        self.assertEqual(discovery.observe_grants(self.decision)['engines'], {grant: 'codex'})
        discovery.revoke_grant(self.decision, grant)
        self.assertNotIn('engines', discovery.observe_grants(self.decision))

    def test_settled_receipt_survives_continuation_wake_failure(self):
        self.add('one.md')
        self.add('two.md')
        with patch.object(enqueue, 'compile_max_files', return_value=1):
            self.dispatch(self.start())
        job = self.jobs()[0]
        with patch.object(selected, 'request_sync', side_effect=OSError('wake failed')):
            with self.assertRaises(OSError):
                self.settle(job)
        self.assertTrue(backfill.settled_job(self.bound, job))
        backfill.settle_job(self.bound, job)
        self.assertEqual(backfill.summary(backfill._load(self.decision))['settled'], 1)
