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

    # ---- 2. writes between every step converge on the latest content -------

    # ---- 6. rollback round trip --------------------------------------------

    # ---- 3. SIGKILL at each step boundary, one install ---------------------

    # ---- 4. parallel recall processes during the switch CAS ----------------

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



    # ---- 7. linked worktree of a switched install --------------------------
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), '-c', 'commit.gpgsign=false', '-c',
                               'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture', '-c',
                               'user.email=fixture@example.invalid', *args],
                              check=True, capture_output=True, timeout=20, encoding='utf8')

    def linked_recall(self, cwd, prompt='heron route northern marsh', host='claude'):
        import io
        from contextlib import redirect_stderr
        route = importlib.import_module('memvane.route.index_host_route')
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
        project_dir = importlib.import_module('memvane.settings.project_dir')
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
