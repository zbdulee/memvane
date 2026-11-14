"""Onboarding -> recall with the fixture provider (model-free stand-in).

Uses the 1.0 install use cases through ``tests.support.install_builder``
(plan, prepare, initial build passes, finalize, activate) with the fixture
provider (``tests.support.fake_provider``) and lexical recall, for a project
with no cards and one with thirty.

The lifecycle checks stop one step at an arbitrary durable point (a raised
``_Crash`` stands in for a killed process), rerun it and assert by journal
epochs, record phases, files and route states; none reads a wall clock.
"""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402
from tests.support.fake_provider import REVIEW_SHA  # noqa: E402
from tests.support.install_builder import Install, build_install, fixture_provider  # noqa: E402
from tests.support.install_builder import SETTLE_PASSES  # noqa: E402
from tests.support.project import make_project  # noqa: E402

INSTALL_ID = '4' * 32
SETTINGS_NAME = 'provenance'
DIMENSION = 2

TOPICS = ['heron', 'lantern', 'glacier', 'violin', 'saffron', 'compass', 'orchard', 'quartz',
          'harbor', 'falcon', 'meadow', 'ember', 'cobalt', 'willow', 'canyon', 'pepper',
          'beacon', 'tundra', 'marble', 'thistle', 'anchor', 'nebula', 'juniper', 'copper',
          'lagoon', 'raven', 'cedar', 'prism', 'dune', 'mosaic']
SECOND_INSTALL = '5' * 32
THIRD_INSTALL = '6' * 32


class _Crash(BaseException):
    """Stands in for a process killed at one durable step."""


def _crash_once(real, when=lambda *_a, **_k: True):
    """``real`` that raises ``_Crash`` on the first call ``when`` accepts."""
    state = {'armed': True}

    def wrapper(*args, **kwargs):
        if state['armed'] and when(*args, **kwargs):
            state['armed'] = False
            raise _Crash()
        return real(*args, **kwargs)
    return wrapper


def _fd_name(fd):
    """Base name of the file an fd is open on ('' when it names no file)."""
    import fcntl
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except (OSError, ValueError, AttributeError):
        return ''
    return os.path.basename(raw.split(b'\0', 1)[0].decode('utf-8', 'replace'))


def _opened_for(name):
    """``os.fdopen`` filter: the file (or the partial file beside it) is ``name``."""
    return lambda fd, *_a, **_k: _fd_name(fd) in (name, '.' + name + '.partial')


class FixtureOnboarding(FixtureCase):
    def install(self, cards, *, onboard=True):
        self.fixture = make_project(self.home, name=SETTINGS_NAME)
        self.project, self.raw_name = self.fixture.root, self.fixture.raw_name
        self.wiki = self.fixture.wiki / 'concepts'
        self.settings = self.fixture.settings_path
        self.note = self.fixture.write_note(
            'notes.md', 'Field notes: the emerald heron nests beside the reed pond.\n')
        for topic in TOPICS[:cards]:
            self.write_card(topic)
        self.fixture.commit('fixture')
        if onboard:
            self.target = build_install(self, self.fixture, install_id=INSTALL_ID)
            self.use_install(INSTALL_ID, self.target)
        else:
            self.use_install(INSTALL_ID)

    def use_install(self, install_id, target=None):
        """Point the lifecycle steps at one install id (the fixture provider stays the same)."""
        self.fake = fixture_provider(self, DIMENSION)
        self.target = target or Install(self, self.fixture, install_id, self.fake)
        self.install_id, self.state = install_id, self.target.state

    def write_card(self, topic):
        title = topic.title() + ' protocol'
        self.fixture.write_card(f'concepts/{topic}.md', title=title, sources=[self.note],
                                body=f'The {topic} protocol keeps marker token zq{topic}x for recall checks.\n')

    # -- lifecycle steps ---------------------------------------------------------
    def prepare(self):
        return self.target.prepare()

    def build(self):
        return self.target.build()

    def finalize(self):
        return self.target.finalize()

    def activate(self):
        return self.target.activate()

    def restore(self):
        return self.target.restore()

    def run_worker(self):
        return self.target.run_sync()

    def blocked(self, reason, step):
        from memvane.install.types import InstallBlocked
        with self.assertRaises(InstallBlocked) as caught:
            step()
        self.assertEqual(str(caught.exception), reason)

    def journal_row(self):
        from memvane.journal.api import inspect_journal
        return inspect_journal(self.state / 'journal.sqlite')

    def build_record(self):
        from memvane.journal.api import build_read_v1
        return build_read_v1(self.state)

    def build_pointer(self):
        from memvane.journal.api import build_registered_pointer_path
        return build_registered_pointer_path(str(self.project))

    def project_pointers(self):
        return sorted(name for name in ('route.json', 'selection.json')
                      if os.path.lexists(self.project / '.memvane' / name))

    def head_epoch(self):
        with closing(sqlite3.connect(self.state / 'store.sqlite')) as db:
            return db.execute('SELECT max(epoch) FROM s_heads').fetchone()[0]

    def hook_state(self, action='recall'):
        """What ``hooks/run-hook`` reads from ``hook-classify`` for this event."""
        from memvane.host.route_input import main as classify
        payload = self.payload_file()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(classify(['classify-hook', action, str(payload), str(self.project)]), 0)
        return out.getvalue().strip()

    def payload_file(self):
        path = self.home / 'hook-payload.json'
        path.write_text(json.dumps({'session_id': 's5-fixture', 'cwd': str(self.project),
                                    'hook_event_name': 'UserPromptSubmit',
                                    'prompt': 'what marker does the heron protocol keep?'}))
        return path

    def serve_hook(self):
        """``hook-serve recall``: exit code and the injected context."""
        from memvane.host.host_response import main as serve
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch('sys.stderr', err):
            code = serve(['serve-hook', 'recall', 'claude', str(self.payload_file()),
                          str(self.project)])
        body = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, ((body.get('hookSpecificOutput') or {}).get('additionalContext', '')), err.getvalue()

    def recall(self, prompt):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        decision = select_route(str(self.project), action='recall')
        self.assertEqual(decision.get('state'), 'candidate', decision)
        served = serve_selected('recall', 'claude', decision, {
            'session_id': 's5-fixture', 'cwd': str(self.project), 'hook_event_name': 'UserPromptSubmit',
            'prompt': prompt})
        return ((served or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')

    # -- onboarding ----------------------------------------------------------------
    def test_project_without_cards_onboards_and_recall_adds_no_card(self):
        self.install(0)
        context = self.recall('emerald heron reed pond')
        self.assertNotIn('zq', context)

    def test_project_with_thirty_cards_recalls_the_matching_card(self):
        self.install(30)
        context = self.recall('what marker does the glacier protocol keep?')
        self.assertIn('zqglacierx', context)
        self.assertNotIn('zqmosaicx', context)

    # -- interrupted steps -------------------------------------------------------------
    def test_hot_store_journal_rolls_back_at_classification(self):
        """F1: a writer killed mid-commit leaves a hot journal; the next route read recovers it."""
        from memvane.journal.api import select_route
        self.install(1)
        store = self.state / 'store.sqlite'
        code = ('import os,sqlite3,sys\n'
                'db=sqlite3.connect(sys.argv[1],isolation_level=None)\n'
                'db.execute("PRAGMA cache_size=1")\n'
                'db.execute("PRAGMA cache_spill=1")\n'
                'db.execute("BEGIN")\n'
                'db.execute("CREATE TABLE fixture_killed(x)")\n'
                'for _ in range(3000): db.execute("INSERT INTO fixture_killed VALUES(randomblob(1000))")\n'
                'os._exit(0)\n')
        subprocess.run([sys.executable, '-c', code, str(store)], check=True, timeout=60)
        hot = Path(str(store) + '-journal')
        self.assertTrue(hot.exists() and hot.stat().st_size > 0, 'the killed writer left a hot journal')
        decision = select_route(str(self.project), action='recall')
        self.assertEqual((decision['state'], decision['reason']), ('candidate', 'route_record_valid'))
        self.assertFalse(os.path.lexists(hot))
        with closing(sqlite3.connect(store.as_uri() + '?mode=ro', uri=True)) as db:
            tables = db.execute("SELECT count(*) FROM sqlite_master WHERE name='fixture_killed'").fetchone()
        self.assertEqual(tables[0], 0, 'the uncommitted transaction rolled back')
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def test_abandon_releases_unselected_installs_for_a_new_prepare(self):
        """F2: abandon before finalize and after finalize; each lets the next prepare start."""
        from memvane.install import initial_build
        self.install(1, onboard=False)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertIsNotNone(self.build_pointer())
        with mock.patch('memvane.install.initial_build.retire_project_pointer',
                        _crash_once(initial_build.retire_project_pointer)):
            with self.assertRaises(_Crash):
                self.restore()
        self.assertEqual(self.journal_row()['state'], 'restored')
        self.assertIsNotNone(self.build_record(), 'the record goes last')
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 1})
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 0})
        self.assertEqual((self.build_pointer(), self.build_record(), self.project_pointers()),
                         (None, None, []))
        self.assertTrue((self.state / 'store.sqlite').exists(), 'abandon deletes no store')
        # A finalized but never activated install abandons the same way.
        self.use_install(SECOND_INSTALL)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        from memvane.install import ledger_common
        with mock.patch.object(ledger_common, '_write',
                               _crash_once(ledger_common._write, lambda path, *_a, **_k:
                                           Path(path).name == 'ledger.json')):
            with self.assertRaises(_Crash):
                self.restore()
        self.assertEqual(self.journal_row()['state'], 'prepared', 'stopped at the ledger fence')
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 1})
        ledger = json.loads((self.state / 'ledger.json').read_text())
        self.assertEqual((ledger['phase'], self.journal_row()['state']), ('restored', 'restored'))
        self.assertIsNone(self.build_pointer())
        self.use_install(THIRD_INSTALL)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual(initial_build.lookup(str(self.project))['identity']['installId'],
                         THIRD_INSTALL)

    def test_source_change_between_finalize_and_activate_reseals(self):
        """F3: a raw edit after finalize (and after an activation stopped past its seed) converges."""
        from memvane.install.application import onboarding_activation as activation
        self.install(1, onboard=False)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        first = self.journal_row()
        with mock.patch.object(activation, '_write_or_verify_private',
                               _crash_once(activation._write_or_verify_private)):
            with self.assertRaises(_Crash):
                self.activate()  # Killed after the discovery seed, before any pointer.
        self.assertEqual(self.project_pointers(), [])
        self.note.write_text('Field notes: the emerald heron left the reed pond at dusk.\n')
        self.blocked('initial_build_out_of_date', self.activate)
        pending = self.finalize()
        self.assertEqual((pending['status'], pending['reason'], self.build_record()['phase']),
                         ('initial_build_pending', 'initial_build_out_of_date', 'building'))
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        resealed = self.journal_row()
        self.assertEqual(resealed['epoch'], first['epoch'] + 1)
        self.assertEqual(resealed['reader']['head_floor']['epoch'], self.head_epoch())
        self.assertGreater(resealed['reader']['head_floor']['epoch'],
                           first['reader']['head_floor']['epoch'])
        self.assertEqual(self.activate()['status'], 'selected')
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def test_rescan_after_selection_is_refused(self):
        """F4: a selected install's refresh belongs to the sync owner."""
        from memvane.install.application import onboarding_workflow as workflow
        self.install(1)
        for _ in range(2):
            self.blocked('initial_build_already_selected',
                         lambda: workflow.request_initial_build_rescan(self.state))
        self.assertEqual(self.build_record()['phase'], 'ready')
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def test_provider_change_releases_a_written_selection(self):
        """F5: after the first selected write a changed provider still exits through restore."""
        from memvane.install.application import onboarding_workflow as workflow
        from memvane.install import initial_build
        self.install(1)
        self.write_card('lantern')
        for _ in range(SETTLE_PASSES):
            self.run_worker()
            if self.journal_row()['newWriteSeen']:
                break
        self.assertTrue(self.journal_row()['newWriteSeen'])
        self.blocked('rollback_forward_only', self.restore)

        self.fake.change_identity()
        with mock.patch('memvane.install.initial_build.retire_project_pointer',
                        _crash_once(initial_build.retire_project_pointer)):
            with self.assertRaises(_Crash):
                self.restore()
        self.assertEqual(self.project_pointers(), [])
        # The provider matches again before the rerun: the begun release still finishes.
        self.fake.change_identity(False)
        self.assertEqual(self.restore(), {'status': 'restored', 'changesApplied': 1})
        row = self.journal_row()
        self.assertEqual((row['state'], row['newWriteSeen']), ('restored', True))
        self.assertEqual((self.build_pointer(), self.build_record()), (None, None))
        self.assertTrue((self.state / 'store.sqlite').exists(), 'the old install stays in place')
        self.assertEqual(workflow.plan_fresh_wiki_origin(self.project)['status'], 'review_required')

    def test_provider_change_release_reports_released(self):
        """F5: an uninterrupted release reports ``released``."""
        self.install(1)
        self.write_card('lantern')
        for _ in range(SETTLE_PASSES):
            self.run_worker()
            if self.journal_row()['newWriteSeen']:
                break
        self.assertTrue(self.journal_row()['newWriteSeen'])
        self.fake.change_identity()
        self.assertEqual(self.restore(), {'status': 'released', 'changesApplied': 1})
        self.fake.change_identity(False)
        self.assertEqual((self.project_pointers(), self.journal_row()['state']), ([], 'restored'))

    def test_reruns_compare_recorded_nodes_by_inode(self):
        """F6: prepare and finalize reruns after a boot that renumbered st_dev write nothing."""
        from memvane.install.application import onboarding_workflow as workflow
        from memvane.journal import journal_reader
        self.install(1, onboard=False)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        route = (self.state / 'route.json').read_bytes()
        record = self.build_record()
        live = workflow._live_node
        with mock.patch.object(workflow, '_live_node',
                               lambda path: [live(path)[0] + 7, live(path)[1]]):
            self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual((self.state / 'route.json').read_bytes(), route)
        self.assertEqual(self.build_record(), record)
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        sealed = self.journal_row()
        node, file_node = journal_reader._node, journal_reader._file
        with mock.patch.object(journal_reader, '_node',
                               lambda path: [node(path)[0] + 7, node(path)[1]]), \
             mock.patch.object(journal_reader, '_file',
                               lambda path, state: [file_node(path, state)[0] + 7,
                                                    file_node(path, state)[1]]):
            self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(self.journal_row(), sealed, 'no reseal, same epoch and reader bytes')

    def test_recall_in_interrupted_windows_exits_zero_without_context(self):
        """F7: every stopped activation step leaves recall at exit 0 without context."""
        from memvane.install import ledger_common
        from memvane.journal import install_journal
        from memvane.journal.api import select_route
        self.install(1, onboard=False)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        # A preparing install whose settings identity moved: preview proof fails.
        self.settings.write_text(json.dumps({'name': 'renamed'}) + '\n')
        preview = select_route(str(self.project), action='recall')
        self.assertEqual((preview['state'], preview.get('recallQuiet')), ('blocked', True))
        self.assertEqual(self.hook_state('recall'), 'legacy')
        self.fixture.write_settings()
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        route_file = self.project / '.memvane' / 'route.json'
        from memvane.install.application import onboarding_activation as activation
        # 1. Marker written, route not yet.
        with mock.patch.object(activation, '_write_or_verify_private',
                               _crash_once(activation._write_or_verify_private,
                                           lambda path, raw: Path(path).name == 'route.json')):
            with self.assertRaises(_Crash):
                self.activate()
        self.assertEqual(self.project_pointers(), ['selection.json'])
        self.assertEqual(self.hook_state('recall'), 'legacy')
        # 2. Both pointers, journal CAS not committed.
        with mock.patch.object(install_journal, '_open',
                               _crash_once(install_journal._open,
                                           lambda path, write: write and route_file.exists())):
            with self.assertRaises(_Crash):
                self.activate()
        self.assertEqual((self.journal_row()['state'], self.hook_state()), ('prepared', 'candidate'))
        self.assertEqual(self.serve_hook()[:2], (0, ''))
        # 3. Journal selected, ledger still prepared.
        with mock.patch.object(ledger_common, '_write',
                               _crash_once(ledger_common._write,
                                           lambda path, *_a, **_k: Path(path).name == 'ledger.json')):
            with self.assertRaises(_Crash):
                self.activate()
        self.assertEqual(self.journal_row()['state'], 'selected')
        self.assertEqual(self.serve_hook()[:2], (0, ''))
        self.assertEqual(self.activate()['status'], 'selected')
        code, context, _err = self.serve_hook()
        self.assertEqual(code, 0)
        self.assertIn('zqheronx', context)

    def test_prepare_refuses_a_raw_tree_over_the_scan_bounds(self):
        """F8: over 4,096 raw files is refused at prepare, before any build record."""
        self.install(1, onboard=False)
        bulk = self.project / 'bulk'
        bulk.mkdir()
        for index in range(4097):
            (bulk / f'n{index:04d}.md').write_text('x\n')
        for _ in range(2):
            self.blocked('raw_scan_limit_exceeded', self.prepare)
            self.assertEqual((self.build_record(), self.build_pointer()), (None, None))
            self.assertFalse((self.state / 'journal.sqlite').exists())
        shutil.rmtree(bulk)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')

    # -- install use cases (rereview C1) ---------------------------------------------------
    def test_finalize_takes_the_sync_dispatch_check_as_a_port(self):
        """C1: finalize lives in install.api; its one app-level check comes in as a port."""
        from memvane.install import api as install
        self.install(1, onboard=False)
        self.assertEqual(install.prepare_fresh_wiki_origin(
            project_root=str(self.project), plugin_root=str(ROOT), state_root=str(self.state),
            expected_proposal_sha256=install.plan_fresh_wiki_origin(self.project)['proposalSha256'],
            install_id=self.install_id, accepted_provider_sha256=REVIEW_SHA,
            dimension=DIMENSION, provider=self.fake)['status'], 'initial_build_registered')
        self.build()
        before = self.journal_row()
        calls = []

        def refused(project):
            calls.append(project)
            raise RuntimeError('dispatch_refused')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                install.finalize_initial_build(project_root=str(self.project), state_root=str(self.state),
                                               resolve_sync_target=refused)
        self.assertEqual(calls, [str(self.project)] * 2)
        self.assertEqual(self.journal_row(), before, 'a refused dispatch check seals nothing')
        self.assertFalse((self.state / 'ledger.json').exists())
        finalized = install.finalize_initial_build(project_root=str(self.project),
                                                   state_root=str(self.state),
                                                   resolve_sync_target=calls.append)
        self.assertEqual((finalized['status'], len(calls)), ('initial_build_finalized', 3))
        self.assertEqual(json.loads((self.state / 'ledger.json').read_text())['phase'], 'prepared')
        self.assertEqual(install.select_origin(project_root=str(self.project),
                                               state_root=str(self.state))['status'], 'selected')
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def cli(self, entry, argv):
        """Run one setup CLI ``main`` past the installed-plugin live-write check (B4 test bypass)."""
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'}):
            code = entry.main(argv)
        return code, json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_recover_without_ledger_never_abandons(self):
        """Verify R1: with no ledger, recover refuses unless an abandon already began."""
        from memvane.app import setup_entry, setup_workflow
        from memvane.install import api as install, initial_build
        self.install(1, onboard=False)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.build()
        recover = lambda: install.select_origin(project_root=str(self.project),
                                                state_root=str(self.state), recover=True)
        for _ in range(2):
            self.blocked('initial_build_not_finalized', recover)
        code, row = self.cli(setup_entry, ['recover-index-origin', '--project', str(self.project),
                                           '--state-root', str(self.state)])
        self.assertEqual((code, row['status'], row['reason']),
                         (1, 'rejected', 'initial_build_not_finalized'))
        self.blocked('initial_build_not_finalized', lambda: self.cli(setup_workflow, [
            'recover', '--project-root', str(self.project), '--state-root', str(self.state),
            '--route-file', str(self.state / 'route.json')]))
        self.assertEqual((self.journal_row()['state'], self.build_record()['phase']), ('prepared', 'ready'))
        self.assertIsNotNone(self.build_pointer())
        self.assertFalse((self.state / 'ledger.json').exists())
        # An abandon that stopped after the journal said restored: recover finishes it.
        with mock.patch('memvane.install.initial_build.retire_project_pointer',
                        _crash_once(initial_build.retire_project_pointer)):
            with self.assertRaises(_Crash):
                self.restore()
        self.assertEqual(self.journal_row()['state'], 'restored')
        self.assertEqual(recover(), {'status': 'restored', 'changesApplied': 1})
        self.assertEqual(recover(), {'status': 'restored', 'changesApplied': 0})
        self.assertEqual((self.build_pointer(), self.build_record()), (None, None))

    def test_other_major_route_is_released_by_restore_then_plans(self):
        """Verify R2: the 'other' notice's path works: restore releases, plan passes."""
        from memvane.host.onboarding_notice import MAJOR_NOTICES, onboarding_notice
        from memvane.install import api as install
        from memvane.install.application import onboarding_restore
        from memvane.journal.api import select_route
        self.install(1)
        folder = self.project / '.memvane'
        route = json.loads((folder / 'route.json').read_bytes())
        newer = self.write_pointer('route.json', {**route, 'schema': 'memvane-route-v5'})
        restore = lambda: install.restore_origin_selection(project_root=str(self.project),
                                                           state_root=str(self.state))
        # A newer major's pointers are never released here.
        for _ in range(2):
            self.assertIsNone(onboarding_restore._release_other_major(self.project))
        self.assertEqual((folder / 'route.json').read_bytes(), newer)
        self.write_pointer('route.json', {**route, 'schema': 'laya-index-route-v1'})
        self.assertEqual(onboarding_notice(str(self.payload_file()), str(self.project)),
                         MAJOR_NOTICES['other'])
        self.assertNotIn('python3', MAJOR_NOTICES['other'])
        self.blocked('project_selection_already_exists',
                     lambda: install.plan_fresh_wiki_origin(self.project))
        with mock.patch.object(onboarding_restore, '_retire_foreign_pointer',
                               _crash_once(onboarding_restore._retire_foreign_pointer,
                                           lambda path: path.name == 'route.json')):
            with self.assertRaises(_Crash):
                restore()
        self.assertEqual(self.project_pointers(), ['route.json'], 'the mismatching route goes last')
        self.assertEqual(restore(), {'status': 'released', 'changesApplied': 1})
        self.assertEqual(self.project_pointers(), [])
        self.assertNotIn('majorMismatch', select_route(str(self.project), action='update'))
        self.assertEqual(install.plan_fresh_wiki_origin(self.project)['status'], 'review_required')
        self.assertEqual(self.journal_row()['state'], 'selected', 'the state root is not read')

    def test_refused_registration_keeps_foreign_pointers(self):
        """Verify R5: foreign pointers go only after the build registration succeeded."""
        import hashlib
        self.install(1)
        folder = self.project / '.memvane'
        saved = {name: (folder / name).read_bytes() for name in ('route.json', 'selection.json')}
        # A second machine: no install directory, but a stale build pointer of it remains.
        self.state.rename(self.home / 'elsewhere')
        self.use_install(THIRD_INSTALL)
        from memvane.install.application import onboarding_workflow as workflow
        self.assertEqual(workflow.plan_fresh_wiki_origin(self.project)['replacesForeignPointers'],
                         {name: hashlib.sha256(raw).hexdigest() for name, raw in saved.items()})
        for _ in range(2):
            with self.assertRaises(Exception) as caught:
                self.prepare()
            self.assertEqual(str(caught.exception), 'initial_build_parent_unavailable')
            self.assertEqual({name: (folder / name).read_bytes() for name in saved}, saved)
        self.build_pointer().rename(self.home / 'elsewhere-build-pointer.json')
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.project_pointers(), [])
        self.assertIsNotNone(self.build_pointer())

    def test_plan_reviewed_before_a_reboot_still_prepares(self):
        """Verify F6: a renumbered project st_dev keeps the plan, so prepare reruns pass."""
        from memvane.install import api as install
        from memvane.install.application import onboarding_workflow as workflow
        self.install(1, onboard=False)
        reviewed = install.plan_fresh_wiki_origin(self.project)
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        record = self.build_record()
        real_stat, real_plan, project = Path.stat, workflow.plan_fresh_wiki_origin, str(self.project)

        def renumbered(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) != project:
                return info
            fields = list(info[:10])
            fields[2] += 7
            return os.stat_result(fields, {name: getattr(info, name) for name in dir(info)
                                           if name.startswith('st_') and name not in (
                                               'st_mode', 'st_ino', 'st_dev', 'st_nlink', 'st_uid',
                                               'st_gid', 'st_size')})

        def plan_after_reboot(*args, **kwargs):
            # The plan sees the new boot's device number for the project.
            with mock.patch.object(Path, 'stat', renumbered):
                self.assertEqual(self.project.stat().st_dev, os.stat(project).st_dev + 7)
                return real_plan(*args, **kwargs)
        with mock.patch.object(workflow, 'plan_fresh_wiki_origin', plan_after_reboot):
            after = workflow.plan_fresh_wiki_origin(self.project)
            self.assertEqual((after['proposalSha256'], after['scopeId']),
                             (reviewed['proposalSha256'], reviewed['scopeId']))
            for _ in range(2):
                self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.build_record(), record)

    # -- proof/state review --------------------------------------------------------------
    def write_pointer(self, name, record):
        """Replace one project pointer with ``record``'s canonical bytes; returns them."""
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        (self.project / '.memvane' / name).write_bytes(raw)
        return raw

    def test_foreign_route_is_replaced_by_a_new_onboarding(self):
        """Proof F3: pointers of an install missing on this machine give way; a live one never."""
        import hashlib
        from memvane.install.application import onboarding_workflow as workflow
        from memvane.journal.api import select_route
        self.install(1)
        plan = lambda: workflow.plan_fresh_wiki_origin(self.project)
        self.blocked('project_selection_already_exists', plan)
        folder = self.project / '.memvane'
        saved = {name: (folder / name).read_bytes() for name in ('route.json', 'selection.json')}
        # Inode mismatch while the install directory exists here: foreign, but never replaced.
        route = json.loads(saved['route.json'])
        moved = self.write_pointer('route.json', {**route, 'stateNode': [route['stateNode'][0],
                                                                      route['stateNode'][1] + 1]})
        marker = json.loads(saved['selection.json'])
        self.write_pointer('selection.json', {**marker, 'routeSha256': hashlib.sha256(moved).hexdigest()})
        decision = select_route(str(self.project), action='update')
        self.assertEqual((decision['state'], decision['reason']), ('legacy', 'foreign_install'))
        self.blocked('project_selection_already_exists', plan)
        for name, raw in saved.items():
            (folder / name).write_bytes(raw)
        self.assertEqual(select_route(str(self.project), action='update')['state'], 'candidate')
        # The committed pointers on a second machine: no install, no build pointer.
        self.state.rename(self.home / 'elsewhere')
        self.build_pointer().rename(self.home / 'elsewhere-build-pointer.json')
        decision = select_route(str(self.project), action='update')
        self.assertEqual((decision['state'], decision['reason']), ('legacy', 'foreign_install'))
        reviewed = plan()
        self.assertEqual(reviewed['status'], 'review_required')
        self.assertEqual(reviewed['replacesForeignPointers'],
                         {name: hashlib.sha256(raw).hexdigest() for name, raw in saved.items()})
        self.use_install(THIRD_INSTALL)
        from memvane.install.application import onboarding_restore
        with mock.patch.object(onboarding_restore, '_retire_foreign_pointer',
                               _crash_once(onboarding_restore._retire_foreign_pointer,
                                           lambda path: path.name == 'selection.json')):
            with self.assertRaises(_Crash):
                self.prepare()
        self.assertEqual(self.project_pointers(), ['selection.json'], 'the route goes first')
        self.assertIsNotNone(self.build_pointer(), 'the build registers before any pointer goes')
        self.assertEqual(list(plan()['replacesForeignPointers']), ['selection.json'])
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.project_pointers(), [])
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(self.activate()['status'], 'selected')
        self.assertEqual(json.loads((folder / 'route.json').read_bytes())['installId'], THIRD_INSTALL)
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def test_pointer_of_another_major_answers_without_hook_work(self):
        """Proof F4: an unknown schema or another major follows D4, never a refused hook."""
        from memvane.install.application import onboarding_workflow as workflow
        from memvane.host.onboarding_notice import MAJOR_NOTICES, onboarding_notice
        from memvane.journal.api import dispatch_target, selected_project_proof
        from memvane.journal.types import DispatchTargetError
        from memvane.kernel.errors import IndexContractError
        self.install(1)
        folder = self.project / '.memvane'
        saved = {name: (folder / name).read_bytes() for name in ('route.json', 'selection.json')}
        route, marker = (json.loads(saved[name]) for name in ('route.json', 'selection.json'))

        def answers(kind):
            for action in ('recall', 'update-queued', 'update', 'gate', 'posttool', 'index',
                           'compile', 'file-change-event'):
                self.assertEqual(self.hook_state(action), 'legacy', action)
            self.assertEqual(onboarding_notice(str(self.payload_file()), str(self.project)),
                             MAJOR_NOTICES[kind])
            with self.assertRaises(DispatchTargetError) as caught:
                dispatch_target(self.project, build=True)
            self.assertEqual(caught.exception.code, 'state_format_mismatch')
            with self.assertRaises(IndexContractError):
                selected_project_proof(self.project)
            self.blocked('project_selection_already_exists',
                         lambda: workflow.plan_fresh_wiki_origin(self.project))

        changed = (('route.json', {**route, 'schema': 'memvane-route-v5'}, 'newer'),
                   ('route.json', {**route, 'stateFormat': 2}, 'newer'),
                   ('route.json', {**route, 'stateFormat': 2, 'storeFormat': 'v5'}, 'newer'),
                   ('route.json', {**route, 'schema': 'laya-index-route-v1'}, 'other'),
                   ('selection.json', {**marker, 'schema': 'memvane-selection-v2'}, 'newer'))
        for name, record, kind in changed:
            self.write_pointer(name, record)
            answers(kind)
            (folder / name).write_bytes(saved[name])
        self.assertEqual(self.hook_state('recall'), 'candidate')
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))

    def test_interrupted_record_creation_reruns(self):
        """Proof F7: attestation, journal and project pointers never stay half written."""
        from memvane.journal import install_journal
        self.install(1, onboard=False)
        partials = lambda: sorted(path.name for folder in (self.state, self.project / '.memvane')
                                  for path in folder.iterdir() if path.name.endswith('.partial'))
        attestation = self.state / install_journal.PRODUCTION_ATTESTATION
        with mock.patch('os.fdopen', _crash_once(os.fdopen, _opened_for(attestation.name))):
            with self.assertRaises(_Crash):
                self.prepare()
        self.assertEqual((attestation.exists(), partials()), (False, []))
        with mock.patch.object(install_journal, '_inspect_v1_row',
                               _crash_once(install_journal._inspect_v1_row)):
            with self.assertRaises(_Crash):
                self.prepare()
        self.assertTrue(attestation.exists())
        self.assertEqual(((self.state / 'journal.sqlite').exists(), partials()), (False, []))
        self.assertEqual(self.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.journal_row()['state'], 'prepared')
        self.build()
        self.assertEqual(self.finalize()['status'], 'initial_build_finalized')
        for name in ('selection.json', 'route.json'):
            with mock.patch('os.fdopen', _crash_once(os.fdopen, _opened_for(name))):
                with self.assertRaises(_Crash):
                    self.activate()
            self.assertEqual(partials(), [])
            self.assertNotIn(name, self.project_pointers())
        self.assertEqual(self.activate()['status'], 'selected')
        self.assertEqual(self.project_pointers(), ['route.json', 'selection.json'])
        self.assertIn('zqheronx', self.recall('what marker does the heron protocol keep?'))


if __name__ == '__main__':
    unittest.main()
