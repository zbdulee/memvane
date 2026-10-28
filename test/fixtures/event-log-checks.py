"""Install event log (core/event_log.py): schema, writers, privacy, rotation,
refusal counting, latency and the read-only logs skill.

Every state root is a real (symlink-free) 0700 temporary directory; nothing is
written under HOME or a project.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
import event_log  # noqa: E402

SCHEMA_KEYS = {'ts', 'v', 'install', 'host', 'session', 'event', 'outcome', 'reason', 'ms'}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='memvane-events-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.state = self.base / 'state'
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.base / 'project'),
                         'journalPath': str(self.state / 'install-journal.sqlite'),
                         'installId': 'install-1', 'collections': [{'name': 'wiki'}]}
        event_log._DROPPED = 0
        os.environ.pop(event_log.PROMPT_ENV, None)

    def events(self, name=None):
        rows = event_log.read(self.state)
        return [row for row in rows if name is None or row['event'] == name]

    def assertSchema(self, row):
        self.assertTrue(SCHEMA_KEYS <= set(row), row)
        self.assertEqual(row['v'], 1)
        self.assertRegex(row['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')


class WriterChecks(Base):
    def hook(self, action, *, raises=None, payload=None):
        import laya_host_response as response

        def once(*_args):
            if raises is not None:
                raise raises
            response._trace_final('sync_requested') if action == 'update' else None
            return {}
        with patch.object(response, '_serve_selected_once', side_effect=once):
            try:
                response.serve_selected(action, 'claude', self.decision,
                                        payload or {'session_id': 'sess-1', 'cwd': '/x'})
            except Exception:
                pass

    def test_hook_actions_record_outcome_and_reason(self):
        import laya_host_response as response
        for action in ('gate', 'posttool', 'update', 'update-queued', 'index', 'compile',
                       'file-change-event'):
            self.hook(action)
        self.hook('gate', raises=response.HostResponseError('selected_proof_changed'))
        self.hook('index', raises=RuntimeError('/secret/path leaked'))
        rows = self.events()
        self.assertEqual([row['event'] for row in rows],
                         ['gate', 'posttool', 'update', 'update-queued', 'index', 'compile',
                          'file-change-event', 'gate', 'index'])
        for row in rows:
            self.assertSchema(row)
            self.assertEqual((row['install'], row['host'], row['session']),
                             ('install-1', 'claude', 'sess-1'))
            self.assertIsInstance(row['ms'], int)
        self.assertEqual(rows[2]['reason'], 'sync_requested')
        self.assertEqual([row['outcome'] for row in rows[-2:]], ['refused', 'refused'])
        self.assertEqual(rows[-2]['reason'], 'selected_proof_changed')
        self.assertEqual(rows[-1]['reason'], 'selected_proof_or_query_failed')
        self.assertNotIn('/secret/path', (self.state / event_log.NAME).read_text())

    def test_recall_event_carries_cards_notes_prompt_hash_and_latency(self):
        import laya_host_response as response
        rows = [{'path': 'wiki/a.md', 'hash': 'a' * 64}, {'path': 'wiki/b.md', 'hash': 'b' * 64}]
        prompt = 'why is the cobalt cache stale?'

        def once(_action, _host, decision, _payload):
            response._write_selection_log(None, 'laya_vector_selected_provider_preparing', [])
            response._log_selection(decision, rows, 'laya_selector_not_prepared',
                                    'selected_provider_preparing')
            return {'hookSpecificOutput': {'additionalContext': 'x'}}
        with patch.object(response, '_serve_selected_once', side_effect=once):
            response.serve_selected('recall', 'codex', self.decision,
                                    {'prompt': prompt, 'session_id': 's2'})
        with patch.object(response, '_serve_selected_once',
                          side_effect=response.HostResponseError('selected_proof_rolled_back')):
            with self.assertRaises(response.HostResponseError):
                response.serve_selected('recall', 'codex', self.decision, {'prompt': prompt})
        served, refused = self.events('recall')
        self.assertSchema(served)
        self.assertEqual((served['outcome'], served['reason']), ('served', 'selected'))
        self.assertEqual(served['notes'], ['laya_vector_selected_provider_preparing'])
        self.assertEqual(served['count'], 2)
        self.assertEqual(served['cards'], [{'path': 'wiki/a.md', 'sha': 'a' * 64},
                                           {'path': 'wiki/b.md', 'sha': 'b' * 64}])
        self.assertEqual(served['selection_fallback'], 'laya_selector_not_prepared')
        self.assertEqual(served['vector_fallback'], 'selected_provider_preparing')
        self.assertEqual(served['prompt'], {'sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                                            'chars': len(prompt)})
        self.assertEqual((refused['outcome'], refused['reason']),
                         ('refused', 'selected_proof_rolled_back'))
        self.assertEqual(refused['count'], 0)
        self.assertNotIn('cobalt', (self.state / event_log.NAME).read_text())

    def test_retry_and_worktree_recalls_write_one_line(self):
        import laya_host_response as response
        calls = []

        def once(_action, _host, decision, _payload):
            calls.append(1)
            if len(calls) == 1:
                raise response.HostResponseError('selected_proof_changed')
            response._log_selection(decision, [], None, None)
            return {}
        with patch.object(response, '_serve_selected_once', side_effect=once), \
             patch.object(response, '_recall_retry_decision', return_value=self.decision):
            response.serve_selected('recall', 'claude', self.decision, {'prompt': 'q'})
        (row,) = self.events('recall')
        self.assertEqual((row['outcome'], row['reason']), ('empty', 'no_results_after_filter'))
        self.assertEqual(row['notes'], ['laya_recall_proof_retry'])
        self.assertEqual(row['retry'], 'selected_proof_changed')
        worktree = {'state': 'worktree', 'mainRoute': self.decision}
        with patch.object(response, '_serve_selected_once',
                          side_effect=lambda *a: response._trace_fields(degraded='worktree') or {}):
            response.serve_selected('recall', 'claude', worktree, {'prompt': 'q'})
        last = self.events('recall')[-1]
        self.assertEqual((last['install'], last['degraded']), ('install-1', 'worktree'))

    def test_main_refusal_after_route_change_is_recorded_once(self):
        import laya_host_response as response
        payload = json.dumps({'cwd': '/x', 'prompt': 'q', 'session_id': 's3'}).encode()
        stdin = SimpleNamespace(buffer=SimpleNamespace(read=lambda *_: payload))
        with patch.object(response, 'select_hook_payload', return_value={'state': 'candidate'}), \
             patch.object(response, 'select_route', return_value=self.decision), \
             patch('sys.stdin', stdin), patch('sys.stderr'):
            self.assertEqual(response.main(['serve-stdin', 'recall', 'claude', '/x']), 3)
        (row,) = self.events('recall')
        self.assertEqual((row['outcome'], row['reason'], row['session']),
                         ('refused', 'selected_route_changed', 's3'))

    def test_wiki_card_writes_and_retirement(self):
        import wiki_activity_log
        import wiki_source_retire
        bound = (self.decision, {})
        wiki_activity_log.append('/p', 'created', 'concepts/a.md', 'A', bound, reason='compile')
        wiki_activity_log.append('/p', 'deleted', 'concepts/b.md', 'B', bound, reason='dedup')
        wiki = self.base / 'wiki'
        wiki_source_retire._record_retired(self.base, None, bound, wiki / 'concepts' / 'c.md',
                                           '---\ntitle: Cee\n---\nbody text\n',
                                           'lifecycle:no_source_revisions', wiki_root=wiki)
        rows = self.events('wiki')
        self.assertEqual([(row['outcome'], row['reason'], row['path']) for row in rows],
                         [('created', 'compile', 'concepts/a.md'),
                          ('deleted', 'dedup', 'concepts/b.md'),
                          ('retired', 'lifecycle:no_source_revisions', 'concepts/c.md')])
        self.assertEqual(rows[2]['title'], 'Cee')
        self.assertNotIn('body text', (self.state / event_log.NAME).read_text())

    def test_wake_and_index_publish(self):
        import laya_runtime_dispatch as dispatch
        from runtime_wake_policy import DispatchFailure
        target = {'terminal': False, 'journalPath': self.decision['journalPath'],
                  'installId': 'install-1', 'generation': 'g1', 'selectionEpoch': 4,
                  'sourceCommit': 'c' * 40}
        with patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target',
                          return_value={'state': 'indexed', 'generation': 'g2'}):
            dispatch.run('laya-sync', '/p')
        with patch.object(dispatch, 'resolve', return_value=target), \
             patch.object(dispatch, '_run_target',
                          side_effect=DispatchFailure('target_child_failed', detail='exit 1\nBODY')):
            with self.assertRaises(DispatchFailure):
                dispatch.run('laya-compile', '/p')
        sync, publish, compile_ = self.events()
        self.assertEqual((sync['event'], sync['outcome'], sync['generation'],
                          sync['previousGeneration'], sync['host']),
                         ('wake.sync', 'indexed', 'g2', 'g1', 'worker'))
        self.assertEqual((publish['event'], publish['generation'], publish['epoch']),
                         ('index.publish', 'g2', 4))
        self.assertEqual((compile_['event'], compile_['outcome'], compile_['reason']),
                         ('wake.compile', 'failed', 'target_child_failed'))
        self.assertNotIn('BODY', (self.state / event_log.NAME).read_text())

    def test_resident_service_drain_forced_exit_and_adopt(self):
        import laya_resident_service as service
        targets = [(self.state, 'install-1')]
        fake = SimpleNamespace(draining=threading.Event(), bundle={'version': '0.41.6'},
                               lock=threading.RLock(), bindings={
                                   '/p': SimpleNamespace(events=targets[0], closed=False)},
                               _close_listener=lambda _b: None,
                               _finish_drain=lambda: None, root=self.base,
                               control_inode=None, exit=lambda code: None)
        fake._event_targets = lambda: service.Service._event_targets(fake)
        service.Service.begin_drain(fake)
        with patch.object(service, '_kill_children', return_value=([11], [])):
            service.Service._force_exit(fake, 12.34)
        service._event(targets, 'resident.adopt', 'adopted', mode='full')
        drain, forced, adopt = self.events()
        self.assertEqual((drain['event'], drain['outcome'], drain['bundle']),
                         ('resident.drain', 'started', '0.41.6'))
        self.assertEqual((forced['event'], forced['reason'], forced['killedChildren']),
                         ('resident.forced_exit', 'cold_load_hung', [11]))
        self.assertEqual((adopt['event'], adopt['mode'], adopt['install']),
                         ('resident.adopt', 'full', 'install-1'))

    def test_learning_capture_status(self):
        from context_learning import laya_learning
        status = laya_learning.capture_recall('claude', self.decision, {}, {}, [], [])
        (row,) = self.events('learning.capture')
        self.assertEqual((row['outcome'], row['host']), (status, 'claude'))


class PrivacyChecks(Base):
    def test_prompt_is_hashed_by_default_and_raw_only_with_flag(self):
        event_log.emit('recall', state_root=self.state, prompt=event_log.prompt_fields('raw words'))
        with patch.dict(os.environ, {event_log.PROMPT_ENV: '1'}):
            event_log.emit('recall', state_root=self.state,
                           prompt=event_log.prompt_fields('raw words'))
        hashed, raw = self.events()
        self.assertEqual(set(hashed['prompt']), {'sha256', 'chars'})
        self.assertEqual(raw['prompt']['text'], 'raw words')
        lines = (self.state / event_log.NAME).read_text().splitlines()
        self.assertNotIn('raw words', lines[0])

    def test_secret_named_fields_are_dropped(self):
        event_log.emit('x', state_root=self.state, api_token='t0k', password='pw',
                       nested={'Authorization': 'Bearer z', 'ok': 1})
        text = (self.state / event_log.NAME).read_text()
        for value in ('t0k', 'pw', 'Bearer'):
            self.assertNotIn(value, text)
        self.assertEqual(self.events()[0]['nested'], {'ok': 1})


class StorageChecks(Base):
    def test_rotation_keeps_two_generations(self):
        with patch.object(event_log, 'LOG_LIMIT', 2000):
            for index in range(200):
                event_log.emit('recall', state_root=self.state, outcome='served', n=index)
        names = sorted(path.name for path in self.state.iterdir())
        self.assertEqual(names, ['events.jsonl', 'events.jsonl.1', 'events.jsonl.2'])
        for name in names:
            self.assertLessEqual((self.state / name).stat().st_size, 2000)
            self.assertEqual(oct((self.state / name).stat().st_mode & 0o777), '0o600')
        numbers = [row['n'] for row in event_log.read(self.state)]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[-1], 199)

    def test_line_is_bounded(self):
        cards = [{'path': 'p' * 200, 'sha': 'f' * 64} for _ in range(16)]
        event_log.emit('recall', state_root=self.state, cards=cards, notes=['n' * 200] * 16)
        line = (self.state / event_log.NAME).read_bytes()
        self.assertLessEqual(len(line), event_log.MAX_LINE)
        self.assertTrue(json.loads(line)['truncated'])

    def test_refusals_are_non_fatal_and_counted(self):
        other = self.base / 'other.txt'
        other.write_text('keep')
        os.symlink(other, self.state / event_log.NAME)
        self.assertFalse(event_log.emit('gate', state_root=self.state))
        self.assertFalse(event_log.emit('gate', state_root=self.state))
        self.assertEqual(other.read_text(), 'keep')
        info = event_log.inspect(self.state)
        self.assertEqual((info['writable'], info['refused']), (False, 2))
        self.assertEqual(info['lastRefusal']['reason'], 'events_not_regular')
        # A state root with group/other bits is refused before any write.
        wide = self.base / 'wide'
        wide.mkdir()
        os.chmod(wide, 0o755)
        self.assertFalse(event_log.emit('gate', state_root=wide))
        self.assertEqual(list(wide.iterdir()), [])
        self.assertEqual(event_log.inspect(wide)['reason'], 'state_root_unsafe')
        # The process carries its unwritten count onto its next written line.
        (self.state / event_log.NAME).unlink()
        self.assertTrue(event_log.emit('gate', state_root=self.state))
        self.assertEqual(self.events()[0]['dropped'], 3)
        self.assertTrue(event_log.emit('gate', state_root=self.state))
        self.assertNotIn('dropped', self.events()[1])

    def test_emit_never_raises(self):
        with patch.object(event_log, '_write', side_effect=RuntimeError('boom')):
            self.assertFalse(event_log.emit('gate', state_root=self.state))
        self.assertFalse(event_log.emit('gate', state_root='relative/path'))
        self.assertFalse(event_log.emit('gate'))  # nothing bound: no state root

    def test_latency_smoke(self):
        samples = []
        for index in range(300):
            started = time.perf_counter()
            event_log.emit('recall', state_root=self.state, outcome='served',
                           cards=[{'path': f'wiki/{index}.md', 'sha': 'a' * 64}] * 3,
                           prompt=event_log.prompt_fields('q' * 400))
            samples.append(time.perf_counter() - started)
        samples.sort()
        # Generous bound for a loaded CI host; the typical cost is well under 1 ms.
        self.assertLess(samples[int(len(samples) * .95)], 0.010, samples[-5:])


class SkillChecks(Base):
    def write_fixture(self):
        rows = [
            {'event': 'recall', 'outcome': 'served', 'reason': 'selected', 'count': 1,
             'cards': [{'path': 'wiki/a.md', 'sha': 'a' * 64}], 'session': 's1'},
            {'event': 'recall', 'outcome': 'empty', 'reason': 'no_candidates',
             'notes': ['laya_vector_selected_provider_preparing'],
             'vector_fallback': 'selected_provider_preparing', 'count': 0},
            {'event': 'recall', 'outcome': 'served', 'reason': 'selected', 'count': 1,
             'degraded': 'bundle_drift', 'cards': [{'path': 'wiki/b.md', 'sha': 'b' * 64}]},
            {'event': 'bundle.upgrade', 'outcome': 'started', 'from': '0.41.5', 'to': '0.41.6'},
            {'event': 'bundle.upgrade', 'outcome': 'refused', 'reason': 'older_host_bundle',
             'from': '0.41.5', 'to': '0.41.6'},
            {'event': 'index.publish', 'outcome': 'published', 'generation': 'g9'},
            {'event': 'gate', 'outcome': 'refused', 'reason': 'selected_proof_changed'},
        ]
        for row in rows:
            fields = {key: value for key, value in row.items()
                      if key not in ('event', 'outcome', 'reason')}
            self.assertTrue(event_log.emit(row['event'], outcome=row['outcome'],
                                           reason=row.get('reason'), state_root=self.state,
                                           install='install-1', host='claude', **fields))

    def run_skill(self, *args):
        before = sorted((path.name, path.lstat().st_mtime_ns) for path in self.state.iterdir())
        done = subprocess.run([sys.executable, '-B', str(REPO / 'skills/logs/scripts/logs.py'),
                               '--state-root', str(self.state), *args],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        after = sorted((path.name, path.lstat().st_mtime_ns) for path in self.state.iterdir())
        self.assertEqual(before, after)  # read-only
        return done.stdout

    def test_views_on_fixture_log(self):
        self.write_fixture()
        summary = self.run_skill()
        self.assertIn('recalls: 3 served=2 empty/refused=1 (no_candidates 1)', summary)
        self.assertIn('degraded recalls: selected_provider_preparing 1, bundle_drift 1', summary)
        self.assertRegex(summary, r'last upgrade: .* bundle\.upgrade refused '
                                  r'reason=older_host_bundle 0\.41\.5 -> 0\.41\.6')
        self.assertIn('last publish:', summary)
        self.assertIn('generation=g9', summary)
        self.assertRegex(summary, r'last error: .* gate refused reason=selected_proof_changed')
        recalls = self.run_skill('recalls')
        self.assertIn('notes=laya_vector_selected_provider_preparing', recalls)
        self.assertIn('  - wiki/a.md aaaaaaaaaaaa', recalls)
        self.assertIn('degraded=bundle_drift', recalls)
        errors = self.run_skill('errors', '--hours', '1')
        self.assertEqual(len(errors.strip().splitlines()), 4)  # 2 header lines + 2 rows
        upgrades = self.run_skill('upgrades', '--json')
        self.assertEqual([json.loads(line)['outcome'] for line in upgrades.splitlines()],
                         ['started', 'refused'])

    def test_unsafe_or_refused_log_is_reported(self):
        os.symlink(self.base / 'nowhere', self.state / event_log.NAME)
        event_log.emit('gate', state_root=self.state)
        out = self.run_skill()
        self.assertIn('WARNING: events cannot be written: events_not_regular', out)
        self.assertIn('WARNING: 1 event write(s) refused', out)

    def test_unselected_project_exits_1(self):
        project = self.base / 'plain'
        project.mkdir()
        done = subprocess.run([sys.executable, '-B', str(REPO / 'skills/logs/scripts/logs.py'),
                               '--cwd', str(project)], capture_output=True, text=True,
                              timeout=30, env={**os.environ, 'HOME': str(self.base)})
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('not onboarded', done.stderr)


class RetiredLogChecks(unittest.TestCase):
    def test_qmd_recall_log_is_not_read(self):
        hits = []
        for directory in ('core', 'hooks', 'hermes_adapter', 'skills', 'scripts'):
            for path in (REPO / directory).rglob('*'):
                if path.is_file() and path.suffix in ('.py', '.mjs', '.sh', '') and \
                        'QMD_RECALL_LOG' in path.read_text(errors='ignore'):
                    hits.append(str(path.relative_to(REPO)))
        self.assertEqual(hits, [])

    def test_folded_logs_have_no_writer(self):
        for name in ('wiki-activity.log', 'bundle-auto-upgrade.log', 'extractor.log'):
            hits = [str(path.relative_to(REPO)) for path in (REPO / 'core').rglob('*.py')
                    if name in path.read_text(errors='ignore')]
            self.assertEqual(hits, [], name)


if __name__ == '__main__':
    unittest.main()
