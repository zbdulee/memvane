"""U6 shadow migration driver after a selected sync pass; synthetic install only.

The sync itself is the ``_run_sync`` seam (its own fixtures cover it); the
driver, candidate store, intent and v6 reader are actual.
"""
from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from laya_index_a import IndexContractError  # noqa: E402
_spec = importlib.util.spec_from_file_location('sqlite_hot_journal', HERE / 'sqlite_hot_journal.py')
hot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hot)

# A worker process killed inside the driver, right before an intent write.
CHILD = r'''
import importlib.util, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
os.environ['HOME'] = payload['home']
os.environ['MEMVANE_TEST_RUNTIME_ROOT'] = payload['runtime']
signal.alarm(90)
spec = importlib.util.spec_from_file_location('driver_crash_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import laya_bridge_readiness as bridge, laya_journal_reader as reader, laya_ledger_common as ledger
import laya_project_dir_move as mover, laya_reader_lease as lease, laya_product_sync_worker as worker
helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
original = ledger._write
def kill_on_intent(path, value, *args, **kwargs):
    if Path(path).name == 'shadow-migration-intent.json':
        print('driver-before-intent', flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    return original(path, value, *args, **kwargs)
with helper.full_authority(case, reader, bridge, lease, mover), \
        mock.patch.object(worker, '_writer_executing_root', return_value=case.plugin), \
        mock.patch.object(worker, '_run_sync', return_value={'state': 'unchanged'}), \
        mock.patch.object(ledger, '_write', side_effect=kill_on_intent):
    worker.run(Path(payload['project']))
raise SystemExit('driver crash checkpoint not reached')
'''


class DriverChecks(unittest.TestCase):
    def setUp(self):
        self.W = importlib.import_module('laya_product_sync_worker')
        self.M = importlib.import_module('laya_shadow_migration')

    def seeded(self):
        spec = importlib.util.spec_from_file_location(
            'shadow_driver_seed', HERE / 'laya-normal-writer-connections-checks.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        normal = module.NormalWriterConnectionChecks('runTest')
        normal.setUp()
        self.addCleanup(normal.doCleanups)
        case = normal.case
        git = ['git', '-C', str(case.project), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(git + ['commit', '--allow-empty', '-qm', 'fixture'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Let the fresh Git metadata leave its timestamp tick.
        return normal.seed, case

    @contextmanager
    def authority(self, seed, case, sync=None):
        with ExitStack() as stack:
            stack.enter_context(seed.authority())
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=case.plugin))
            stack.enter_context(mock.patch.object(self.W, '_run_sync',
                                                  return_value=sync or {'state': 'unchanged'}))
            yield

    def files(self, case):
        return {name: (case.state / name).read_bytes() if os.path.lexists(case.state / name) else None
                for name in (self.W.SHADOW_ACTIVATION, self.W.SHADOW_CANDIDATE, self.M._INTENT)}

    def events(self, case):
        path = case.state / 'events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]

    def intent(self, case):
        path = case.state / self.M._INTENT
        return json.loads(path.read_text(encoding='utf8')) if path.exists() else None

    def receipt(self, case):
        return self.M.shadow_parity_receipt(case.state, candidate=case.state / self.W.SHADOW_CANDIDATE,
                                            candidate_incarnation=self.incarnation(case))

    def incarnation(self, case):
        import sqlite3
        from contextlib import closing
        uri = (case.state / self.W.SHADOW_CANDIDATE).as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            return db.execute('SELECT incarnation FROM store_meta').fetchone()[0]

    def crash(self, case):
        payload = case.home / 'shadow-driver-crash.json'
        payload.write_text(json.dumps({'home': str(case.home), 'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                                       'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project)}), encoding='utf8')
        killed = subprocess.run([sys.executable, '-B', '-c', CHILD, str(ROOT), str(payload)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
        self.assertIn('driver-before-intent', killed.stdout)

    def test_sync_passes_create_candidate_and_reach_shadow_verified(self):
        seed, case = self.seeded()
        W = self.W
        # Until U15 provides advance_reader_ready the driver stops at shadow_verified.
        absent = mock.patch.object(self.M, 'advance_reader_ready', None, create=True)
        absent.start()
        self.addCleanup(absent.stop)
        preferred = seed.selection.preferred_endpoint
        self.assertEqual(self.files(case), dict.fromkeys(self.files(case)))
        per_pass, sync_result = [], {'state': 'indexed', 'generation': 'synthetic'}
        with self.authority(seed, case, sync=sync_result):
            for _ in range(12):
                before = len(self.events(case))
                self.assertIs(W.run(case.project), sync_result)
                added = self.events(case)[before:]
                # The driver adds only migration events to a sync pass.
                self.assertEqual({e['event'] for e in added} - {'migration.shadow', 'migration.driver'}, set())
                driver = [e for e in added if e['event'] == 'migration.driver']
                self.assertEqual(len(driver), 1, added)
                attempts = {e['attempt'] for e in added if e['event'] == 'migration.shadow'}
                per_pass.append((driver[0]['steps'], len(attempts), driver[0].get('migration_state'),
                                 driver[0].get('reason')))
                # Measurement only (contract 9/10): the driver's own wall time after the sync.
                print('pass_ms', driver[0]['steps'], driver[0]['ms'], flush=True)
                if (self.intent(case) or {}).get('migration_state') == 'shadow_verified':
                    break
            print('per_pass', per_pass, flush=True)
            self.assertEqual(self.intent(case)['migration_state'], 'shadow_verified', per_pass)
            # More than one pass: the prepare waits for a pass of its own.
            self.assertGreater(len(per_pass), 1)
            for steps, attempts, _state, _reason in per_pass:
                self.assertLessEqual(steps, W.SHADOW_STEPS_PER_PASS)
                # One step may trace several sub-stages; every pass ran some.
                self.assertGreater(attempts, 0)
            self.assertEqual([p[3] for p in per_pass].count('prepare_next_pass'), 1, per_pass)
            self.assertEqual(per_pass[0][3], 'prepare_next_pass', per_pass)
            proved = self.receipt(case)
            self.assertIsNotNone(proved)
            activation = json.loads((case.state / W.SHADOW_ACTIVATION).read_text(encoding='utf8'))
            self.assertEqual((activation['project'], activation['candidate']),
                             (str(case.project), str(case.state / W.SHADOW_CANDIDATE)))

            # Verified: each pass re-proves the head in one step and stops
            # there until the reader_ready entry (U15) exists.
            verified = self.files(case)[self.M._INTENT]
            before = len(self.events(case))
            with mock.patch.object(self.M, 'advance_reader_ready', None, create=True):
                self.assertIs(W.run(case.project), sync_result)
            driver = [e for e in self.events(case)[before:] if e['event'] == 'migration.driver']
            self.assertEqual([(e['steps'], e['migration_state']) for e in driver], [(1, 'shadow_verified')])
            self.assertEqual(self.files(case)[self.M._INTENT], verified)
            self.assertEqual(self.receipt(case), proved)
            calls = []
            def reader_ready(store, *, project, executing_root, deadline):
                calls.append((Path(store._path), project, executing_root))
                return {'stage': 'reader_ready', 'migration_state': 'shadow_verified'}
            with mock.patch.object(self.M, 'advance_reader_ready', side_effect=reader_ready, create=True):
                self.assertIs(W.run(case.project), sync_result)
            self.assertEqual(calls, [(case.state / W.SHADOW_CANDIDATE, case.project, case.plugin)])
            # The v6 reader stays selected throughout.
            self.assertEqual(seed.selection.preferred_endpoint, preferred)

            # A sync that did not complete never runs the driver.
            for state in ('deferred', 'pending', 'blocked'):
                with mock.patch.object(W, '_run_sync', return_value={'state': state}), \
                        mock.patch.object(W, 'shadow_pass', side_effect=AssertionError('driver ran')):
                    self.assertEqual(W.run(case.project), {'state': state})

    def test_guards_and_kill_switch_never_start_or_advance(self):
        seed, case = self.seeded()
        W = self.W
        import laya_fresh_wiki_origin as fresh
        import laya_install_journal as journal
        import laya_ledger_common as ledger
        linked = case.home / 'linked-worktree'
        subprocess.run(['git', '-C', str(case.project), 'worktree', 'add', '-q', '-b', 'linked', str(linked)],
                       check=True, capture_output=True, timeout=10)
        linked = linked.resolve()
        qmd = case.state / ledger.QMD_LEDGER
        split = case.project / '.memvane'

        @contextmanager
        def touched(path, directory=False):
            path.mkdir() if directory else path.write_text('{}', encoding='utf8')
            try:
                yield
            finally:
                path.rmdir() if directory else path.unlink()

        guards = [
            ('disabled', case.project, lambda: mock.patch.dict(os.environ, {W.SHADOW_KILL_SWITCH: 'off'})),
            ('route_not_selected', linked, ExitStack),
            ('project_dir_split', case.project, lambda: touched(split, directory=True)),
            ('qmd_origin_detach_required', case.project, lambda: touched(qmd)),
            ('upgrade_fenced', case.project, lambda: mock.patch.object(fresh, 'upgrade_fenced', return_value=True)),
            ('bundle_drift', case.project, lambda: mock.patch.object(
                journal, 'verify_selected_route',
                side_effect=journal.JournalEvidenceError('installed_bundle_changed'))),
        ]

        def check(expected):
            for reason, project, guard in guards:
                before_events = len(self.events(case))
                with guard():
                    status = W.shadow_pass(project)
                    if project == case.project:
                        self.assertEqual(W.run(project), {'state': 'unchanged'})
                self.assertEqual((status['state'], status['reason'], status['steps']), ('skipped', reason, 0))
                self.assertEqual(self.files(case), expected, reason)
                self.assertEqual(self.events(case)[before_events:], [], reason)

        with self.authority(seed, case):
            # Before any activation: nothing is created.
            check(dict.fromkeys(self.files(case)))
            # Started: nothing advances.
            started = W.shadow_pass(case.project)
            # The pass stops where the lock-free prepare comes due (its own pass).
            self.assertEqual((started['state'], started['reason']), ('progressed', 'prepare_next_pass'))
            self.assertLess(started['steps'], W.SHADOW_STEPS_PER_PASS)
            self.assertIsNotNone(self.intent(case))
            check(self.files(case))
            # An unknown kill switch value does not disable the driver.
            with mock.patch.dict(os.environ, {W.SHADOW_KILL_SWITCH: 'on'}):
                status = W.shadow_pass(case.project)
                # It ran steps; how the pass ended depends on the time budget.
                print('kill_switch_on_pass', status['state'], status['reason'], status['steps'], flush=True)
                self.assertIn(status['state'], ('progressed', 'waiting'), status)
                self.assertGreater(status['steps'], 0, status)
                self.assertNotIn(status['reason'], ('disabled',), status)

    def test_worker_sigkill_resumes_from_intent(self):
        seed, case = self.seeded()
        W = self.W
        # Killed before the begin intent: the candidate exists without one.
        self.crash(case)
        files = self.files(case)
        self.assertIsNotNone(files[W.SHADOW_ACTIVATION])
        self.assertIsNotNone(files[W.SHADOW_CANDIDATE])
        self.assertIsNone(files[self.M._INTENT])
        with self.authority(seed, case):
            resumed = W.shadow_pass(case.project)
            self.assertEqual(resumed['stages'][0], 'begin', resumed)
            self.assertEqual(resumed['state'], 'progressed')
        copying = self.files(case)[self.M._INTENT]
        self.assertEqual(json.loads(copying)['migration_state'], 'copying')
        # Killed inside a later step, right before its intent write.
        self.crash(case)
        self.assertEqual(self.files(case)[self.M._INTENT], copying)
        with self.authority(seed, case):
            for _ in range(10):
                status = W.shadow_pass(case.project)
                if status.get('migration_state') == 'shadow_verified':
                    break
        self.assertEqual(self.intent(case)['migration_state'], 'shadow_verified')
        self.assertEqual(json.loads(copying)['migration_id'], self.intent(case)['migration_id'])
        self.assertIsNotNone(self.receipt(case))

    def test_hot_journal_on_the_candidate_is_rolled_back_under_the_sync_owner(self):
        import sqlite3
        from contextlib import closing
        seed, case = self.seeded()
        W = self.W
        candidate = case.state / W.SHADOW_CANDIDATE
        with self.authority(seed, case):
            first = W.shadow_pass(case.project)
        self.assertIsNotNone(self.intent(case), first)
        _before, journal = hot.kill_mid_write(candidate)
        torn = candidate.read_bytes()

        @contextmanager
        def busy(_queue, **_kwargs):
            yield None
        with self.authority(seed, case):
            # Recovery waits for the queue's sync owner and never takes it busy.
            with mock.patch.object(W, 'sync_owner', busy):
                held = W.shadow_pass(case.project)
            untouched = journal.exists() and candidate.read_bytes() == torn
            resumed = W.shadow_pass(case.project)
        probe = None  # A still-hot journal refuses a ro read.
        if not os.path.lexists(journal):
            with closing(sqlite3.connect(candidate.as_uri() + '?mode=ro', uri=True)) as db:
                probe = db.execute("SELECT count(*) FROM sqlite_master WHERE name='hot_probe'").fetchone()[0]
        print(json.dumps(dict(case='shadow-hot-journal', held=[held['state'], held['reason']],
                              untouched=untouched, resumed=[resumed['state'], resumed['reason'],
                                                            resumed['stages']],
                              journalLeft=os.path.lexists(journal), probeTable=probe)), flush=True)
        self.assertEqual((held['state'], held['reason'], held['steps'], untouched),
                         ('error', 'sync_owner_busy', 0, True), held)
        self.assertNotEqual(resumed['state'], 'error', resumed)
        self.assertGreater(resumed['steps'], 0, resumed)
        self.assertFalse(os.path.lexists(journal))
        self.assertEqual(probe, 0)  # SQLite's rollback removed the killed write.

    def test_schema3_selected_install_is_never_touched(self):
        spec = importlib.util.spec_from_file_location(
            'shadow_driver_schema3_seed', HERE / 'laya-schema3-sync-publish-checks.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seed = module.Schema3SyncPublishChecks('test_04_v6_selection_keeps_the_v6_sync_path')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        seed.select_schema3()
        # A leftover intent from before the switch stays byte-identical.
        leftover = case.state / self.M._INTENT
        leftover.write_bytes(b'{"leftover":true}')
        leftover.chmod(0o600)
        seed.card('concepts/driver-schema3.md', 'driverschemaprobe')
        before = len(self.events(case))
        result, _calls = seed.run_pass()
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertEqual(result['storeFormat'], 'schema3')
        with seed.seed.authority():
            status = self.W.shadow_pass(case.project)
        self.assertEqual((status['state'], status['reason'], status['steps']),
                         ('skipped', 'reader_schema3_selected', 0))
        self.assertFalse(os.path.lexists(case.state / self.W.SHADOW_ACTIVATION))
        self.assertFalse(os.path.lexists(case.state / self.W.SHADOW_CANDIDATE))
        self.assertEqual(leftover.read_bytes(), b'{"leftover":true}')
        self.assertEqual([e for e in self.events(case)[before:] if e['event'].startswith('migration.')], [])

    def logs(self, case, *args):
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/logs/scripts/logs.py'),
                               '--state-root', str(case.state), *args],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_space_precheck_defers_and_logs_summarize_without_source_text(self):
        seed, case = self.seeded()
        W, M = self.W, self.M
        import event_log
        absent = mock.patch.object(M, 'advance_reader_ready', None, create=True)
        absent.start()
        self.addCleanup(absent.stop)
        spec = importlib.util.spec_from_file_location('shadow_driver_logs', ROOT / 'skills/logs/scripts/logs.py')
        logs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(logs)
        # The skill renders progress over the migration's own operation order.
        self.assertEqual(logs.MIGRATION_OPS, M.MIGRATION_OPS)
        real = os.statvfs
        state = case.state.resolve()

        def tight(path):
            # Only the migration's state directory is short of space: 4 blocks.
            found = real(path)
            if Path(path).resolve() != state:
                return found
            return os.statvfs_result((found.f_bsize, 4096, found.f_blocks, 4, 4, found.f_files,
                                      found.f_ffree, found.f_favail, found.f_flag, found.f_namemax))

        def deferred(before):
            return [e for e in self.events(case)[before:] if e['event'] == 'migration.shadow'
                    and e['outcome'] == 'deferred']

        with self.authority(seed, case):
            # Start: the activation record and the empty candidate exist, but
            # no intent is written while the projected copy does not fit.
            for attempt in range(2):
                before = len(self.events(case))
                with mock.patch.object(os, 'statvfs', side_effect=tight):
                    status = W.shadow_pass(case.project)
                self.assertEqual((status['state'], status['reason']), ('waiting', 'migration_space_deferred'))
                self.assertEqual(status['stages'], ['activate', 'create'] if attempt == 0 else [])
                files = self.files(case)
                self.assertIsNotNone(files[W.SHADOW_CANDIDATE])
                self.assertIsNone(files[M._INTENT])
                if attempt:
                    self.assertEqual(files, held)
                held = files
                rows = deferred(before)
                self.assertEqual([(e['reason'], e['op']) for e in rows], [('migration_space_deferred', 'begin')])
                self.assertEqual(rows[0]['free_bytes'], 4 * 4096)
                self.assertGreater(rows[0]['needed_bytes'], rows[0]['free_bytes'])
            # Space returns: the begin step writes the copy intent.
            with mock.patch.object(W, 'SHADOW_STEPS_PER_PASS', 1):
                self.assertEqual(W.shadow_pass(case.project)['stages'], ['begin'])
            copying = self.files(case)
            self.assertEqual(json.loads(copying[M._INTENT])['phase'], 'copying')
            # Additional copy: a bounded retry that leaves intent and candidate as they were.
            for _ in range(2):
                before = len(self.events(case))
                with mock.patch.object(os, 'statvfs', side_effect=tight):
                    status = W.shadow_pass(case.project)
                self.assertEqual((status['state'], status['reason'], status['stages']),
                                 ('waiting', 'step_retry', ['retry']))
                self.assertEqual(self.files(case), copying)
                self.assertEqual([(e['reason'], e['op']) for e in deferred(before)],
                                 [('migration_space_deferred', 'copy')])
            summary = self.logs(case)
            line = [row for row in summary.splitlines() if row.startswith('migration: ')]
            self.assertEqual(len(line), 1, summary)
            self.assertIn('state=copying', line[0])
            self.assertIn('reason=migration_space_deferred', line[0])
            self.assertIn('deferred=4', line[0])
            self.assertIn(f'progress=2/{len(M.MIGRATION_OPS)}', line[0])
            # Space returns again: the same migration finishes, with the
            # legacy prompt debug flag on.
            with mock.patch.dict(os.environ, {event_log.PROMPT_ENV: '1'}):
                for _ in range(12):
                    W.shadow_pass(case.project)
                    if (self.intent(case) or {}).get('migration_state') == 'shadow_verified':
                        break
        self.assertEqual(self.intent(case)['migration_state'], 'shadow_verified')
        self.assertEqual(self.intent(case)['migration_id'], json.loads(copying[M._INTENT])['migration_id'])
        summary = self.logs(case)
        line = [row for row in summary.splitlines() if row.startswith('migration: ')]
        self.assertEqual(len(line), 1, summary)
        self.assertIn('state=shadow_verified', line[0])
        total = len(M.MIGRATION_OPS)
        self.assertIn(f'progress={total}/{total}', line[0])
        shadow = self.logs(case, 'migration', '--limit', '0')
        self.assertIn('op=parity_seal', shadow)
        self.assertIn('shadow_verified', shadow)
        # No card text, recall parity prompt or prompt field reaches the log or the skill.
        probes = set()
        for card in case.project.rglob('*.md'):
            if '.git' in card.parts:
                continue
            body = card.read_text(encoding='utf8')
            rel = card.relative_to(case.project).as_posix()
            probes.update(p for p in M.recall_corpus_prompts(rel, body, None) if len(p) >= 8)
            probes.update(line.strip() for line in body.splitlines() if len(line.strip()) >= 12
                          and not line.startswith(('---', 'title:', 'type:', 'status:', 'createdBy:')))
        self.assertGreater(len(probes), 0)
        raw = b''.join((case.state / name).read_bytes() for name in ('events.jsonl',)
                       if (case.state / name).exists()).decode('utf8')
        rendered = summary + shadow + self.logs(case, 'events', '--json', '--limit', '0')
        self.assertEqual(sorted(p for p in probes if p in raw or p in rendered), [])
        self.assertEqual([row for row in self.events(case) if 'prompt' in row], [])

    def test_install_provider_spec_reaches_the_vector_copy(self):
        # U12e/K5: the driver registers the install provider's view recipe and
        # profile in the candidate and passes that spec to advance_shadow, so
        # the caught-up head copies compatible legacy vectors. The fixture
        # manifest names no recipe or model digest; both are filled in here.
        seed, case = self.seeded()
        W, M = self.W, self.M
        views = importlib.import_module('laya_view_vectors')
        real_specs = views.provider_specs

        def specs(manifest, profile):
            return real_specs(dict(manifest or {}, recipeId=views.PROVIDER_CHUNK_RECIPE,
                                   modelSha256='3' * 64), profile)
        seen, real_advance = [], M.advance_shadow

        def advance(store, **kwargs):
            seen.append(kwargs.get('vector_spec'))
            return real_advance(store, **kwargs)
        stages = []
        with self.authority(seed, case), mock.patch.object(views, 'provider_specs', new=specs), \
                mock.patch.object(M, 'advance_shadow', new=advance), \
                mock.patch.object(M, 'advance_reader_ready', None, create=True):
            expected = views.provider_spec({}, case.route['vectorProfile'])
            for _ in range(16):
                stages.extend(W.shadow_pass(case.project)['stages'])
                if (self.intent(case) or {}).get('migration_state') == 'shadow_verified':
                    break
        intent = self.intent(case)
        self.assertEqual(intent['migration_state'], 'shadow_verified', stages)
        self.assertTrue(seen)
        self.assertEqual(set(map(json.dumps, seen)), {json.dumps(expected)})
        self.assertIn('vectors', stages)
        record = intent['vectors']
        self.assertEqual((record['recipe_id'], record['profile_id'], record['complete']),
                         (expected['recipe_id'], expected['profile_id'], True))
        self.assertEqual(record['copied'] + sum(record['deferred'].values()), record['total'])
        import sqlite3
        from contextlib import closing
        uri = (case.state / W.SHADOW_CANDIDATE).as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            self.assertEqual(db.execute('SELECT recipe_id FROM chunk_recipes').fetchall(),
                             [(expected['recipe_id'],)])
            self.assertEqual(db.execute('SELECT profile_id FROM vector_profiles').fetchall(),
                             [(expected['profile_id'],)])
        print('vector_copy', record['total'], record['copied'], dict(record['deferred']), flush=True)

    @contextmanager
    def queued(self):
        """The fixture's private wake queue, a virtual wall clock and a spy on its offers.

        The fixture runtime root lives in this temp HOME; QMD_SANDBOX is
        cleared only so ``runtime_work.request`` writes that queue, and
        QMD_RUNTIME_DISPATCH keeps it from starting a worker process.
        """
        import runtime_work
        from types import SimpleNamespace
        clock = {'now': time.time()}
        fake = SimpleNamespace(time=lambda: clock['now'], monotonic=time.monotonic, sleep=lambda _s: None)
        offers, real = [], runtime_work.request

        def spy(kind, target='', **kwargs):
            offers.append((kind, target, kwargs, clock['now']))
            return real(kind, target, **kwargs)
        with mock.patch.dict(os.environ, {'QMD_SANDBOX': '', 'QMD_SETUP_GUARD_FIXTURE': '',
                                          'QMD_RUNTIME_DISPATCH': '1'}), \
                mock.patch.object(runtime_work, 'time', fake), \
                mock.patch.object(runtime_work, 'request', side_effect=spy):
            yield runtime_work.WakeQueue(), clock, offers

    def test_queue_rewakes_carry_the_migration_to_shadow_verified(self):
        # U6b: one natural sync wake, then only the wake queue (no edit, no
        # hook): each eligible pass offers the same laya-sync wake once, due
        # one interval later, and the chain ends at shadow_verified.
        seed, case = self.seeded()
        W, M = self.W, self.M
        import runtime_work
        absent = mock.patch.object(M, 'advance_reader_ready', None, create=True)
        absent.start()
        self.addCleanup(absent.stop)
        project = str(case.project)
        passes, drains = [], []
        with self.authority(seed, case), self.queued() as (queue, clock, offers):
            def execute(row):
                self.assertEqual((row['kind'], row['target']), ('laya-sync', project))
                before = len(self.events(case))
                result = W.run(Path(row['target']))
                driver = [e for e in self.events(case)[before:] if e['event'] == 'migration.driver']
                self.assertEqual(len(driver), 1)
                passes.append((clock['now'], driver[0]))
                return result['state'] in ('indexed', 'unchanged')
            self.assertEqual(queue.offer('laya-sync', project), 'queued')
            for _ in range(60):
                drains.append(runtime_work.drain(queue, execute=execute, ready=lambda: True,
                                                 wall=lambda: clock['now'], sleep=lambda _s: None))
                rows = queue.pending()
                if not rows:
                    break
                row, = rows
                # Not due yet: the worker released its lease instead of running it again.
                self.assertGreater(row['due'], clock['now'])
                clock['now'] = row['due']
        trail = [(e['outcome'], e['reason'], e['migration_state'], e.get('rewake')) for _t, e in passes]
        self.assertEqual((self.intent(case) or {}).get('migration_state'), 'shadow_verified', trail)
        self.assertGreater(len(passes), 1, trail)
        self.assertEqual(queue.pending(), [])
        # Every pass but the last (at shadow_verified) offered exactly one rewake.
        self.assertEqual([e.get('rewake') for _t, e in passes], [True] * (len(passes) - 1) + [False], trail)
        self.assertEqual(len(offers), len(passes) - 1)
        self.assertLessEqual(len(offers), len(passes))
        self.assertEqual({(k, t, kw.get('delay'), kw.get('start')) for k, t, kw, _n in offers},
                         {('laya-sync', project, W.SHADOW_REWAKE_SECONDS, False)})
        # One pass per drain: the rewake never ran inside the drain that offered it.
        self.assertEqual([d['runs'] for d in drains], [1] * len(passes))
        times = [t for t, _e in passes]
        self.assertTrue(all(b - a >= W.SHADOW_REWAKE_SECONDS for a, b in zip(times, times[1:])), times)
        print('rewake_passes', len(passes), trail, flush=True)

    def test_rewake_stops_on_waits_error_streak_and_kill_switch(self):
        seed, case = self.seeded()
        W, M = self.W, self.M
        import laya_install_journal as journal
        absent = mock.patch.object(M, 'advance_reader_ready', None, create=True)
        absent.start()
        self.addCleanup(absent.stop)
        real = os.statvfs
        state = case.state.resolve()

        def tight(path):
            found = real(path)
            if Path(path).resolve() != state:
                return found
            return os.statvfs_result((found.f_bsize, 4096, found.f_blocks, 4, 4, found.f_files,
                                      found.f_ffree, found.f_favail, found.f_flag, found.f_namemax))
        with self.authority(seed, case), self.queued() as (queue, _clock, offers):
            # Space held: waiting, no rewake.
            with mock.patch.object(os, 'statvfs', side_effect=tight):
                status = W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason'], status['rewake']),
                             ('waiting', 'migration_space_deferred', False))
            self.assertEqual(offers, [])
            # The kill switch and other guards: skipped, nothing offered or written.
            files = self.files(case)
            for guard in (mock.patch.dict(os.environ, {W.SHADOW_KILL_SWITCH: 'off'}),
                          mock.patch.object(journal, 'verify_selected_route',
                                            side_effect=journal.JournalEvidenceError('installed_bundle_changed'))):
                with guard:
                    self.assertEqual(W.run(case.project), {'state': 'unchanged'})
                    self.assertEqual(W.shadow_pass(case.project)['state'], 'skipped')
            self.assertEqual((offers, self.files(case)), ([], files))
            # A progressing pass offers one rewake.
            status = W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['rewake']), ('progressed', True), status)
            self.assertEqual(len(offers), 1)
            # Consecutive errors rewake until the streak reaches the cap, then
            # only natural wakes run passes; a progressing pass resets it.
            flags = []
            with mock.patch.object(M, 'advance_shadow',
                                   side_effect=IndexContractError('fts_deadline_exceeded')):
                for _ in range(W.SHADOW_REWAKE_ERRORS + 1):
                    status = W.shadow_pass(case.project)
                    self.assertEqual((status['state'], status['reason']), ('error', 'fts_deadline_exceeded'))
                    flags.append(status['rewake'])
            self.assertEqual(flags, [True] * (W.SHADOW_REWAKE_ERRORS - 1) + [False, False])
            self.assertEqual(len(offers), W.SHADOW_REWAKE_ERRORS)
            # One step keeps this pass before shadow_verified (a step-cap pass).
            with mock.patch.object(W, 'SHADOW_STEPS_PER_PASS', 1):
                status = W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['rewake']), ('progressed', True), status)
            # The pass budget ends a migration that can go on: rewake (the
            # first step still runs on the prepare budget).
            with mock.patch.object(W, 'SHADOW_PASS_SECONDS', 0):
                status = W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason'], status['rewake']),
                             ('waiting', 'pass_deadline', True), status)
            self.assertNotEqual(status['migration_state'], 'shadow_verified', status)
            with mock.patch.object(M, 'advance_shadow',
                                   side_effect=IndexContractError('fts_deadline_exceeded')):
                self.assertTrue(W.shadow_pass(case.project)['rewake'])
            # The streak lives in the activation record; its identity fields stay.
            record = json.loads((case.state / W.SHADOW_ACTIVATION).read_text(encoding='utf8'))
            self.assertEqual((record['project'], record['candidate'], record['rewake_errors']),
                             (str(case.project), str(case.state / W.SHADOW_CANDIDATE), 1))
            # A gate wait at shadow_verified offers nothing.
            for _ in range(16):
                W.shadow_pass(case.project)
                if (self.intent(case) or {}).get('migration_state') == 'shadow_verified':
                    break
            self.assertEqual(self.intent(case)['migration_state'], 'shadow_verified')
            before = len(offers)
            gate = {'stage': 'retry', 'reason': 'switch_bridge_missing', 'migration_state': 'shadow_verified'}
            with mock.patch.object(M, 'advance_reader_ready', return_value=gate, create=True):
                status = W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason'], status['rewake']),
                             ('waiting', 'switch_bridge_missing', False), status)
            self.assertEqual(len(offers), before)

    def test_driver_failure_never_changes_the_sync_result(self):
        W = self.W
        for result in ({'state': 'indexed', 'generation': 'g'}, {'state': 'unchanged'}):
            with mock.patch.object(W, '_run_sync', return_value=result), \
                    mock.patch.object(W, 'shadow_pass', side_effect=RuntimeError('boom')) as driver:
                self.assertIs(W.run(Path('/nonexistent-project')), result)
            self.assertEqual(driver.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
