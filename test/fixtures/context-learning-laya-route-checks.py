#!/usr/bin/env python3
"""Context learning on a selected Laya route: capture, corpus, gate, acks.

Every check uses a real LayaIndexA database with a sealed, vector-sealed
published head and a real-shaped v2 route decision. Only the installer
journal proof is replaced; QMD is patched to fail if anything reads it.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, nullcontext, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

import config
import laya_host_response as response
import laya_index_host_route
import laya_install_journal
from laya_index_a import LayaIndexA, _digest as manifest_digest
from laya_index_vector import LayaVectorIndex, profile_identity, qmd_markdown_title
from context_learning import corpus, shared
try:  # Absent before this change; every check then fails on its own behavior.
    from context_learning import laya_learning
except ImportError:
    laya_learning = None
from context_learning.contracts import digest
from context_learning.store import canonical

SCOPE = '6' * 32
INSTALL = 'a' * 32
MODEL = 'fixture-only'
FINGERPRINT = 'f' * 64
CLAIM = 'e' * 64


def body(index, extra=''):
    return ('---\ntitle: Synthetic card %02d\n---\nSynthetic fact %02d about the bridge.%s\n'
            % (index, index, extra))


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class LayaProject:
    """A temporary project, a real Laya index and a matching route decision."""

    def __init__(self, base: Path, *, wiki='wiki', cards=4):
        self.base = base
        self.wiki_name = wiki
        self.project = base / 'project'
        self.wiki = self.project / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        self.state = base / 'laya-state'
        self.state.mkdir(mode=0o700)
        self.private = base / 'private'
        self.private.mkdir(mode=0o700)
        self.shared = base / 'shared'
        self.shared.mkdir(mode=0o700)
        self.index_path = self.state / 'index.sqlite'
        self.index = LayaIndexA(self.index_path, create=True)
        self.vectors = LayaVectorIndex(self.index)
        self.profile_id = profile_identity(MODEL, FINGERPRINT, 2)
        self.vectors.register_profile(MODEL, FINGERPRINT, 2)
        self.head = None
        self.epoch = 0
        self.bodies = {}
        self.publish({'%s/card%02d.md' % (wiki, i): body(i) for i in range(cards)})
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.project),
            'pluginRoot': str(CORE.parent), 'dbPath': str(self.index_path),
            'queuePath': str(self.state / 'queue'), 'journalPath': str(self.state / 'journal'),
            'installId': INSTALL, 'scopeId': SCOPE,
            'dbIncarnation': self.index.incarnation(),
            'vectorProfile': {'profileId': self.profile_id, 'modelId': MODEL,
                              'embeddingFingerprint': FINGERPRINT, 'dimension': 2,
                              'metric': 'cosine', 'approvedProviderId': 'fixture',
                              'providerArtifactSha256': 'e' * 64,
                              'providerRelativePath': 'core/fixture.py'},
            'collections': [
                {'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.project)},
                {'name': wiki, 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
            'recallSettings': {'topN': 3, 'lexicalPatterns': [], 'minScore': 0,
                               'skipPaths': [], 'recallStrategy': 'wikiOnly'}}
        self.settings = config.normalize_config({
            'indexing': True, 'collections': ['raw', wiki],
            'collectionPaths': {'raw': '.', wiki: '.auto-context/wiki'},
            'collectionRoles': {'raw': 'raw', wiki: 'wiki'},
            'recallStrategy': 'wikiOnly', 'topN': 3,
            'contextLearning': {'capture': True, 'stateRoot': str(self.private)},
            'sharedLearning': {'enabled': True, 'root': str(self.shared)}})
        consents = self.shared / 'consents'
        consents.mkdir(mode=0o700)
        consent = consents / (digest(str(self.project)) + '.json')
        consent.write_text(canonical({
            'schema': 'qmd-shared-consent-v1', 'projectRoot': str(self.project),
            'sharedRoot': str(self.shared), 'capture': True, 'localTraining': True,
            'externalTeacher': False}))
        consent.chmod(0o600)
        artifact = base / 'base.artifact'
        artifact.write_text('base')
        artifact.chmod(0o600)
        policy = self.shared / 'shared-policy.json'
        policy.write_text(canonical({
            'schema': 'qmd-shared-learning-v1', 'enabled': True,
            'goldModel': 'gpt-6-luna', 'maxGoldCalls': 10, 'batchMin': 5,
            'batchMax': 20, 'targetExact': .95,
            'trainerArgv': [sys.executable, str(CORE / 'context_learning/laya_adapter.py'),
                            str(base)],
            'baseArtifact': str(artifact), 'maxRssMib': 4096, 'minAvailableMib': 128,
            'maxLoadPerCpu': 4, 'safetySeconds': 900, 'experimental': True}))
        policy.chmod(0o600)

    def publish(self, documents):
        """Stage, vector-seal and publish one sealed full generation."""
        self.epoch += 1
        gen = 'gen-%d' % self.epoch
        rows = [(path, text, True) for path, text in sorted(documents.items())]
        manifest = manifest_digest(SCOPE, [(path, sha(text), 1) for path, text, _ in rows])
        assert self.index.stage(gen, SCOPE, rows, base=self.head, source_manifest=manifest,
                                claim_digest=CLAIM, claim_epoch=self.epoch) == 'prepared'
        for path, text in documents.items():
            self.vectors.insert_chunk_set(sha(text), qmd_markdown_title(text, path),
                                          self.profile_id,
                                          [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}])
        self.vectors.seal_generation(gen, self.profile_id)
        assert self.index.publish(self.head, gen, 'claim-%d' % self.epoch,
                                  source_manifest=manifest, claim_digest=CLAIM,
                                  claim_epoch=self.epoch, require_vector=True) == 'published'
        self.head = gen
        self.bodies = dict(documents)
        for path, text in documents.items():
            target = self.wiki / path.split('/', 1)[1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def rows(self):
        return [{'scope': SCOPE, 'generation': self.head, 'path': path,
                 'hash': sha(text), 'body': text, 'score': 1 / (i + 1)}
                for i, (path, text) in enumerate(sorted(self.bodies.items()))]

    @property
    def proof(self):
        return {'productRouteAllowed': True, 'selectionEpoch': 1, 'providerManifest': {},
                'vectorProfile': self.decision['vectorProfile']}

    def route(self, *, verified=True):
        stack = ExitStack()
        stack.enter_context(patch.object(laya_index_host_route, 'select_route',
                                         return_value=dict(self.decision)))
        if verified:
            stack.enter_context(patch.object(laya_install_journal, 'verify_selected_route',
                                             return_value=self.proof))
        else:
            stack.enter_context(patch.object(laya_install_journal, 'verify_selected_route',
                                             side_effect=ValueError('journal_changed')))
        return stack


class Checks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='laya-learning-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.laya = LayaProject(self.base)
        self.addCleanup(self.laya.index.close)

    def serve(self, *, chosen):
        """The real selected recall entry with only provider/runtime faked."""
        import laya_index_runtime
        import laya_local_embed_provider
        import laya_embed_resident
        import laya_incremental_docs_query
        rows = self.laya.rows()
        runtime = SimpleNamespace(index=object(), scope=SCOPE)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(laya_install_journal, 'verify_selected_route',
                               return_value=self.laya.proof))
            enter(patch.object(laya_index_runtime, 'LayaIndexRuntime',
                               return_value=nullcontext(runtime)))
            enter(patch.object(laya_local_embed_provider, 'ReviewedLocalQmdProvider',
                               return_value=lambda *_args: {}))
            enter(patch.object(laya_embed_resident, 'admit_query'))
            enter(patch.object(laya_incremental_docs_query, 'query_incremental',
                               return_value=([], frozenset(), {}, (), None)))
            def query(*_args, pinned_generation, **_kwargs):
                pinned_generation.append(self.laya.head)
                return rows, frozenset()
            enter(patch.object(response, 'query_selected_v2', side_effect=query))
            enter(patch.object(config, 'load_project_config', return_value=self.laya.settings))
            enter(patch('context_learning.live_select.choose',
                        return_value=(chosen, 'selected', {})))
            return response.serve_selected('recall', 'codex', self.laya.decision,
                                           {'prompt': 'Which card has the bridge fact?'})

    def test_corpus_fingerprint_reads_sealed_laya_head_only(self):
        with self.laya.route():
            first = corpus.snapshot(self.laya.project, self.laya.settings)
            again = corpus.snapshot(self.laya.project, self.laya.settings)
        self.assertIsNotNone(first)
        self.assertEqual(first['material']['schema'], 'laya-corpus-snapshot-v1')
        self.assertEqual(first['fingerprint'], again['fingerprint'])
        self.assertEqual(first['generation'], self.laya.head)
        self.assertEqual(first['material']['activeDocuments'],
                         [['wiki', 'card%02d.md' % i, sha(body(i))] for i in range(4)])
        # Identical rows under a new published generation keep the fingerprint.
        self.laya.publish(dict(self.laya.bodies))
        with self.laya.route():
            republished = corpus.snapshot(self.laya.project, self.laya.settings)
        self.assertEqual(republished['fingerprint'], first['fingerprint'])
        self.assertNotEqual(republished['generation'], first['generation'])
        changed = dict(self.laya.bodies)
        changed['wiki/card01.md'] = body(1, ' Revised.')
        self.laya.publish(changed)
        with self.laya.route():
            revised = corpus.snapshot(self.laya.project, self.laya.settings)
        self.assertNotEqual(revised['fingerprint'], first['fingerprint'])
        # An unverifiable selected route never falls back to QMD.
        with self.laya.route(verified=False):
            self.assertIsNone(corpus.snapshot(self.laya.project, self.laya.settings))

    def test_selected_recall_captures_private_and_shared_case_after_output(self):
        pool = self.laya.rows()
        # The query-time selector reads an existing private state only; a
        # prepared checkpoint pointer makes its choice final.
        (self.laya.private / digest(str(self.laya.project))).mkdir(mode=0o700)
        (self.laya.private / digest(str(self.laya.project)) /
         'active-checkpoint.json').write_text('{}')
        with self.laya.route(), patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}):
            result = self.serve(chosen=[pool[2]['path']])
            status = laya_learning.capture_recall(*response._LEARNING_CAPTURE[0])
            fingerprint = corpus.snapshot(self.laya.project, self.laya.settings)['fingerprint']
        self.assertIn('wiki/card02.md', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(status, 'stored')
        state = self.laya.private / digest(str(self.laya.project))
        with closing(sqlite3.connect(state / 'learning.sqlite3')) as db:
            (raw,), = db.execute('SELECT body FROM samples').fetchall()
        sample = json.loads(raw)
        ids = [row['path'] for row in pool]
        self.assertEqual(sample['schema_version'], 3)
        self.assertEqual(sample['host'], 'codex')
        self.assertEqual([c['candidate_id'] for c in sample['candidates']], ids)
        self.assertEqual(sample['sampling']['baseline_selected_ids'], ['wiki/card02.md'])
        self.assertEqual(sample['sampling']['index_revision'], fingerprint)
        self.assertTrue(sample['sampling']['complete_within_returned_bound'])
        with closing(sqlite3.connect(self.laya.shared / 'shared-learning.sqlite3')) as db:
            cases = db.execute('SELECT status,corpus_fingerprint,project_root FROM cases').fetchall()
        self.assertEqual(cases, [('queued', fingerprint, str(self.laya.project))])

    def test_unprepared_selector_captures_served_ranked_rows(self):
        pool = self.laya.rows()
        (self.laya.private / digest(str(self.laya.project))).mkdir(mode=0o700)
        with self.laya.route(), patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}):
            result = self.serve(chosen=[pool[2]['path']])
            status = laya_learning.capture_recall(*response._LEARNING_CAPTURE[0])
        context = result['hookSpecificOutput']['additionalContext']
        for path in [row['path'] for row in pool[:3]]:
            self.assertIn(path, context)
        self.assertEqual(status, 'stored')
        state = self.laya.private / digest(str(self.laya.project))
        with closing(sqlite3.connect(state / 'learning.sqlite3')) as db:
            (raw,), = db.execute('SELECT body FROM samples').fetchall()
        sample = json.loads(raw)
        self.assertEqual(sample['sampling']['baseline_selected_ids'],
                         [row['path'] for row in pool[:3]])

    def test_capture_runs_after_flush_and_never_changes_hook_output(self):
        decision = dict(self.laya.decision)
        events = []
        payload = self.base / 'payload.json'
        payload.write_text(json.dumps({'prompt': 'bridge', 'cwd': str(self.laya.project)}))
        answer = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                         'additionalContext': 'ctx'}}

        def serve(*_args):
            response._LEARNING_CAPTURE[:] = [('codex', decision, {}, {}, [], [])]
            return answer

        def capture(*_args, **_kwargs):
            events.append(('capture', stdout.getvalue()))
            raise RuntimeError('capture exploded')

        stdout = io.StringIO()
        with patch.object(response, 'select_hook_route', return_value=decision), \
             patch.object(response, 'select_route', return_value=decision), \
             patch.object(response, 'serve_selected', side_effect=serve), \
             patch('context_learning.laya_learning.capture_recall', side_effect=capture), \
             redirect_stdout(stdout):
            code = response.main(['serve-hook', 'recall', 'codex', str(payload),
                                  str(self.laya.project)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), answer)
        # The answer was already written when capture started.
        self.assertEqual(json.loads(events[0][1]), answer)
        self.assertEqual(response._LEARNING_CAPTURE, [])
        # A non-recall action never captures.
        events.clear()
        with patch.object(response, 'select_hook_route', return_value=decision), \
             patch.object(response, 'select_route', return_value=decision), \
             patch.object(response, 'serve_selected', side_effect=serve), \
             patch('context_learning.laya_learning.capture_recall', side_effect=capture), \
             redirect_stdout(io.StringIO()):
            response.main(['serve-hook', 'posttool', 'codex', str(payload),
                           str(self.laya.project)])
        self.assertEqual(events, [])

    def test_capture_honors_setting_and_hook_budget(self):
        pool = self.laya.rows()
        disabled = dict(self.laya.settings)
        disabled['contextLearning'] = {**disabled['contextLearning'], 'capture': False}
        with self.laya.route():
            self.assertEqual(laya_learning.capture_recall(
                'codex', self.laya.decision, {'prompt': 'bridge'}, disabled, pool, []),
                'disabled')
            self.assertEqual(laya_learning.capture_recall(
                'codex', self.laya.decision, {'prompt': 'bridge'}, self.laya.settings, pool, [],
                started=time.monotonic() - 60), 'capture_deadline')
        self.assertFalse((self.laya.private / digest(str(self.laya.project))).exists())

    def test_stale_incremental_row_stays_private(self):
        pool = self.laya.rows()
        newer = body(9, ' Incremental.')
        pool[0] = {**pool[0], 'generation': 'gen-incremental', 'hash': sha(newer), 'body': newer}
        with self.laya.route(), patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}):
            status = laya_learning.capture_recall('codex', self.laya.decision,
                {'prompt': 'Which card has the bridge fact?'}, self.laya.settings, pool, [])
        self.assertEqual(status, 'stored')
        state = self.laya.private / digest(str(self.laya.project))
        with closing(sqlite3.connect(state / 'learning.sqlite3')) as db:
            sample = json.loads(db.execute('SELECT body FROM samples').fetchone()[0])
        self.assertEqual(sample['sampling']['snapshot_failures'],
                         [{'candidate_id': 'wiki/card00.md', 'reason': 'candidate_index_stale'}])
        self.assertFalse(sample['sampling']['complete_within_returned_bound'])
        # The incomplete pool is refused by shared admission before any case row.
        last = json.loads((state / 'shared-last-status.json').read_text())
        self.assertEqual(last['status'], 'ineligible')

    def test_training_gate_accepts_verified_laya_route(self):
        lock = self.base / 'lease.lock'

        def run_owned():
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            with patch.object(shared, '_owned_root', return_value=(
                    self.laya.project, self.laya.settings, self.laya.shared)), \
                 patch.object(shared, '_check_owned_lease'), \
                 patch.object(shared, '_training_worker_state',
                              return_value={'outcome': None}), \
                 patch.object(shared, 'run_training_pending',
                              return_value={'status': 'trained'}), \
                 patch.object(shared, '_settle_owned',
                              side_effect=lambda _r, _n, _s, result, _p: result):
                return shared._run_owned_training(self.laya.project, fd)

        with self.laya.route():
            self.assertEqual(run_owned(), {'status': 'trained'})
            self.assertTrue(laya_learning.ready(self.laya.project))
        with self.laya.route(verified=False):
            self.assertEqual(run_owned(), {'status': 'setup_in_progress'})
            self.assertFalse(laya_learning.ready(self.laya.project))

    def test_laya_publish_wakes_learning_without_changing_sync_result(self):
        import runtime_handlers
        import laya_runtime_dispatch
        woken = []
        row = {'target': str(self.laya.project), 'leaseFd': None}
        with patch.object(laya_runtime_dispatch, 'run', return_value={'state': 'indexed'}), \
             patch.object(shared, 'wake_after_index', side_effect=woken.append):
            self.assertTrue(runtime_handlers.laya_sync(row))
        self.assertEqual(woken, [self.laya.project])
        with patch.object(laya_runtime_dispatch, 'run', return_value={'state': 'unchanged'}), \
             patch.object(shared, 'wake_after_index', side_effect=woken.append):
            self.assertTrue(runtime_handlers.laya_sync(row))
        self.assertEqual(len(woken), 1)
        with patch.object(laya_runtime_dispatch, 'run', return_value={'state': 'indexed'}), \
             patch.object(shared, 'wake_after_index', side_effect=OSError('busy')):
            self.assertTrue(runtime_handlers.laya_sync(row))


if __name__ == '__main__':
    unittest.main()
