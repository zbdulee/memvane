#!/usr/bin/env python3
"""Selected v2 raw->wiki durable discovery; no model, QMD, socket, host or extractor.

Reuses the V2 worker harness of laya-product-sync-worker-checks.py: a disposable
A-int DB/claim queue, two disjoint roots, the real route selector, hook entry
point, WakeQueue and sync pipeline. The journal proof, provider and service are
synthetic. The laya-compile handler is replaced by a recorder, so no compile
worker or extractor can run; its call count is the paid-work proxy.
"""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
_BASE = runpy.run_path(str(HERE / 'laya-product-sync-worker-checks.py'),
                       run_name='laya_product_sync_worker_checks')
V2 = _BASE['V2WorkerChecks']
SCOPE = _BASE['SCOPE']
PROFILE = _BASE['PROFILE']

import laya_host_response as response
from laya_index_a import IndexContractError, LayaIndexA
import laya_index_host_route as host_route
from laya_index_vector import profile_identity
import laya_product_sync_worker as worker
import laya_wiki_discovery as discovery
import runtime_handlers
import runtime_work
import wiki_compile_enqueue
import wiki_freshness


class _Idle(Exception):
    """drain found only retry-delayed wakes."""


class DiscoveryChecks(unittest.TestCase):
    # Borrowed V2 harness: real A-int pipeline, synthetic proof/provider/service.
    proof = V2.proof
    provider_class = V2.provider_class
    run_worker = V2.run_worker
    members = V2.members
    hook_world = V2.hook_world
    recall = V2.recall
    assert_refused = V2.assert_refused

    def setUp(self):
        V2.setUp(self)
        self.compile_wakes = []

    def selected_route(self, triggers=('post_sync_source',), expect='candidate'):
        """V2 route with the selected auto-wiki compile settings the selector allows."""
        self.profile = {'profileId': profile_identity(PROFILE['modelId'],
                            PROFILE['embeddingFingerprint'], PROFILE['dimension']),
            'modelId': PROFILE['modelId'], 'embeddingFingerprint': PROFILE['embeddingFingerprint'],
            'dimension': PROFILE['dimension'], 'metric': 'cosine',
            'approvedProviderId': 'synthetic-provider', 'providerArtifactSha256': 'e' * 64,
            'providerRelativePath': 'core/synthetic_provider.py'}
        auto = self.home / '.auto-context'
        auto.mkdir()
        settings = auto / 'settings.json'
        settings.write_text(json.dumps({'collections': ['alpha', 'beta'], 'indexing': True,
            'collectionPaths': {'alpha': 'alpha', 'beta': 'beta'},
            'collectionRoles': {'alpha': 'raw', 'beta': 'wiki'},
            'recallStrategy': 'hierarchical', 'wikiPath': 'beta',
            'compile': {'mode': 'auto-wiki', 'triggers': list(triggers),
                        'extractor': {'builtins': ['claude']}}}), encoding='utf-8')
        record = {'schema': host_route.SCHEMA_V2, 'status': 'active', 'projectRoot': str(self.home),
            'settingsSha256': hashlib.sha256(settings.read_bytes()).hexdigest(),
            'dbPath': str(self.index_path), 'queuePath': str(self.queue_path),
            'dbIncarnation': self.incarnation, 'scopeId': SCOPE,
            'pluginRoot': str(Path(response.__file__).resolve().parents[1]),
            'journalPath': str(self.home / 'install-journal.sqlite'), 'installId': '3' * 32,
            'generation': 'g-' + '4' * 32, 'bundleManifestSha256': '2' * 64,
            'ownershipManifestSha256': '5' * 64, 'vectorProfile': self.profile,
            'collections': [{'name': 'alpha', 'role': 'raw', 'sourceRoot': str(self.alpha)},
                            {'name': 'beta', 'role': 'wiki', 'sourceRoot': str(self.beta)}]}
        (auto / host_route.RECORD).write_text(json.dumps(record), encoding='utf-8')
        decision = host_route.select_route(str(self.home), action='update')
        if expect != 'candidate':
            self.assertNotEqual(decision['state'], 'candidate', decision)
            return
        self.assertEqual(decision['state'], 'candidate', decision)
        self.decision = decision

    # -- helpers ---------------------------------------------------------
    def hook(self, action, payload):
        path = self.payloads / ('payload-%d.json' % len(list(self.payloads.iterdir())))
        path.write_text(json.dumps({'cwd': str(self.home), **payload}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = response.main(['serve-hook', action, 'claude', str(path), str(self.home)])
        return status, out.getvalue(), err.getvalue()

    def session_start(self):
        status, out, err = self.hook('update', {'hook_event_name': 'SessionStart'})
        self.assertEqual((status, err), (0, ''))
        self.assertIn('update queued', out)

    def bash(self, code=0):
        status, out, err = self.hook('file-change-event', {
            'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': 'git pull --ff-only'},
            'tool_response': {'exit_code': code}})
        self.assertEqual((status, out.strip(), err), (0, '{}', ''))

    def model_work(self):
        return len(self.queried), len(self.services), len(self.embedded)

    def drain_all(self, queue):
        """Dispatch every due wake once; laya-compile only records the request."""
        real_run = worker.run
        results, failures = [], []

        def handler(project):
            self.assertEqual(project, self.home)
            try:
                with patch.object(worker, 'run', real_run):
                    results.append(self.run_worker())
            except Exception as exc:
                failures.append(exc)
                raise
            return results[-1]

        def compile_wake(row):
            self.compile_wakes.append(row['target'])
            return True

        def idle(_seconds):
            raise _Idle()
        try:
            with patch.object(worker, 'run', side_effect=handler), \
                    patch.dict(runtime_handlers.HANDLERS, {'laya-compile': compile_wake}):
                status = runtime_work.drain(queue, ready=lambda: True, sleep=idle)
        except _Idle:
            status = {'status': 'waiting'}
        return status, results, failures

    @staticmethod
    def make_due(queue):
        with queue.connect() as db:
            db.execute('UPDATE wakes SET due=0')

    def paths(self):
        return discovery._paths(self.decision)

    def state(self):
        path = self.paths()[0]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return json.loads(path.read_text())

    def grants(self, phase=0):
        """Active (phase 0) or provisional (1) grant count by kind."""
        live = discovery._read_grants(self.paths()[1], self.decision)[phase]
        return {kind: len(ids) for kind, ids in live.items()}

    def accepted(self, kind):
        """A grant whose hook passed every check."""
        grant = discovery.record_grant(self.decision, kind)
        self.assertTrue(discovery.activate_grant(self.decision, kind, grant))
        return grant

    def bash_status(self):
        return self.hook('file-change-event', {
            'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': 'git pull --ff-only'},
            'tool_response': {'exit_code': 0}})

    def two_pending_changes(self):
        """G1 baseline, then two raw creates published as G2 with no grant."""
        self.selected_route()
        self.run_worker()
        for name in ('a.md', 'b.md'):
            (self.alpha / name).write_text('# %s\nnew raw\n' % name, encoding='utf-8')
        self.assertEqual(self.run_worker()['state'], 'unchanged')
        self.assertEqual(sorted(self.state()['pending']), ['alpha/a.md', 'alpha/b.md'])

    def source_rows(self):
        path = self.home / '.auto-context' / 'compile' / 'source-queue.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        for row in rows:
            self.assertNotIn('body', json.dumps(row))
        return sorted((row['source']['path'], row['trigger'], row.get('contentRevision'))
                      for row in rows)

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def published_with_pending_change(self, triggers=('post_sync_source',)):
        """G1 baseline, then an out-of-band raw edit published as G2 with no grant."""
        self.selected_route(triggers)
        first = self.run_worker()
        self.assertEqual(first['state'], 'indexed')
        self.assertEqual(self.state()['pending'], {})
        (self.alpha / 'one.md').write_text('silver otter upstream', encoding='utf-8')
        second = self.run_worker()
        self.assertEqual(second['state'], 'unchanged')
        self.assertEqual(self.state()['pending'],
                         {'alpha/one.md': self.sha(self.alpha / 'one.md')})
        return first, second

    # -- end to end --------------------------------------------------------
    def test_out_of_band_raw_pending_until_accepted_grant(self):
        self.selected_route()
        gone = self.alpha / 'gone.md'
        gone.write_text('# Gone\nThe otter lantern.\n', encoding='utf-8')
        first = self.run_worker()
        self.assertEqual(first['state'], 'indexed')
        # Initial baseline is G1 itself: pre-existing raw files are not compiled.
        self.assertEqual((self.state()['baseGen'], self.state()['pending']),
                         (first['generation'], {}))
        with self.hook_world() as queue:
            # Out-of-band pull: raw edit/create/delete plus a wiki edit.
            one, new = self.alpha / 'one.md', self.alpha / 'new.md'
            one.write_text('# One\nThe otter guards the south dam.\n', encoding='utf-8')
            new.write_text('# New\nThe otter swims the estuary.\n', encoding='utf-8')
            gone.unlink()
            (self.beta / 'two.md').write_text('green heron', encoding='utf-8')
            before = self.model_work()
            self.assert_refused(self.home, 'index_stale_sync_queued')
            self.assertEqual(self.model_work(), before)
            status, results, failures = self.drain_all(queue)
            self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
            second = results[0]
            self.assertEqual(second['state'], 'indexed')
            self.assertEqual(self.members(second['generation']), ['beta/two.md'])
            # A stale-recall wake is no grant: revisions wait, nothing is queued.
            expected = {'alpha/new.md': self.sha(new), 'alpha/one.md': self.sha(one)}
            self.assertEqual((self.state()['baseGen'], self.state()['pending']),
                             (second['generation'], expected))
            self.assertFalse(self.paths()[1].exists())
            self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
            # SessionStart grant: the selected compile triggers do not include
            # session_start_source, so it is denied and only that grant is used.
            embedded = len(self.embedded)
            before = self.model_work()
            self.session_start()
            self.assertEqual(self.model_work(), before)
            self.assertEqual(self.grants(), {'sync': 0, 'session_start': 1})
            status, results, failures = self.drain_all(queue)
            self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
            self.assertEqual(results[0], {'state': 'unchanged',
                                          'generation': second['generation'], 'claim': None})
            self.assertEqual(self.state()['pending'], expected)
            self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
            self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
            # A later successful Bash event carries the accepted sync grant.
            before = self.model_work()
            self.bash()
            self.assertEqual(self.model_work(), before)
            self.assertEqual(self.grants(), {'sync': 1, 'session_start': 0})
            status, results, failures = self.drain_all(queue)
            self.assertEqual(failures, [])
            self.assertEqual(status, {'status': 'drained', 'runs': 2})
            self.assertEqual(results[0]['state'], 'unchanged')
        self.assertEqual(self.source_rows(),
                         [('alpha/new.md', 'post_sync_source', expected['alpha/new.md']),
                          ('alpha/one.md', 'post_sync_source', expected['alpha/one.md'])])
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
        self.assertEqual(self.compile_wakes, [str(self.home)])
        # Unchanged syncs reuse sealed chunk sets: no second inference.
        self.assertEqual(len(self.embedded), embedded)

    def test_selected_session_start_consent_records_session_trigger(self):
        # Real selector + settings: explicit session_start_source is accepted.
        _first, second = self.published_with_pending_change(('session_start_source',))
        embedded = len(self.embedded)
        with self.hook_world() as queue:
            before = self.model_work()
            self.session_start()
            self.assertEqual(self.model_work(), before)
            self.assertEqual(self.grants(), {'sync': 0, 'session_start': 1})
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 2}, []))
        self.assertEqual(results[0], {'state': 'unchanged',
                                      'generation': second['generation'], 'claim': None})
        self.assertEqual(self.source_rows(),
                         [('alpha/one.md', 'session_start_source',
                           self.sha(self.alpha / 'one.md'))])
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
        self.assertEqual(self.compile_wakes, [str(self.home)])
        self.assertEqual(len(self.embedded), embedded)

    def test_post_tool_consent_does_not_cover_session_start(self):
        # post_tool_source is not widened to SessionStart: the grant is denied
        # and spent, the revision stays pending, and no paid work is requested.
        _first, second = self.published_with_pending_change(('post_tool_source',))
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        embedded = len(self.embedded)
        with self.hook_world() as queue:
            before = self.model_work()
            self.session_start()
            self.assertEqual(self.model_work(), before)
            self.assertEqual(self.grants(), {'sync': 0, 'session_start': 1})
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
        self.assertEqual(results[0], {'state': 'unchanged',
                                      'generation': second['generation'], 'claim': None})
        self.assertEqual((self.state()['baseGen'], self.state()['pending']),
                         (second['generation'], expected))
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual(len(self.embedded), embedded)

    def test_selected_route_rejects_unsupported_compile_trigger(self):
        # Accepting session_start_source does not open the selector to others.
        self.selected_route(('session_start_source', 'repeated_recall'), expect='refused')

    def test_absent_state_after_unobserved_publish_uses_sealed_base(self):
        self.selected_route()
        first = self.run_worker()
        self.paths()[0].unlink()  # as if G1 predates discovery
        (self.alpha / 'one.md').write_text('silver otter moved', encoding='utf-8')
        with patch.object(discovery, 'drain_after_sync',
                          return_value={'state': 'drained', 'reason': None}):
            second = self.run_worker()
        self.assertEqual(second['state'], 'unchanged')
        self.assertFalse(self.paths()[0].exists())
        with self.hook_world() as queue:
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual((failures, results[0]['state']), ([], 'unchanged'))
        # A deleted raw cursor cannot prove an earlier source revision. Normal
        # cutover seeds it before route activation; this synthetic deletion is
        # surfaced as cursorStarted rather than guessing from wiki membership.
        self.assertEqual(self.source_rows(), [])
        self.assertEqual((self.state()['baseGen'], self.state()['pending']),
                         (second['generation'], {}))
        self.assertEqual(first['generation'], second['generation'])

    def test_installed_route_missing_source_snapshot_fails_closed(self):
        self.selected_route()
        self.run_worker()
        self.paths()[0].unlink()
        Path(self.decision['journalPath']).touch()
        (self.alpha / 'one.md').write_text('silver otter changed', encoding='utf-8')
        result = self.run_worker()
        self.assertEqual(result['wikiDiscovery'],
                         {'state': 'blocked', 'reason': 'snapshot_missing'})
        self.assertFalse(self.paths()[0].exists())
        self.assertEqual(self.source_rows(), [])

    def test_preactivation_seed_detects_first_raw_edit_without_raw_membership(self):
        self.selected_route()
        first = self.run_worker()
        old_hash = self.sha(self.alpha / 'one.md')
        self.paths()[0].unlink()
        Path(self.decision['journalPath']).touch()
        discovery.seed_source_snapshot(self.decision, {'alpha/one.md': old_hash})
        (self.alpha / 'one.md').write_text('silver otter first edit', encoding='utf-8')
        result = self.run_worker()
        self.assertEqual(result['state'], 'unchanged')
        self.assertEqual(result['generation'], first['generation'])
        self.assertEqual(self.state()['pending'],
                         {'alpha/one.md': self.sha(self.alpha / 'one.md')})
        self.assertEqual(self.members(result['generation']), ['beta/two.md'])

    def test_preactivation_seed_retains_lagging_qmd_inventory_for_later_discovery(self):
        self.selected_route()
        self.run_worker()
        self.paths()[0].unlink()
        old_hash = self.sha(self.alpha / 'one.md')
        (self.alpha / 'one.md').write_text('changed after QMD inventory', encoding='utf-8')
        self.assertEqual(discovery.seed_source_snapshot(
            self.decision, {'alpha/one.md': old_hash}), {'alpha/one.md': old_hash})
        self.assertEqual(self.state()['rawSnapshot'], {'alpha/one.md': old_hash})

    def test_preactivation_seed_refuses_raw_corpus_over_discovery_scan_cap(self):
        self.selected_route()
        self.run_worker()
        self.paths()[0].unlink()
        for number in range(200):
            (self.alpha / ('extra-%03d.md' % number)).write_text('x', encoding='utf-8')
        with self.assertRaisesRegex(discovery.DiscoveryBlocked, 'raw_source_stale'):
            discovery.seed_source_snapshot(self.decision, {})
        self.assertFalse(self.paths()[0].exists())

    def publish_blocked(self, change):
        """Publish one generation while discovery blocks before its first journal."""
        grants = self.paths()[1]
        grants.write_bytes(b'{not json')
        grants.chmod(0o600)
        change()
        result = self.run_worker()
        self.assertIn(result['state'], ('indexed', 'unchanged'))
        self.assertEqual(result['wikiDiscovery'], {'state': 'blocked', 'reason': 'state_corrupt'})
        self.assertFalse(self.paths()[0].exists())
        grants.unlink()
        return result['generation']

    def three_blocked_publishes(self):
        """G1 baseline, G2 edits one.md, G3 creates three.md; never a cursor."""
        self.selected_route()
        one, three = self.alpha / 'one.md', self.alpha / 'three.md'
        g1 = self.publish_blocked(lambda: None)
        def change_one():
            one.write_text('silver otter G2', encoding='utf-8')
            (self.beta / 'two.md').write_text('wiki G2', encoding='utf-8')
        def change_three():
            three.write_text('# Three\nThe otter naps.\n', encoding='utf-8')
            (self.beta / 'two.md').write_text('wiki G3', encoding='utf-8')
        g2 = self.publish_blocked(change_one)
        g3 = self.publish_blocked(change_three)
        with closing(LayaIndexA(self.index_path)) as index:
            bases = [index.db.execute('SELECT base_gen FROM candidate_seals WHERE gen=?',
                                      (gen,)).fetchone()[0] for gen in (g1, g2, g3)]
        # G3's own sealed base is G2: a one-step diff would never see one.md.
        self.assertEqual(bases, [None, g1, g2])
        return g3, [('alpha/one.md', 'post_sync_source', self.sha(one)),
                    ('alpha/three.md', 'post_sync_source', self.sha(three))]

    def test_absent_cursor_after_several_publishes_uses_one_step_base(self):
        head, expected = self.three_blocked_publishes()
        with self.hook_world() as queue:
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual((failures, results[0]['state'], results[0]['generation']),
                         ([], 'unchanged', head))
        # Base is G2, one publish back: the G2->G3 change (three.md) is found.
        # The G1->G2 edit of one.md predates it and is not backfilled.
        self.assertEqual(self.source_rows(), [])
        self.assertEqual((self.state()['baseGen'], self.state()['pending']), (head, {}))
        self.assertEqual(self.compile_wakes, [])

    def test_absent_cursor_does_not_depend_on_lineage_length(self):
        head, expected = self.three_blocked_publishes()
        with self.hook_world() as queue:
            self.bash()
            # A long history no longer blocks the first run: it walks one link.
            with patch.object(discovery, 'MAX_LINEAGE', 1):
                _status, results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        # Not blocked; the cursorless start is reported.
        self.assertNotEqual(results[0]['wikiDiscovery']['state'], 'blocked')
        self.assertIs(results[0]['wikiDiscovery']['cursorStarted'], True)
        self.assertEqual((failures, results[0]['generation']), ([], head))
        self.assertEqual(self.source_rows(), [])
        self.assertEqual((self.state()['baseGen'], self.state()['pending']), (head, {}))

    def test_deleted_cursor_restarts_one_step_back_and_is_reported(self):
        self.selected_route()
        self.run_worker()
        self.paths()[0].unlink()  # e.g. removed by hand
        (self.alpha / 'one.md').write_text('silver otter after delete', encoding='utf-8')
        with self.hook_world() as queue:
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertIs(results[0]['wikiDiscovery']['cursorStarted'], True)
        self.assertEqual(self.source_rows(), [])
        # Later runs have a cursor again and report nothing extra.
        with self.hook_world() as queue:
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertNotIn('wikiDiscovery', results[0])

    def test_lineage_walk_fails_closed_on_broken_cyclic_or_overlimit(self):
        # Synthetic seal/generation rows only: A-int triggers forbid forging these.
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE generations(gen TEXT PRIMARY KEY, scope TEXT, state TEXT);'
                         'CREATE TABLE candidate_seals(gen TEXT PRIMARY KEY, scope TEXT,'
                         ' base_gen TEXT);')
        other = 'b' * 32 if SCOPE != 'b' * 32 else 'c' * 32

        def chain(*links):
            db.execute('DELETE FROM generations')
            db.execute('DELETE FROM candidate_seals')
            for gen, base, scope, state, sealed in links:
                db.execute('INSERT INTO generations VALUES(?,?,?)', (gen, scope, state))
                if sealed:
                    db.execute('INSERT INTO candidate_seals VALUES(?,?,?)', (gen, scope, base))

        def ok(gen, base, scope=SCOPE, state='published', sealed=True):
            return gen, base, scope, state, sealed
        chain(ok('g1', None), ok('g2', 'g1'), ok('g3', 'g2'))
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g3'), 'g1')
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g1'), 'g1')
        with patch.object(discovery, 'MAX_LINEAGE', 3):
            self.assertEqual(discovery._lineage_root(db, SCOPE, 'g3'), 'g1')
        # A step limit returns the checked generation that many links back.
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g3', 1), 'g2')
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g3', 2), 'g1')
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g1', 1), 'g1')
        chain(ok('g1', None, state='prepared'), ok('g2', 'g1'), ok('g3', 'g2'))
        self.assertEqual(discovery._lineage_root(db, SCOPE, 'g3', 1), 'g2')
        for broken in (ok('g2', 'g1', state='prepared'), ok('g2', 'g1', scope=other),
                       ok('g2', 'g1', sealed=False)):
            chain(ok('g1', None), broken, ok('g3', 'g2'))
            with self.assertRaisesRegex(discovery.DiscoveryBlocked, '^lineage_broken$'):
                discovery._lineage_root(db, SCOPE, 'g3', 1)
        chain(ok('g1', None), ok('g2', 'g1'), ok('g3', 'g2'))
        cases = {
            'missing': ('lineage_broken', 'g3', [ok('g2', 'g1'), ok('g3', 'g2')]),
            'unpublished': ('lineage_broken', 'g3',
                            [ok('g1', None, state='prepared'), ok('g2', 'g1'), ok('g3', 'g2')]),
            'foreign': ('lineage_broken', 'g3',
                        [ok('g1', None, scope=other), ok('g2', 'g1'), ok('g3', 'g2')]),
            'unsealed': ('lineage_broken', 'g3',
                         [ok('g1', None, sealed=False), ok('g2', 'g1'), ok('g3', 'g2')]),
            'invalid': ('lineage_broken', 'g2', [ok('g2', '../g1')]),
            'head': ('head_base_unproved', 'g1', [ok('g1', None, sealed=False)]),
            'cycle': ('lineage_cycle', 'g3', [ok('g1', 'g3'), ok('g2', 'g1'), ok('g3', 'g2')]),
            'self': ('lineage_cycle', 'g1', [ok('g1', 'g1')]),
        }
        for name, (reason, head, links) in cases.items():
            with self.subTest(name=name):
                chain(*links)
                with self.assertRaisesRegex(discovery.DiscoveryBlocked, '^%s$' % reason):
                    discovery._lineage_root(db, SCOPE, head)
        chain(ok('g1', None), ok('g2', 'g1'), ok('g3', 'g2'))
        with patch.object(discovery, 'MAX_LINEAGE', 2), \
                self.assertRaisesRegex(discovery.DiscoveryBlocked, '^lineage_over_cap$'):
            discovery._lineage_root(db, SCOPE, 'g3')

    # -- fault windows -----------------------------------------------------
    def test_fault_after_publish_keeps_wake_revisions_and_grant(self):
        self.selected_route()
        first = self.run_worker()
        with self.hook_world() as queue:
            (self.alpha / 'one.md').write_text('silver otter fault', encoding='utf-8')
            self.bash()
            with patch.object(discovery, 'drain_after_sync',
                              side_effect=RuntimeError('crash_after_publish')):
                status, results, failures = self.drain_all(queue)
            self.assertEqual((status, results), ({'status': 'waiting'}, []))
            self.assertEqual([type(exc).__name__ for exc in failures], ['RuntimeError'])
            # runtime_work.drain kept the wake: an exception is not an ACK.
            self.assertEqual([(row['kind'], row['attempts'], row['reason']) for row in queue.pending()],
                             [('laya-sync', 1, 'RuntimeError')])
            self.assertEqual((self.state()['baseGen'], self.state()['pending']),
                             (first['generation'], {}))
            self.assertEqual(self.grants()['sync'], 1)
            self.make_due(queue)
            _status, results, failures = self.drain_all(queue)
        self.assertEqual((failures, results[0]['state']), ([], 'unchanged'))
        self.assertEqual(results[0]['generation'], first['generation'])
        self.assertEqual(self.source_rows(),
                         [('alpha/one.md', 'post_sync_source', self.sha(self.alpha / 'one.md'))])
        self.assertEqual((self.state()['pending'], self.grants()['sync']), ({}, 0))

    def test_fault_after_journal_keeps_pending_and_grant(self):
        _first, second = self.published_with_pending_change()
        real = wiki_compile_enqueue.enqueue_changed_sources
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('synthetic_disk_full')
            return real(*args, **kwargs)
        with self.hook_world() as queue, \
                patch.object(wiki_compile_enqueue, 'enqueue_changed_sources', side_effect=failing):
            self.bash()
            _status, _results, failures = self.drain_all(queue)
            self.assertEqual([type(exc).__name__ for exc in failures], ['OSError'])
            self.assertEqual(self.source_rows(), [])
            self.assertEqual(self.state()['pending'],
                             {'alpha/one.md': self.sha(self.alpha / 'one.md')})
            self.assertEqual(self.state()['baseGen'], second['generation'])
            self.assertEqual(self.grants()['sync'], 1)
            self.assertEqual(queue.pending()[0]['kind'], 'laya-sync')
            self.make_due(queue)
            _status, _results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertEqual(len(self.source_rows()), 1)
        self.assertEqual((self.state()['pending'], self.grants()['sync']), ({}, 0))

    def test_fault_after_enqueue_retry_does_not_duplicate(self):
        self.published_with_pending_change()
        real = wiki_compile_enqueue.enqueue_changed_sources
        calls = []

        def crash_after(*args, **kwargs):
            result = real(*args, **kwargs)
            calls.append(result)
            if len(calls) == 1:
                raise RuntimeError('crash_before_ack')
            return result
        with self.hook_world() as queue, \
                patch.object(wiki_compile_enqueue, 'enqueue_changed_sources',
                             side_effect=crash_after):
            self.bash()
            _status, _results, failures = self.drain_all(queue)
            self.assertEqual([type(exc).__name__ for exc in failures], ['RuntimeError'])
            rows = self.source_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(self.state()['pending']), 1)
            self.assertEqual(self.grants()['sync'], 1)
            self.make_due(queue)
            _status, _results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        # The live-queue revision key coalesces the replay; ACK follows.
        self.assertEqual(calls[1]['reason'], 'no_sources')
        self.assertEqual(self.source_rows(), rows)
        self.assertEqual((self.state()['pending'], self.grants()['sync']), ({}, 0))

    # -- bounds and policy -------------------------------------------------
    def test_per_wake_cap_defers_until_next_grant(self):
        self.selected_route()
        self.run_worker()
        for name in ('a.md', 'b.md'):
            (self.alpha / name).write_text('# %s\nnew raw\n' % name, encoding='utf-8')
        with self.hook_world() as queue, patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '1'}):
            self.bash()
            _status, results, failures = self.drain_all(queue)
            self.assertEqual((failures, results[0]['state']), ([], 'unchanged'))
            self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md'])
            self.assertEqual(list(self.state()['pending']), ['alpha/b.md'])
            # The capped grant is used up: a grantless wake does not continue.
            self.assertEqual(self.grants()['sync'], 0)
            self.assertEqual(self.run_worker()['state'], 'unchanged')
            self.assertEqual(len(self.source_rows()), 1)
            self.bash()
            _status, _results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md', 'alpha/b.md'])
        self.assertEqual(self.state()['pending'], {})

    def test_pending_over_cap_skips_the_burst_instead_of_blocking(self):
        self.selected_route()
        self.run_worker()
        for name in ('a.md', 'b.md', 'c.md'):
            (self.alpha / name).write_text('# %s\nnew raw\n' % name, encoding='utf-8')
        with self.hook_world() as queue, patch.object(discovery, 'MAX_PENDING', 2):
            self.bash()
            _status, results, failures = self.drain_all(queue)
            self.assertEqual(failures, [])
            self.assertEqual(results[0]['wikiDiscovery']['reason'], 'pending_over_cap_skipped')
            self.assertEqual(results[0]['wikiDiscovery']['skipped'], 3)
            # The cursor moves to head with nothing pending; nothing is queued.
            self.assertEqual(self.state()['pending'], {})
            self.assertEqual(self.source_rows(), [])
            self.assertEqual(self.grants()['sync'], 0)
            # A later small change is discovered normally: no permanent block.
            (self.alpha / 'a.md').write_text('# a\nedited again\n', encoding='utf-8')
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md'])

    def test_corrupt_grants_file_is_set_aside_not_failing_every_hook(self):
        self.selected_route()
        self.run_worker()
        grants_path = self.paths()[1]
        grants_path.write_text('{not json', encoding='utf-8')
        grants_path.chmod(0o600)
        (self.alpha / 'a.md').write_text('# a\nnew raw\n', encoding='utf-8')
        with self.hook_world() as queue:
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        kept = [p for p in grants_path.parent.iterdir()
                if p.name.startswith(grants_path.name + '.corrupt-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{not json')
        self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md'])

    def test_wiki_only_change_enqueues_nothing_and_uses_grant(self):
        self.selected_route()
        self.run_worker()
        with self.hook_world() as queue:
            (self.beta / 'two.md').write_text('green heron', encoding='utf-8')
            self.bash()
            _status, results, failures = self.drain_all(queue)
        self.assertEqual((failures, results[0]['state']), ([], 'indexed'))
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual((self.state()['pending'], self.grants()['sync']), ({}, 0))

    def test_unknown_or_corrupt_state_fails_closed_without_reset(self):
        self.selected_route()
        first = self.run_worker()
        path = self.paths()[0]
        valid = path.read_bytes()
        (self.alpha / 'one.md').write_text('silver otter corrupt', encoding='utf-8')
        foreign = json.loads(valid)
        foreign['rawCollections'] = ['beta']
        target = self.home / 'elsewhere.json'
        target.write_bytes(valid)
        target.chmod(0o600)
        cases = {'state_corrupt': (b'{not json', 0o600, False),
                 'state_roots_changed': (json.dumps(foreign).encode(), 0o600, False),
                 'state_unsafe': (valid, 0o644, False),
                 'state_unreadable': (None, None, True)}
        seen = []
        for reason, (data, mode, link) in cases.items():
            with self.subTest(reason=reason):
                path.unlink()
                if link:
                    path.symlink_to(target)
                else:
                    path.write_bytes(data)
                    path.chmod(mode)
                result = self.run_worker()
                seen.append(result['generation'])
                self.assertEqual(result['wikiDiscovery'], {'state': 'blocked', 'reason': reason})
                if link:
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(target.read_bytes(), valid)
                else:
                    self.assertEqual(path.read_bytes(), data)
        self.assertEqual(seen[0], first['generation'])
        self.assertEqual(self.source_rows(), [])
        # Nothing was reset: restoring the cursor still discovers G1->G2.
        path.unlink()
        path.write_bytes(valid)
        path.chmod(0o600)
        with self.hook_world() as queue:
            self.bash()
            _status, _results, failures = self.drain_all(queue)
        self.assertEqual(failures, [])
        self.assertEqual(self.source_rows(),
                         [('alpha/one.md', 'post_sync_source', self.sha(self.alpha / 'one.md'))])

    def test_index_stale_recall_and_failed_bash_record_no_grant(self):
        self.selected_route()
        self.run_worker()
        with self.hook_world() as queue:
            (self.alpha / 'one.md').write_text('silver otter edited', encoding='utf-8')
            (self.beta / 'two.md').write_text('wiki changed', encoding='utf-8')
            status, _out, err = self.hook('index', {
                'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                'tool_input': {'file_path': str(self.alpha / 'one.md')}})
            self.assertEqual((status, err), (0, ''))
            self.assert_refused(self.home, 'index_stale_sync_queued')
            self.bash(code=1)
            self.assertFalse(self.paths()[1].exists())
            self.assertEqual([row['kind'] for row in queue.pending()], ['laya-sync'])
            self.session_start()
            self.bash()
        self.assertEqual(self.grants(), {'sync': 1, 'session_start': 1})
        self.assertEqual(self.paths()[1].stat().st_mode & 0o777, 0o600)

    def test_grant_recorded_mid_run_survives_consumption(self):
        self.published_with_pending_change()
        first = self.accepted('sync')
        observed = discovery.observe_grants(self.decision)
        empty = {'sync': [], 'session_start': []}
        self.assertEqual(observed, {'grants': {'sync': [first], 'session_start': []},
                                    'provisional': empty})
        later = self.accepted('sync')  # after the run's snapshot
        with self.hook_world():
            outcome = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None)
        self.assertEqual((outcome['state'], outcome['queued']), ('drained', 1))
        self.assertEqual(discovery._read_grants(self.paths()[1], self.decision),
                         ({'sync': [later], 'session_start': []}, empty))

    # -- grant lifecycle ---------------------------------------------------
    def test_refused_sync_wake_revokes_grant_before_unrelated_wake(self):
        # Before the fix the counted grant outlived the refused hook and paid
        # for the next stale-recall wake's discovery.
        _first, second = self.published_with_pending_change()
        with self.hook_world() as queue:
            refusals = {'selected_sync_enqueue_failed':
                            {'return_value': {'status': 'deferred_queue_capacity'}},
                        'selected_proof_or_query_failed':
                            {'side_effect': sqlite3.OperationalError('database is locked')}}
            for code, refusal in refusals.items():
                with self.subTest(code=code), patch.object(runtime_work, 'request', **refusal):
                    self.assertEqual(self.bash_status(),
                                     (3, '', '[laya-index] %s; QMD fallback refused\n' % code))
                    self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
                    self.assertEqual(queue.pending(), [])
            # A later, unrelated out-of-band change woken by stale recall only.
            three = self.alpha / 'three.md'
            three.write_text('# Three\nThe otter naps.\n', encoding='utf-8')
            (self.beta / 'two.md').write_text('wiki changed', encoding='utf-8')
            self.assert_refused(self.home, 'index_stale_sync_queued')
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
        self.assertEqual(results[0]['state'], 'indexed')
        self.assertNotEqual(results[0]['generation'], second['generation'])
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual(self.state()['pending'],
                         {'alpha/one.md': self.sha(self.alpha / 'one.md'),
                          'alpha/three.md': self.sha(three)})
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})

    def test_hook_rejected_after_wake_revokes_grant(self):
        # The wake is queued, then the SessionStart hook fails: its run must
        # not use the accepted session_start consent of a rejected event.
        _first, second = self.published_with_pending_change(('session_start_source',))
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        with self.hook_world() as queue:
            with patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                              return_value=None):
                self.assertEqual(self.hook('update', {'hook_event_name': 'SessionStart'}),
                                 (3, '', '[laya-index] wiki_pending_refresh_unknown; '
                                         'QMD fallback refused\n'))
            self.assertEqual([row['kind'] for row in queue.pending()], ['laya-sync'])
            self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})
            self.assertEqual(self.grants(1), {'sync': 0, 'session_start': 0})
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
        self.assertEqual(results[0], {'state': 'unchanged',
                                      'generation': second['generation'], 'claim': None})
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual(self.state()['pending'], expected)

    def test_run_inside_failing_hook_never_uses_its_grant(self):
        # Residual race: a worker already running another laya-sync wake runs
        # this event's wake before the hook's later checks finish, then the
        # hook fails. A grant usable on record paid for that run's compile;
        # a provisional one is never usable.
        _first, second = self.published_with_pending_change(('session_start_source',))
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        concurrent = []
        with self.hook_world() as queue:
            def run_then_fail(_root):
                concurrent.append((self.drain_all(queue), self.grants(1)))
                return None
            with patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                              side_effect=run_then_fail):
                self.assertEqual(self.hook('update', {'hook_event_name': 'SessionStart'}),
                                 (3, '', '[laya-index] wiki_pending_refresh_unknown; '
                                         'QMD fallback refused\n'))
            (status, results, failures), waiting = concurrent[0]
            self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
            self.assertEqual(results[0], {'state': 'unchanged',
                                          'generation': second['generation'], 'claim': None})
            self.assertEqual(waiting, {'sync': 0, 'session_start': 1})
            self.assertEqual(queue.pending(), [])
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual(self.state()['pending'], expected)
        self.assertEqual((self.grants(), self.grants(1)),
                         ({'sync': 0, 'session_start': 0}, {'sync': 0, 'session_start': 0}))

    def test_run_before_activation_gets_follow_up_wake(self):
        # The event's coalesced wake ran and was ACKed while the grant was
        # still provisional; activation must be followed by its own wake.
        _first, second = self.published_with_pending_change(('session_start_source',))
        concurrent = []
        with self.hook_world() as queue:
            def run_then_pass(_root):
                concurrent.append((self.drain_all(queue), self.grants(1), self.source_rows()))
                return []
            with patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                              side_effect=run_then_pass):
                self.session_start()
            (status, results, failures), waiting, rows = concurrent[0]
            self.assertEqual((status, failures, waiting, rows),
                             ({'status': 'drained', 'runs': 1}, [],
                              {'sync': 0, 'session_start': 1}, []))
            self.assertEqual(results[0]['state'], 'unchanged')
            self.assertEqual((self.grants(), self.grants(1)),
                             ({'sync': 0, 'session_start': 1}, {'sync': 0, 'session_start': 0}))
            self.assertEqual([row['kind'] for row in queue.pending()], ['laya-sync'])
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 2}, []))
        self.assertEqual(results[0], {'state': 'unchanged',
                                      'generation': second['generation'], 'claim': None})
        self.assertEqual(self.source_rows(),
                         [('alpha/one.md', 'session_start_source',
                           self.sha(self.alpha / 'one.md'))])
        self.assertEqual((self.state()['pending'], self.grants()),
                         ({}, {'sync': 0, 'session_start': 0}))
        self.assertEqual(self.compile_wakes, [str(self.home)])

    def test_refused_follow_up_wake_withdraws_unless_served(self):
        self.published_with_pending_change()
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        zero = {'sync': 0, 'session_start': 0}
        real = runtime_work.request
        calls, served = [], []

        def second_refused(during=None):
            def request(kind, target='', **options):
                calls.append((kind, options.get('start', True)))
                if len(calls) == 2:
                    if during is not None:
                        served.append(during())
                    return {'status': 'deferred_queue_capacity'}
                return real(kind, target, **options)
            return request
        with self.hook_world() as queue:
            # As if the first wake was ACKed and the follow-up's new row met a
            # full queue: the active grant is withdrawn, the hook fails.
            with patch.object(runtime_work, 'request', side_effect=second_refused()):
                self.assertEqual(self.bash_status(),
                                 (3, '', '[laya-index] selected_wiki_wake_failed; '
                                         'QMD fallback refused\n'))
            self.assertEqual(calls, [('laya-sync', False), ('laya-sync', True)])
            self.assertEqual((self.grants(), self.grants(1)), (zero, zero))
            # The event's first wake still runs, without consent.
            status, results, failures = self.drain_all(queue)
            self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
            self.assertEqual((self.source_rows(), self.state()['pending']), ([], expected))
            # A run that took the lock after activation already used it.
            calls.clear()
            with patch.object(runtime_work, 'request',
                              side_effect=second_refused(lambda: self.drain_all(queue))):
                self.assertEqual(self.bash_status(), (0, '{}\n', ''))
            self.assertEqual(calls[:2], [('laya-sync', False), ('laya-sync', True)])
            self.assertEqual(served[0][2], [])
        self.assertEqual(self.source_rows(),
                         [('alpha/one.md', 'post_sync_source', expected['alpha/one.md'])])
        self.assertEqual((self.state()['pending'], self.grants(), self.grants(1)),
                         ({}, zero, zero))

    def test_uncertain_grant_writes_never_become_usable(self):
        self.published_with_pending_change()
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        zero = {'sync': 0, 'session_start': 0}
        real = discovery._write_grants

        def faults(*plan):
            steps = iter(plan)

            def write(*args):
                if next(steps) == 'landed':
                    real(*args)  # the replace landed; the directory fsync failed
                raise OSError('synthetic_fsync')
            return write
        # Record landed, its cleanup did not: an id nobody can activate.
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'refused')):
            with self.assertRaises(OSError):
                discovery.record_grant(self.decision, 'sync')
        stray = {'sync': 1, 'session_start': 0}
        self.assertEqual((self.grants(), self.grants(1)), (zero, stray))
        observed = discovery.observe_grants(self.decision)
        with self.hook_world():
            outcome = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None)
        self.assertEqual((outcome['state'], outcome['reason']), ('pending', 'no_accepted_grant'))
        self.assertEqual((self.grants(), self.grants(1)), (zero, stray))
        # Activation landed, the withdrawal landed too: proved inactive.
        grant = discovery.record_grant(self.decision, 'session_start')
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'landed')):
            with self.assertRaises(OSError):
                discovery.activate_grant(self.decision, 'session_start', grant)
        self.assertEqual((self.grants(), self.grants(1)), (zero, stray))
        self.assertFalse(discovery.activate_grant(self.decision, 'session_start', grant))
        # Activation landed, the withdrawal did not: reported, never silent.
        grant = discovery.record_grant(self.decision, 'session_start')
        with patch.object(discovery, '_write_grants', side_effect=faults('landed', 'refused')):
            with self.assertRaises(discovery.GrantUncertain):
                discovery.activate_grant(self.decision, 'session_start', grant)
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 1})
        self.assertEqual((self.source_rows(), self.state()['pending']), ([], expected))

    def test_hook_activation_failure_fails_closed(self):
        self.published_with_pending_change()
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        zero = {'sync': 0, 'session_start': 0}
        cases = (('selected_wiki_grant_failed', discovery.DiscoveryBusy('lock_busy'), zero),
                 # Uncertain: the provisional id is left inert, never activated.
                 ('selected_wiki_grant_revoke_failed', discovery.GrantUncertain('grant_uncertain'),
                  {'sync': 1, 'session_start': 0}))
        with self.hook_world() as queue:
            for code, error, waiting in cases:
                with self.subTest(code=code), \
                        patch.object(discovery, 'activate_grant', side_effect=error):
                    self.assertEqual(self.bash_status(),
                                     (3, '', '[laya-index] %s; QMD fallback refused\n' % code))
                    self.assertEqual((self.grants(), self.grants(1)), (zero, waiting))
            # Both events' first wakes coalesced; no follow-up was requested.
            self.assertEqual([(row['kind'], row['revision']) for row in queue.pending()],
                             [('laya-sync', 2)])
            status, results, failures = self.drain_all(queue)
        self.assertEqual((status, failures), ({'status': 'drained', 'runs': 1}, []))
        self.assertEqual((self.source_rows(), self.compile_wakes), ([], []))
        self.assertEqual(self.state()['pending'], expected)

    def test_revoke_after_snapshot_is_not_used_by_that_run(self):
        self.published_with_pending_change()
        grant = self.accepted('sync')
        observed = discovery.observe_grants(self.decision)
        self.assertTrue(discovery.revoke_grant(self.decision, grant))
        self.assertFalse(discovery.revoke_grant(self.decision, grant))
        with self.hook_world():
            outcome = discovery.drain_after_sync(self.decision, observed, recheck=lambda: None)
        self.assertEqual((outcome['state'], outcome['reason']), ('pending', 'no_accepted_grant'))
        self.assertEqual(self.source_rows(), [])
        self.assertEqual(self.state()['pending'],
                         {'alpha/one.md': self.sha(self.alpha / 'one.md')})

    def test_failed_grant_removal_after_ack_never_pays_deferred_tail(self):
        self.two_pending_changes()
        grant = self.accepted('sync')
        observed = discovery.observe_grants(self.decision)

        def full_disk(*_args, **_kwargs):
            raise OSError('synthetic_disk_full')
        with self.hook_world(), patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '1'}):
            with patch.object(discovery, '_write_grants', side_effect=full_disk):
                with self.assertRaises(OSError):
                    discovery.drain_after_sync(self.decision, observed, recheck=lambda: None)
            # ACK landed with the spent id; the grant file still lists it.
            self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md'])
            self.assertEqual(list(self.state()['pending']), ['alpha/b.md'])
            self.assertEqual(self.state()['spentGrants'], [grant])
            self.assertEqual(self.grants()['sync'], 1)
            # The retried wake re-observes the same id but may not use it again.
            retry = discovery.observe_grants(self.decision)
            self.assertEqual(retry['grants']['sync'], [grant])
            outcome = discovery.drain_after_sync(self.decision, retry, recheck=lambda: None)
        self.assertEqual((outcome['state'], outcome['reason']), ('pending', 'no_accepted_grant'))
        self.assertEqual([row[0] for row in self.source_rows()], ['alpha/a.md'])
        self.assertEqual(list(self.state()['pending']), ['alpha/b.md'])
        self.assertEqual(self.grants(), {'sync': 0, 'session_start': 0})

    def test_blocked_run_retires_observed_grant_and_keeps_revisions(self):
        self.published_with_pending_change()
        expected = {'alpha/one.md': self.sha(self.alpha / 'one.md')}
        self.accepted('sync')
        waiting = discovery.record_grant(self.decision, 'sync')  # hook still running
        observed = discovery.observe_grants(self.decision)
        later = self.accepted('session_start')  # after the snapshot
        with self.hook_world():
            with patch.object(discovery, '_project_config',
                              side_effect=discovery.DiscoveryBlocked('settings_changed')):
                outcome = discovery.drain_after_sync(self.decision, observed,
                                                     recheck=lambda: None)
            self.assertEqual(outcome, {'state': 'blocked', 'reason': 'settings_changed'})
            # The observed provisional id is not the worker's to retire.
            self.assertEqual(discovery._read_grants(self.paths()[1], self.decision),
                             ({'sync': [], 'session_start': [later]},
                              {'sync': [waiting], 'session_start': []}))
            self.assertEqual(self.state()['pending'], expected)
            # A later grantless wake (stale recall) cannot reach the retired id.
            outcome = discovery.drain_after_sync(
                self.decision, {'grants': {'sync': [], 'session_start': []}},
                recheck=lambda: None)
        self.assertEqual((outcome['state'], outcome['reason']), ('pending', 'no_accepted_grant'))
        self.assertEqual(self.source_rows(), [])
        self.assertEqual(self.state()['pending'], expected)

    def test_route_change_before_enqueue_raises_without_writes(self):
        self.published_with_pending_change()
        self.accepted('sync')
        observed = discovery.observe_grants(self.decision)
        before = self.paths()[0].read_bytes()

        def changed():
            raise IndexContractError('selected_proof_changed')
        with self.hook_world():
            with self.assertRaisesRegex(IndexContractError, 'selected_proof_changed'):
                discovery.drain_after_sync(self.decision, observed, recheck=changed)
        self.assertEqual(self.paths()[0].read_bytes(), before)
        self.assertEqual((self.source_rows(), self.grants()['sync']), ([], 1))

    def test_v1_route_writes_no_discovery_state(self):
        decision = {key: value for key, value in self.decision.items()
                    if key not in ('collections', 'collectionRoots')}
        decision.update(collectionName='alpha', sourceRoot=str(self.alpha))
        with patch.object(discovery, 'observe_grants', side_effect=AssertionError('v1')):
            self.assertEqual(self.run_worker(decision)['state'], 'indexed')
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if 'wiki-discovery' in p.name), [])


if __name__ == '__main__':
    # Explicit suite: V2WorkerChecks is a module global via runpy and must not
    # be collected here; the Node wrapper counts only DiscoveryChecks.
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiscoveryChecks)
    sys.exit(not unittest.TextTestRunner().run(suite).wasSuccessful())
