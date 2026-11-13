#!/usr/bin/env python3
"""U17: PR6-7 end-to-end migration, switch, crash, concurrency and rollback scenarios.

Builds on the U16 rollback fixture (itself U15 -> U11 -> U10): a real v5
install, v6 index, journal images, Git checkout, the actual shadow migration
driver (``laya_product_sync_worker.run``/``shadow_pass``), the actual switch
CAS, rollback/resume and main recall (``serve_selected``). Synthetic
authority is the same as there: bundle inspection, host observation (the
recall hooks record their receipts with a synthetic host process), the
upgrade's provider review, and the ``.memvane`` layout of the published
``.auto-context`` fixture project. v6 writes between passes are the fixture's
v6 writer core (``publish_all``); writes after the switch go through the
actual schema3 sync pass (``_run_sync`` -> ``_run_schema3``). Crashes are
SIGKILLs of separate interpreter processes (``subprocess``, never fork).
The fixture provider manifest is unusable, so v6 and schema3 recall both
serve lexical results with the same named vector fallback; the hybrid
schema3 path is covered by the U12e fixture, not here.
"""
from contextlib import ExitStack, closing, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
spec = importlib.util.spec_from_file_location('pr67_rollback_checks', HERE / 'laya-shadow-rollback-checks.py')
rollback_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback_checks)
switch_checks, lifecycle = rollback_checks.switch_checks, rollback_checks.lifecycle
J = rollback_checks.J
HOSTS = ('claude', 'codex', 'hermes')

# One driver pass in a separate interpreter, SIGKILLed at a named checkpoint.
# kill: none | copy | prepare | publish | before:<state> | after:<state> | step:<name>
CHILD = r'''
import importlib.util, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
signal.alarm(150)
spec = importlib.util.spec_from_file_location('pr67_crash_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import laya_bridge_readiness as bridge, laya_install_journal as J, laya_journal_reader as reader
import laya_ledger_common as ledger, laya_project_dir_move as mover, laya_reader_lease as lease
import laya_product_sync_worker as worker, laya_view_store as views, project_dir
helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
project, kill = Path(payload['project']), payload['kill']
real_layout, real_write, real_step = project_dir.layout, ledger._write, J._conversion_step
real_roots, real_publish = views.ViewStore._roots, views.ViewStore.publish_generation
def die(label):
    print('killed-' + label, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
def write(path, value, *args, **kwargs):
    intent = Path(path).name == 'shadow-migration-intent.json' and isinstance(value, dict)
    state = value.get('migration_state') if intent else None
    if intent and kill == 'copy' and Path(path).exists() and value.get('phase') in ('copying', 'copied'):
        die('copy')
    if intent and kill == 'before:' + str(state):
        die(kill)
    result = real_write(path, value, *args, **kwargs)
    if intent and kill == 'after:' + str(state):
        die(kill)
    return result
def step(name):
    if kill == 'step:' + name:
        die(kill)
    if real_step is not None:
        real_step(name)
def roots(*args, **kwargs):
    if kill == 'prepare':
        die('prepare')
    return real_roots(*args, **kwargs)
def publish(store, *args, **kwargs):
    result = real_publish(store, *args, **kwargs)
    if kill == 'publish':
        die('publish')
    return result
def inspect(root):
    return {**case.proof, 'pluginRoot': str(root)}
with helper.full_authority(case, reader, bridge, lease, mover), \
        mock.patch.object(bridge, 'inspect_full_plugin_candidate', side_effect=inspect), \
        mock.patch.object(project_dir, 'layout',
                          side_effect=lambda p: 'current' if Path(p) == project else real_layout(p)), \
        mock.patch.object(worker, '_writer_executing_root', return_value=case.plugin), \
        mock.patch.object(worker, '_run_sync', return_value={'state': 'unchanged'}), \
        mock.patch.object(ledger, '_write', side_effect=write), \
        mock.patch.object(J, '_conversion_step', side_effect=step), \
        mock.patch.object(views.ViewStore, '_roots', side_effect=roots), \
        mock.patch.object(views.ViewStore, 'publish_generation', new=publish):
    print(json.dumps(worker.shadow_pass(project)), flush=True)
raise SystemExit(3)
'''

# One main recall in a separate interpreter; prints the generations it served.
RECALL = r'''
import importlib.util, io, json, sys
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
spec = importlib.util.spec_from_file_location('pr67_recall_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import laya_bridge_readiness as bridge, laya_index_host_route as route, laya_journal_reader as reader
import laya_project_dir_move as mover, laya_reader_lease as lease, laya_host_response as host
helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
project, gens, served = payload['project'], payload['gens'], []
for _ in range(payload['limit']):
    err = io.StringIO()
    with helper.full_authority(case, reader, bridge, lease, mover), redirect_stderr(err), \
            mock.patch.object(host, '__file__', str(case.plugin / 'core' / 'laya_host_response.py')):
        decision = route.select_route(project, action='recall')
        answer = host.serve_selected('recall', payload['host'], decision,
                                     {'cwd': project, 'prompt': 'heron route northern marsh',
                                      'session_id': 'pr67-parallel', 'hook_event_name': 'UserPromptSubmit'})
    context = answer['context'] if payload['host'] == 'hermes' else answer['hookSpecificOutput']['additionalContext']
    found = [name for name, gen in sorted(gens.items()) if 'generation ' + gen in context]
    served.append({'found': found, 'refused': 'request refused' in err.getvalue(), 'state': decision['state']})
    if found == ['new']:
        break
print(json.dumps(served), flush=True)
'''

# Read-only open of a store another interpreter wrote (Apple sqlite / CPython sqlite).
CROSS = r'''
import json, sys, time
from pathlib import Path
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
import laya_view_store as views
store = views.ViewStore.open_reader(Path(payload['store']), repository=payload['repository'],
                                   scope_id=payload['scope'], deadline=time.monotonic() + 20)
try:
    pin = store.pin_view(store.capture_view(payload['worktree']))
    rows = [row['path'] for row in store.search(pin, payload['query'], 10)]
    print(json.dumps({'version': list(sys.version_info[:2]), 'gen': pin.gen_id, 'rows': rows}), flush=True)
finally:
    store.close()
'''


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


def other_python():
    """An interpreter of the other sqlite build: Apple /usr/bin/python3 (3.9) or a 3.13."""
    if sys.version_info[:2] != (3, 9):
        return '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else None
    for name in ('python3.13', 'python3.12', 'python3.11'):
        found = shutil.which(name)
        if found:
            return found
    return None


TEMPLATE = []


def card(title, body):
    """A trusted card: the fixture otter card's frontmatter (its sourceRevisions) with a new title and body."""
    text = TEMPLATE[0]
    head, _sep, _rest = text[4:].partition('\n---\n')
    lines = ['title: "%s"' % title if line.startswith('title:') else line for line in head.splitlines()]
    return '---\n' + '\n'.join(lines) + '\n---\n\n## Summary\n%s\n' % body


class Pr67IntegrationChecks(unittest.TestCase):
    def setUp(self):
        self.u16 = rollback_checks.ShadowRollbackChecks('runTest')
        self.u16.setUp()
        self.addCleanup(self.u16.doCleanups)
        self.u15, self.u11, self.u10, self.case = self.u16.u15, self.u16.u11, self.u16.u10, self.u16.case
        self.W, self.M, self.reader = self.u15.W, self.u15.M, self.u15.reader
        self.wiki = self.case.auto / 'wiki'
        TEMPLATE[:] = [(self.wiki / 'concepts/otter-ledger.md').read_text(encoding='utf8')]
        self.assertIn('sourceRevisions:', TEMPLATE[0])

    # ---- helpers -----------------------------------------------------------
    def cards(self, wiki=None):
        """Card paths under the wiki root (``.md`` files below a type directory)."""
        wiki = wiki or self.wiki
        return sorted(p.relative_to(wiki).as_posix() for p in wiki.rglob('*.md') if p.parent != wiki)

    def v6_write(self, change):
        """Change the wiki, then publish it with the fixture's v6 writer core."""
        case = self.case
        change(self.wiki)
        roots = {case.WIKI: str(self.wiki)}
        with self.u10.seed.authority():
            with case.runtime(roots) as runtime:
                runtime.catalog.request_scan()
            case.publish_all(roots)
            case.rebind_fixture_ledger(roots)
        return self.u11.v6_generation()

    def store_rows(self, store, sql, args=()):
        with closing(sqlite3.connect(Path(store).as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute(sql, args).fetchall()

    def members(self, gen, store=None):
        """Member paths of one schema3 generation and the duplicated ones."""
        store = store or self.u15.intent()['candidate']
        rows = self.store_rows(store, 'SELECT path FROM wiki_members WHERE gen_id=?', (gen,))
        dups = self.store_rows(store, 'SELECT path FROM wiki_members WHERE gen_id=? GROUP BY path '
                               'HAVING count(*) > 1', (gen,))
        return sorted(path for (path,) in rows), [path for (path,) in dups]

    def v6_members(self):
        prefix = self.case.WIKI + '/'
        return sorted(path[len(prefix):] if path.startswith(prefix) else path for path in self.case.head()[1])

    def ops(self, since=0):
        """Terminal migration.shadow ops after ``since`` events (outcome != attempt/error)."""
        return [(e.get('op'), e.get('outcome')) for e in self.u15.events()[since:]
                if e['event'] == 'migration.shadow' and e.get('outcome') not in ('attempt',)]

    def gen_of(self, answer):
        return answer['results'][-1].data_generation if answer['results'] else None

    def same_answer(self, before, after, old_gen, new_gen):
        """U11 parity: the switched answer is the v6 answer with only the generation changed."""
        self.assertEqual(after['v6_runtime'], 0)
        self.assertEqual(after['context'], before['context'].replace(old_gen, new_gen))
        for key in ('outcome', 'reason', 'cards', 'count', 'notes', 'vector_fallback'):
            self.assertEqual(after['event'].get(key), before['event'].get(key), key)
        self.assertEqual(after['event']['store_format'], 'schema3')

    @contextmanager
    def sync_authority(self, extra=()):
        """The actual sync pass (``_run_sync`` unmocked) with the driver's authority."""
        real = self.u15.project_dir.layout
        paths = {self.case.project, *extra}
        with ExitStack() as stack:
            stack.enter_context(self.u10.bridge_authority())
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=self.case.plugin))
            stack.enter_context(mock.patch.object(
                self.u15.project_dir, 'layout', side_effect=lambda p: 'current' if Path(p) in paths else real(p)))
            yield

    def sync(self, project=None):
        def no_service(*_args, **_kwargs):
            raise AssertionError('a lexical schema3 pass started a provider service')
        with self.sync_authority():
            return self.W.run(project or self.case.project, start_service=no_service)

    def child(self, kill):
        case = self.case
        payload = case.home / 'pr67-crash.json'
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project), 'kill': kill}), encoding='utf8')
        return subprocess.run([sys.executable, '-B', '-c', CHILD, str(ROOT), str(payload)],
                              capture_output=True, text=True, timeout=200, env=dict(os.environ))

    def kill_at(self, kill, limit=40):
        """Child passes until one dies at ``kill``; returns the passes that completed first."""
        completed = []
        for _ in range(limit):
            done = self.child(kill)
            if done.returncode == -signal.SIGKILL:
                self.assertIn('killed-' + kill, done.stdout, done.stdout + done.stderr)
                return completed
            self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
            status = json.loads(done.stdout.strip().splitlines()[-1])
            self.assertNotEqual(status['state'], 'error', status)
            completed.append(status.get('migration_state'))
        self.fail('%s never reached: %s' % (kill, completed))

    # ---- 1. full path: passes, hook receipts, switch, U11 parity -----------
    def test_01_full_path_from_v6_to_schema3_answers_the_v6_recall(self):
        case, u15 = self.case, self.u15
        u15.challenges()
        v6_gen = self.u11.v6_generation()
        before = {host: self.u11.recall(host) for host in HOSTS}
        since = len(u15.events())
        with u15.driver():
            passes = u15.drive_to('shadow_verified')
        states = [p[1] for p in passes if p]
        ops = sorted({op for op, outcome in self.ops(since) if outcome not in ('error',)})
        for op in ('begin', 'copy', 'verification', 'prepare', 'publish', 'parity', 'parity_seal'):
            self.assertIn(op, ops)
        # Gate refusals of reader_ready (no receipts yet) are retries, not errors.
        self.assertEqual([op for op, outcome in self.ops(since)
                          if outcome == 'error' and op not in ('reader_ready', 'switch')], [])
        initial = u15.selection()
        # Ordinary recall hooks of both hosts record the loaded-reader receipts.
        for host in ('claude', 'codex'):
            code, _out = u15.hook(host)
            self.assertEqual(code, 0)
        self.assertEqual(len(u15.challenges()), 1)
        with u15.driver():
            ready, switched = u15.run_pass(), u15.run_pass()
        self.assertEqual([(e['outcome'], e['migration_state']) for e in (ready, switched)],
                         [('progressed', 'reader_ready'), ('progressed', 'switched')])
        after = u15.selection()
        self.assertEqual((after[0], after[2]), (initial[0] + 1, initial[1]))
        self.assertEqual(u15.store_format(), 'schema3')
        gen = u15.schema3_generation()
        members, dups = self.members(gen)
        self.assertEqual((members, dups), (self.cards(), []))
        self.assertEqual(members, self.v6_members())
        for host in HOSTS:
            with self.subTest(host=host):
                answer = self.u11.recall(host)
                self.same_answer(before[host], answer, v6_gen, gen)
                self.assertEqual(answer['results'][-1].store_format, 'schema3')
        # The registered hook entry point answers from the schema3 view too.
        for host in ('claude', 'codex'):
            code, out = u15.hook(host)
            self.assertIn('generation ' + gen, out)
            self.assertNotIn('generation ' + v6_gen, out)
        emit('pr67-full-path', passes=len(passes), states=sorted(set(states)), ops=ops,
             initial=list(initial), after=list(after), members=len(members))

    # ---- 2. writes between every step converge on the latest content -------
    def test_02_writes_between_steps_converge_and_recall_is_latest(self):
        case, u15 = self.case, self.u15
        heron = 'concepts/heron-route.md'

        def edit(token):
            def change(wiki):
                path = wiki / heron
                path.write_text(path.read_text(encoding='utf8').rstrip('\n') + '\n%s marker.\n' % token,
                                encoding='utf8')
            return change

        def add(rel, title, body):
            def change(wiki):
                (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
                (wiki / rel).write_text(card(title, body), encoding='utf8')
            return change

        def delete(rel):
            return lambda wiki: (wiki / rel).unlink()
        writes = [edit('Plover'), add('concepts/grebe-note.md', 'Grebe note', 'grebe note floats on the lake'),
                  delete('decisions/kestrel-plan.md'), edit('Avocet')]
        seen, restarts, passes = [], 0, 0
        with u15.driver():
            for _ in range(80):
                with mock.patch.object(self.W, 'SHADOW_STEPS_PER_PASS', 1):
                    event = u15.run_pass()
                passes += 1
                state = (u15.intent() or {}).get('migration_state')
                seen.append(state)
                if writes and passes % 2 == 0:
                    self.v6_write(writes.pop(0))
                    continue
                if not writes and state == 'shadow_verified':
                    break
            self.assertEqual(u15.intent()['migration_state'], 'shadow_verified', seen)
        restarts = len(list(case.state.glob('shadow-migration-intent.retired-*.json')))
        u15.loaded()
        with u15.driver():
            u15.run_pass()
            u15.run_pass()
        self.assertEqual(u15.intent()['migration_state'], 'switched')
        gen = u15.schema3_generation()
        members, dups = self.members(gen)
        emit('pr67-writes-members', schema3=members, v6=self.v6_members(), cards=self.cards(), seen=seen)
        self.assertEqual((members, dups), (self.v6_members(), []))
        self.assertEqual(members, self.cards())
        self.assertNotIn('decisions/kestrel-plan.md', members)
        latest = self.u11.recall(prompt='avocet marker')
        self.assertIn('Avocet marker', latest['context'])
        self.assertIn('Plover marker', latest['context'])
        self.assertIn(case.WIKI + '/concepts/grebe-note.md', self.u11.recall(prompt='grebe note lake')['context'])
        self.assertNotIn('kestrel-plan', self.u11.recall(prompt='kestrel plan tower')['context'])
        # After the switch the actual schema3 sync pass takes the writes.
        self.wiki.joinpath('concepts/otter-ledger.md').unlink()
        add('concepts/stilt-card.md', 'Stilt card', 'stilt card wades in brine')(self.wiki)
        edit('Curlew')(self.wiki)
        result = self.sync()
        self.assertIn(result['state'], ('indexed', 'published'), result)
        answer = self.u11.recall(prompt='curlew marker')
        post = self.gen_of(answer)
        emit('pr67-post-switch-sync', result=result, gen=post, context=answer['context'][:300])
        self.assertNotEqual(post, gen)
        store = self.u15.intent()['candidate']
        members, dups = self.members(post, store)
        self.assertEqual((members, dups), (self.cards(), []))
        self.assertIn('Curlew marker', answer['context'])
        self.assertIn(case.WIKI + '/concepts/stilt-card.md', self.u11.recall(prompt='stilt card brine')['context'])
        self.assertNotIn('otter-ledger', self.u11.recall(prompt='otter ledger river counts')['context'])
        emit('pr67-writes-converge', passes=passes, restarts=restarts, states=sorted(set(filter(None, seen))),
             members=len(members), post=result.get('state'))

    # ---- 6. rollback round trip --------------------------------------------
    def test_06_rollback_resume_and_switch_again(self):
        case, u15 = self.case, self.u15
        initial, switched = self.u16.switched()
        first_candidate = u15.intent()['candidate']
        result, _wakes = self.u16.rollback()
        self.assertEqual(result['status'], 'rolled_back', result)
        rolled = u15.selection()
        v6_gen = self.u11.v6_generation()
        self.assertIn('generation ' + v6_gen, u15.recall()['context'])
        with self.u10.bridge_authority():
            resumed = self.W.resume_shadow_migration(case.project)
        self.assertEqual(resumed['status'], 'resumed', resumed)
        with u15.driver():
            u15.drive_to('shadow_verified')
        u15.loaded()
        with u15.driver():
            u15.run_pass()
            u15.run_pass()
        intent = u15.intent()
        self.assertEqual(intent['migration_state'], 'switched')
        self.assertNotEqual(intent['candidate'], first_candidate)
        again = u15.selection()
        self.assertEqual((again[0], again[2]), (rolled[0] + 1, rolled[1]))
        self.assertNotEqual(again[1], switched[1])
        self.assertEqual(u15.store_format(), 'schema3')
        self.assertTrue(Path(first_candidate).exists())
        gen = u15.schema3_generation()
        answer = u15.recall()
        self.assertEqual(self.gen_of(answer), gen)
        self.assertEqual(self.members(gen)[0], self.cards())
        emit('pr67-rollback-round-trip', epochs=[initial[0], switched[0], rolled[0], again[0]])

    # ---- 3. SIGKILL at each step boundary, one install ---------------------
    def test_03_sigkill_at_each_boundary_resumes_without_loss_or_duplicates(self):
        case, u15 = self.case, self.u15
        initial = u15.selection()
        intent_path = case.state / self.M._INTENT
        chain = []

        def survives(kill, written):
            """After a kill: same selection, v6 recall unchanged, the killed write absent."""
            self.assertEqual(u15.selection(), initial, kill)
            answer = self.u11.recall()
            self.assertEqual(answer['v6_runtime'], 1, kill)
            self.assertIn('generation ' + self.u11.v6_generation(), answer['context'], kill)
            state = (u15.intent() or {}).get('migration_state')
            if kill.startswith('before:'):
                self.assertNotEqual(state, kill.split(':', 1)[1], kill)
            if kill.startswith('after:'):
                self.assertEqual(state, kill.split(':', 1)[1], kill)
            if written is not None and not kill.startswith('after:'):
                # Killed before its intent write: the recorded intent is the previous one.
                self.assertEqual(intent_path.read_bytes() if intent_path.exists() else None, written, kill)
            chain.append((kill, state))
        for kill in ('copy', 'prepare', 'publish', 'before:caught_up'):
            completed = self.kill_at(kill)
            survives(kill, None)
            chain[-1] += (len(completed),)
        # A v6 write between the publish cut and the parity seal.
        self.v6_write(lambda wiki: (wiki / 'concepts/heron-route.md').write_text(
            (wiki / 'concepts/heron-route.md').read_text(encoding='utf8') + '\nSanderling marker.\n',
            encoding='utf8'))
        for kill in ('before:shadow_verified', 'after:shadow_verified'):
            completed = self.kill_at(kill)
            written = None
            survives(kill, written)
            chain[-1] += (len(completed),)
        before = self.u11.recall()
        v6_gen = self.u11.v6_generation()
        u15.loaded()
        written = intent_path.read_bytes()
        self.kill_at('before:reader_ready')
        survives('before:reader_ready', written)
        self.kill_at('step:v5-image-committed')
        self.assertEqual(u15.selection(), initial)
        self.assertEqual(u15.store_format(), 'v6')
        self.kill_at('step:v5-image-replaced')
        landed = u15.selection()
        self.assertEqual((landed[0], landed[2]), (initial[0] + 1, initial[1]))
        self.assertEqual(u15.store_format(), 'schema3')
        self.assertEqual(u15.intent()['migration_state'], 'reader_ready')
        with u15.driver():
            status = self.W.shadow_pass(case.project)
        self.assertEqual((status['state'], status['reason']), ('skipped', 'reader_schema3_selected'))
        self.assertEqual(u15.intent()['migration_state'], 'switched')
        self.assertEqual(u15.selection(), landed)
        self.assertFalse((case.state / '.reader-next.sqlite').exists())
        gen = u15.schema3_generation()
        members, dups = self.members(gen)
        self.assertEqual((members, dups), (self.v6_members(), []))
        self.assertEqual(members, self.cards())
        after = self.u11.recall(prompt='sanderling marker')
        self.assertIn('Sanderling marker', after['context'])
        self.same_answer(before, self.u11.recall(), v6_gen, gen)
        candidates = sorted(p.name for p in case.state.glob('shadow-candidate*.sqlite'))
        restarts = len(list(case.state.glob('shadow-migration-intent.retired-*.json')))
        emit('pr67-crash-chain', chain=chain, candidates=len(candidates), restarts=restarts,
             members=len(members))

    # ---- 4. parallel recall processes during the switch CAS ----------------
    def test_04_parallel_recalls_during_the_cas_complete_in_old_or_new(self):
        case, u15 = self.case, self.u15
        with u15.driver():
            u15.drive_to('shadow_verified')
        u15.loaded()
        with u15.driver():
            u15.run_pass()
        self.assertEqual(u15.intent()['migration_state'], 'reader_ready')
        gens = {'old': self.u11.v6_generation(), 'new': u15.schema3_generation()}
        payload = case.home / 'pr67-recall.json'
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof, 'project': str(case.project),
                                       'gens': gens, 'limit': 60, 'host': 'claude'}), encoding='utf8')
        recalls = []
        for index in range(9):
            host_payload = case.home / ('pr67-recall-%d.json' % index)
            host_payload.write_text(json.dumps({**json.loads(payload.read_text(encoding='utf8')),
                                                'host': HOSTS[index % 3]}), encoding='utf8')
            recalls.append(subprocess.Popen([sys.executable, '-B', '-c', RECALL, str(ROOT), str(host_payload)],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                            env=dict(os.environ)))
        cas_payload = case.home / 'pr67-crash.json'
        cas_payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                           'project': str(case.project), 'kill': 'none'}), encoding='utf8')
        cas = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(ROOT), str(cas_payload)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        served = []
        try:
            out, err = cas.communicate(timeout=240)
            self.assertEqual(cas.returncode, 3, out + err)
            self.assertEqual(json.loads(out.strip().splitlines()[-1])['migration_state'], 'switched')
            for proc in recalls:
                rout, rerr = proc.communicate(timeout=300)
                self.assertEqual(proc.returncode, 0, rout + rerr)
                served.append(json.loads(rout.strip().splitlines()[-1]))
        finally:
            for proc in [cas, *recalls]:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        flat = [item for answers in served for item in answers]
        self.assertTrue(all(item['found'] in (['old'], ['new']) for item in flat), flat)
        self.assertFalse(any(item['refused'] for item in flat), flat)
        self.assertTrue(all(answers[-1]['found'] == ['new'] for answers in served), served)
        old = sum(item['found'] == ['old'] for item in flat)
        self.assertEqual(len(served), 9)
        self.assertEqual(self.gen_of(self.u11.recall()), gens['new'])
        emit('pr67-parallel-recall', processes=len(served), recalls=len(flat), old=old, new=len(flat) - old)

    # ---- 5. bundle upgrade across the migration ----------------------------
    @contextmanager
    def new_bundle(self, new, *, run_sync=True):
        """Upgrade authority for the bundle ``new`` as the executing (and journal-selected) bundle."""
        real = self.u15.project_dir.layout
        with ExitStack() as stack:
            upgrade, target, review, att = stack.enter_context(lifecycle.Lifecycle.upgrade_authority(self.u10, new))
            stack.enter_context(mock.patch.object(J, '_verified_executing_root', return_value=str(new)))
            stack.enter_context(mock.patch.object(J, '_provider_file_identity',
                                                  return_value=[[str(new / 'p.py'), 1, 2, 3, 4, 5]]))
            stack.enter_context(mock.patch.object(self.u10.bridge, 'inspect_full_plugin_candidate',
                                                  side_effect=lambda root: {**target, 'pluginRoot': str(root)}))
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=new))
            if run_sync:
                stack.enter_context(mock.patch.object(self.W, '_run_sync', return_value={'state': 'unchanged'}))
            stack.enter_context(mock.patch.object(
                self.u15.project_dir, 'layout',
                side_effect=lambda p: 'current' if Path(p) == self.case.project else real(p)))
            yield upgrade, target, review

    def upgrade(self, new, stop=None):
        case = self.case
        with lifecycle.Lifecycle.upgrade_authority(self.u10, new) as (upgrade, target, review, _att):
            intent = upgrade._prepare(case.project, case.state, case.journal, case.home, new,
                                      target['sourceCommit'], review['proposalSha256'], '/synthetic/model', 3)
            if stop is None:
                self.assertEqual(upgrade._switch(intent)['status'], 'active_upgrade_complete')
                return target
            with stop, self.assertRaises(rollback_checks.Stopped):
                upgrade._switch(intent)
            return target

    def loaded_for(self, new, target):
        """Loaded-reader receipts of the upgraded bundle ``new`` (owner) and a companion copy."""
        case, u10, bridge = self.case, self.u10, self.u10.bridge
        current = self.reader.read_selection(case.journal)
        companion = case.home / 'pr67-new-companion'
        companion.mkdir(exist_ok=True)
        commit = target['sourceCommit']

        def observed(challenge, host):
            value = u10.seed.helper.observer(case, challenge, host)
            root = new if host == 'claude' else companion
            info = root.stat()
            value.update(hook_stage='loaded-v5-reader', endpoint_id=current.preferred_endpoint,
                         reader_epoch=current.epoch, store_format='v6', generation='g' * 8,
                         sentinel_sha256='5' * 64, owner_role='owner' if host == 'claude' else 'companion',
                         plugin_root=str(root), plugin_node=[info.st_dev, info.st_ino],
                         process_pid=os.getpid() + (1 if host == 'codex' else 0),
                         release_commit=commit, manifest_sha256=target['bundleManifestSha256'])
            return value
        with self.new_bundle(new), \
                mock.patch.object(bridge, '_observe_registered_hook', side_effect=observed):
            challenge = bridge.begin_loaded_bridge(case.state, release_commit=commit, expected_hosts=frozenset(
                ('claude', 'codex')), sentinel_path='concepts/otter-ledger.md', sentinel_sha256='5' * 64)
            return [bridge.collect_bridge_preflight(challenge, host=host) for host in ('claude', 'codex')]

    def test_05_upgrade_finished_inside_the_migration_still_switches(self):
        case, u15 = self.case, self.u15
        with u15.driver():
            u15.drive_to('shadow_verified')
        before = u15.selection()
        new = case.home / 'pr67-new-plugin'
        new.mkdir()
        target = self.upgrade(new)
        upgraded = u15.selection()
        passes = []
        with self.new_bundle(new):
            for _ in range(40):
                status = self.W.shadow_pass(case.project)
                passes.append((status['state'], status.get('reason'), status.get('migration_state')))
                if status.get('migration_state') == 'shadow_verified' and status.get('reason') == 'switch_bridge_missing':
                    break
                if status.get('migration_state') == 'switched' or status['state'] == 'error':
                    break
        emit('pr67-upgrade-inside-migration', before=list(before), upgraded=list(upgraded), passes=passes)
        self.assertNotIn('error', [p[0] for p in passes], passes)
        # Receipts for the upgraded bundle's release commit, at the current selection.
        self.loaded_for(new, target)
        with self.new_bundle(new):
            ready = self.W.shadow_pass(case.project)
        self.assertEqual(u15.intent()['migration_state'], 'reader_ready', ready)
        # The CAS proves the attestation step on the v6 endpoint's sealed
        # successor chain; a chain that does not reach the target writes nothing.
        bytes_before = case.journal.read_bytes()
        real_chain = self.reader._successor_chain

        def other_target(db, endpoint_id, stored, _target_sha, *args, **kwargs):
            return real_chain(db, endpoint_id, stored, '0' * 64, *args, **kwargs)
        with self.new_bundle(new), mock.patch.object(self.reader, '_successor_chain', side_effect=other_target):
            refused = self.W.shadow_pass(case.project)
        self.assertEqual((refused['state'], refused['reason']), ('error', 'reader_successor_unproved'))
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        with self.new_bundle(new):
            for _ in range(4):
                status = self.W.shadow_pass(case.project)
                passes.append((status['state'], status.get('reason'), status.get('migration_state')))
                if status.get('migration_state') == 'switched' or status['state'] == 'error':
                    break
        emit('pr67-upgrade-then-switch', passes=passes[-4:])
        self.assertEqual(u15.intent()['migration_state'], 'switched', passes)
        self.assertEqual(u15.intent()['reader_ready']['release_commit'], target['sourceCommit'])
        with self.new_bundle(new):
            _endpoint, record, rows = lifecycle.Lifecycle.recall(self.u10, new, authority=False)
        self.assertEqual((record['store_format'], rows.store_format), ('schema3', 'schema3'))
        self.assertEqual(rows.data_generation, u15.schema3_generation())
        # Rollback from the upgraded bundle returns the v6 reader.
        result, _endpoint, stored, rows = self.u16.rollback_from(new)
        self.assertEqual((result['status'], stored['store_format'], rows.store_format),
                         ('rolled_back', 'v6', 'v6'))

    def test_05b_stopped_upgrade_fence_restore_settles_and_rollback_proceeds(self):
        """A worker stopped after its fence for a reason unrelated to schema3.

        The operator restore of the active upgrade releases the fence (recall
        and the edit gate work again). Nothing was staged, so the journal keeps
        selecting the old bundle; the restored intent is a settled receipt: a
        second restore is a no-op, the SessionStart worker refuses the same
        bundle before any fence instead of retrying it, the reader rollback
        proceeds, and the operator's explicit upgrade archives the receipt.
        """
        case, u15 = self.case, self.u15
        fresh = importlib.import_module('laya_fresh_wiki_origin')
        active = importlib.import_module('laya_active_upgrade')
        auto = importlib.import_module('laya_bundle_upgrade')
        _initial, switched = self.u16.switched()
        gen = u15.schema3_generation()
        new = case.home / 'pr67-stuck-plugin'
        new.mkdir()
        actual = fresh._change

        def stopped(*args, **kwargs):
            row = actual(*args, **kwargs)
            if row['phase'] == 'upgrading':
                raise rollback_checks.Stopped('_change')
            return row
        target = self.upgrade(new, stop=mock.patch.object(fresh, '_change', side_effect=stopped))
        self.assertTrue(fresh.upgrade_fenced(str(case.project)))
        bytes_before = case.journal.read_bytes()
        ledger_path = case.state / fresh.LEDGER

        def operator(restore):
            # The entry point's upgrade/restore action for the stuck bundle.
            with lifecycle.Lifecycle.upgrade_authority(self.u10, new) as (upgrade, _t, review, _a):
                return upgrade.upgrade(project_root=str(case.project), state_root=str(case.state),
                                       source_root=str(case.home), plugin_root=str(new),
                                       commit_id=target['sourceCommit'],
                                       accepted_provider_sha256=review['proposalSha256'],
                                       model_path='/synthetic/model', dimension=3, restore=restore)
        restored = operator(True)
        self.assertEqual(restored['status'], 'active_upgrade_restored_laya')
        self.assertFalse(fresh.upgrade_fenced(str(case.project)))
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        # Recall answers again from the selected schema3 view.
        self.assertEqual(self.gen_of(u15.recall()), gen)
        # The restored intent is a settled receipt; a second restore changes nothing.
        intent = active._load_intent(case.state, case.project)
        receipt = active.finished_intent_receipt(case.state, case.project, intent)
        self.assertFalse(receipt.exists())
        ledger_before = ledger_path.read_bytes()
        self.assertEqual(operator(True)['status'], 'active_upgrade_restored_laya')
        self.assertEqual((ledger_path.read_bytes(), case.journal.read_bytes()), (ledger_before, bytes_before))
        # The SessionStart worker for the same bundle refuses before any review
        # or fence: the operator restored away from it.
        # The fixture authority stubs ``upgrade_install``; load the real one.
        spec = importlib.util.spec_from_file_location('pr67_bundle_upgrade', ROOT / 'core' / 'laya_bundle_upgrade.py')
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        with lifecycle.Lifecycle.upgrade_authority(self.u10, new), \
                mock.patch.object(real, 'installed_commit', return_value=target['sourceCommit']) as installed, \
                mock.patch.object(real, 'select_source', side_effect=AssertionError('restored route reached review')):
            with self.assertRaisesRegex(real.UpgradeRefused, '^active_upgrade_restored$'):
                real.upgrade_install(case.project, case.state, new, home=case.home)
        installed.assert_called_once_with(new, case.home)
        self.assertIn('active_upgrade_restored', auto.REFUSALS)
        self.assertFalse(fresh.upgrade_fenced(str(case.project)))
        self.assertEqual((ledger_path.read_bytes(), case.journal.read_bytes()), (ledger_before, bytes_before))
        self.assertEqual(active._load_intent(case.state, case.project), intent)
        # The reader rollback proceeds and recall reads the v6 view.
        result, _wakes = self.u16.rollback()
        self.assertEqual(result['status'], 'rolled_back', result)
        self.assertNotEqual(u15.selection(), switched)
        self.assertIn('generation ' + self.u11.v6_generation(), u15.recall()['context'])
        # After fixing the cause, the operator's explicit upgrade archives the
        # receipt and moves the install to the bundle.
        self.assertEqual(operator(False)['status'], 'active_upgrade_complete')
        self.assertFalse(fresh.upgrade_fenced(str(case.project)))
        self.assertEqual(len(list(case.state.glob('active-upgrade-*-settled.json'))), 1)
        self.assertEqual(active._load_intent(case.state, case.project)['newProof']['pluginRoot'], str(new))
        emit('pr67-stuck-fence-restore', restored=restored['status'], journal=J.inspect_journal(case.journal).get('state'),
             rollback=result['status'])

    # ---- 7. linked worktree of a switched install --------------------------
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), '-c', 'commit.gpgsign=false', '-c',
                               'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture', '-c',
                               'user.email=fixture@example.invalid', *args],
                              check=True, capture_output=True, timeout=20, encoding='utf8')

    def linked_recall(self, cwd, prompt='heron route northern marsh', host='claude'):
        import io
        from contextlib import redirect_stderr
        route = importlib.import_module('laya_index_host_route')
        event_log = importlib.import_module('event_log')
        events, err = [], io.StringIO()
        with self.u11.loaded(), redirect_stderr(err), \
                mock.patch.object(event_log, 'emit', side_effect=lambda event, **f: events.append(dict(f, event=event))):
            decision = route.select_route(str(cwd), action='recall')
            answer = self.u11.host.serve_selected('recall', host, decision,
                                                  {'cwd': str(cwd), 'prompt': prompt, 'session_id': 'pr67-' + host,
                                                   'hook_event_name': 'UserPromptSubmit'})
        context = answer['context'] if host == 'hermes' else answer['hookSpecificOutput']['additionalContext']
        recall = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        return dict(state=decision['state'], context=context, event=recall[0], stderr=err.getvalue())

    def test_07_linked_worktree_reads_its_own_view_and_a_branch_switch_is_cold(self):
        case, u15 = self.case, self.u15
        project_dir = importlib.import_module('project_dir')
        self.u16.switched()
        main_gen = u15.schema3_generation()
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(case.project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        linked = base / 'linked'
        self.git(case.project, 'worktree', 'add', '-q', '-b', 'side', str(linked))
        settings = project_dir.settings_path(case.project)
        (linked / settings.relative_to(case.project)).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, linked / settings.relative_to(case.project))
        linked_wiki = linked / self.wiki.relative_to(case.project)
        shutil.copytree(self.wiki, linked_wiki, dirs_exist_ok=True)
        (linked_wiki / 'concepts/wren-linked.md').write_text(card('Wren linked', 'wren linked perches on the reed'),
                                                             encoding='utf8')
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.
        cold = self.linked_recall(linked)
        self.assertEqual((cold['state'], cold['context'], cold['event'].get('reason')),
                         ('worktree', '', 'view_preparing'), cold)
        with self.sync_authority(extra=[linked]):
            built = self.W.run(case.project, start_service=lambda *a, **k: self.fail('provider started'))
        emit('pr67-linked-built', state=built.get('state'), worktrees=built.get('worktrees'))
        self.assertIn(built['state'], ('indexed', 'unchanged'), built)
        main_gen = self.gen_of(self.u11.recall())
        own = {host: self.linked_recall(linked, 'wren linked reed', host) for host in HOSTS}
        linked_gen = own['claude']['event'].get('generation')
        self.assertNotIn(linked_gen, (None, main_gen))
        for host, answer in own.items():
            with self.subTest(host=host):
                self.assertEqual(answer['event'].get('store_format'), 'schema3')
                self.assertEqual(answer['event'].get('generation'), linked_gen)
                self.assertIn('wren-linked.md', answer['context'])
                self.assertNotIn('generation ' + main_gen, answer['context'])
        main = self.u11.recall(prompt='wren linked reed')
        self.assertEqual(self.gen_of(main), main_gen)
        self.assertNotIn('wren-linked.md', main['context'])
        # A branch switch in the linked checkout is a cold view until the owner publishes it.
        self.git(linked, 'switch', '-q', '-c', 'other')
        time.sleep(1.1)
        cold = self.linked_recall(linked, 'wren linked reed')
        self.assertEqual((cold['context'], cold['event'].get('reason')), ('', 'view_preparing'), cold)
        with self.sync_authority(extra=[linked]):
            self.W.run(case.project, start_service=lambda *a, **k: self.fail('provider started'))
        other = self.linked_recall(linked, 'wren linked reed')
        self.assertNotIn(other['event'].get('generation'), (None, main_gen, linked_gen))
        self.assertIn('wren-linked.md', other['context'])
        emit('pr67-linked-worktree', main=main_gen, linked=linked_gen, other=other['event'].get('generation'))

    # ---- 8. stores written by one sqlite build, read by the other ----------
    def test_08_other_interpreter_advances_and_reads_the_same_stores(self):
        case, u15 = self.case, self.u15
        other = other_python()
        if other is None:
            self.skipTest('no interpreter of the other sqlite build')
        version = subprocess.run([other, '-c', 'import sys, sqlite3; print(sys.version_info[:2], sqlite3.sqlite_version)'],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
        mine = '%s %s' % (tuple(sys.version_info[:2]), sqlite3.sqlite_version)
        if version == mine:
            self.skipTest('the other interpreter uses the same sqlite build')
        payload = case.home / 'pr67-crash.json'

        def other_pass():
            payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                           'project': str(case.project), 'kill': 'none'}), encoding='utf8')
            done = subprocess.run([other, '-B', '-c', CHILD, str(ROOT), str(payload)],
                                  capture_output=True, text=True, timeout=200, env=dict(os.environ))
            self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
            return json.loads(done.stdout.strip().splitlines()[-1])
        # This interpreter creates the candidate and copies; the other continues.
        with u15.driver(), mock.patch.object(self.W, 'SHADOW_STEPS_PER_PASS', 1):
            for _ in range(6):
                u15.run_pass()
                if u15.intent() is not None:
                    break
        self.assertEqual(u15.intent()['migration_state'], 'copying')
        statuses = []
        for _ in range(30):
            statuses.append(other_pass())
            if statuses[-1].get('migration_state') == 'shadow_verified':
                break
        self.assertNotIn('error', [s['state'] for s in statuses], statuses)
        self.assertEqual(u15.intent()['migration_state'], 'shadow_verified', statuses)
        u15.loaded()
        with u15.driver():
            u15.run_pass()
            u15.run_pass()
        self.assertEqual(u15.intent()['migration_state'], 'switched')
        gen = u15.schema3_generation()
        intent = u15.intent()
        store = intent['candidate']
        repository = json.loads(self.store_rows(store, 'SELECT repository_json FROM store_meta')[0][0])
        cross = case.home / 'pr67-cross.json'
        cross.write_text(json.dumps({'store': store, 'repository': repository, 'scope': case.route['scopeId'],
                                     'worktree': intent['worktree_id'], 'query': 'heron'}), encoding='utf8')
        read = subprocess.run([other, '-B', '-c', CROSS, str(ROOT), str(cross)],
                              capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        seen = json.loads(read.stdout.strip().splitlines()[-1])
        self.assertEqual(seen['gen'], gen)
        self.assertIn('concepts/heron-route.md', seen['rows'])
        recall_payload = case.home / 'pr67-cross-recall.json'
        recall_payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                              'project': str(case.project), 'gens': {'new': gen},
                                              'limit': 1, 'host': 'claude'}), encoding='utf8')
        recalled = subprocess.run([other, '-B', '-c', RECALL, str(ROOT), str(recall_payload)],
                                  capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(recalled.returncode, 0, recalled.stdout + recalled.stderr)
        self.assertEqual(json.loads(recalled.stdout.strip().splitlines()[-1]),
                         [{'found': ['new'], 'refused': False, 'state': 'candidate'}])
        emit('pr67-cross-sqlite', mine=mine, other=version, passes=len(statuses), rows=len(seen['rows']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
