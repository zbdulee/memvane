"""1.0.21 fixture for G-9, G-3 and G-14: a selected install whose default namespace ran the enable.

``EnabledInstall`` builds a real selected install (``tests.support.install_builder``)
of a git project with one verified card, enrolls its default namespace while
the teacher is switched off (default-local), removes the switch and runs the
1.0.21 enable transaction (``default_teacher_enable.run``). With
``enable_fault`` set the transaction stops at that write and its intent stays.
The installed-host boundary is ``RenewalFixture``'s (synthetic host bundles,
code identity, selector heal/warm), in this process and, through a
``sitecustomize``, in this bundle's hook children; selection, the install
journal and store, enrollment, the setup journal, the enable CAS and the
shared DB are real. Shared DB rows (cases, cohort, ledger) are seeded with
product helpers.

Old bundles are the repository's ``v1.0.18``/``v1.0.19`` tags, extracted with
``git archive`` once per process. They run as child processes with this HOME,
test runtime root and a ``PATH`` whose ``claude``/``codex`` are fakes that log
every launch (``fake_teacher_cli_121``) and whose ``python3`` is this
interpreter.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock

from memvane.kernel.memvane_version import plugin_version
from memvane.learning import default_enrollment, default_teacher_enable as enable
from memvane.learning.context_learning import adapter, edit_git_migration, general_cohort
from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import shared, teacher_consent, teacher_state
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import edit_sample, install as install_fake
from memvane.learning.tests.renewal_fixture import RenewalFixture
from tests.support.case import FixtureCase, require_fixture_env, wait_until
from tests.support.install_builder import ROOT, build_install
from tests.support.project import make_project

TAGS = ('v1.0.18', 'v1.0.19', 'v1.0.20', 'v1.0.21')
NOTE = 'The emerald heron nests beside the north pond.\n'
CARD_BODY = 'The emerald heron keeps marker token zqheronx beside the north pond.\n'
PROMPT = 'what marker does the emerald heron keep?'
# This bundle's hook children (hook-serve and the capture children) get the
# same installed-host stand-ins as this process: synthetic bundle code
# identity, host listings, the running adapter path and the running bundle
# (``shared.__file__``). Old bundles only when explicitly requested (``patched=True``).
SITECUSTOMIZE = '''import json, os
_raw = os.environ.get('MV121_CHILD')
if _raw:
    def _apply(config):
        from pathlib import Path
        from unittest import mock
        from memvane.learning.context_learning import (adapter_binding, host_inventory,
                                                       inference_identity, project_scope,
                                                       selector_heal, shared)
        home, running = Path(config['home']), Path(config['running'])

        def snapshot(root, _home=None):
            root = Path(root)
            if home not in root.parents or not (root / 'memvane/marker.py').is_file():
                root = running
            files = {'learning/context_learning/adapter.py': inference_identity.file_sha(
                         root / 'memvane/learning/context_learning/adapter.py'),
                     'marker.py': inference_identity.file_sha(root / 'memvane/marker.py')}
            return files, tuple(('memvane/' + name, value) for name, value in sorted(files.items()))

        def code(*_args, **_kwargs):
            return project_scope.proposal_sha256(snapshot(running)[0])

        def listing(argv, *_args):
            return {'installed': [dict(config['codex'])]} if argv[0] == 'codex' else [dict(config['claude'])]
        for target, name, value in ((project_scope, '_code_snapshot', snapshot),
                                    (project_scope, 'code_identity', code),
                                    (inference_identity, 'code_identity', code),
                                    (host_inventory, '_project_listing', listing),
                                    (adapter_binding, '_codex_installed', lambda *a: dict(config['codex'])),
                                    (selector_heal, 'running_adapter',
                                     lambda: running / 'memvane/learning/context_learning/adapter.py')):
            mock.patch.object(target, name, value).start()
        if config.get('resource_ready'):
            from memvane.learning.context_learning import general_teacher
            ready = {'ready': True, 'available_mib': 10000, 'load_per_cpu': 0.1, 'total_mib': 16384}
            for mod in (shared, general_teacher):
                mock.patch.object(mod, 'resource_report', return_value=dict(ready)).start()
        shared.__file__ = str(running / 'memvane/learning/context_learning/shared.py')
    _apply(json.loads(_raw))
'''
BUNDLE_PATHS = ('memvane', 'entry', 'hooks', 'package.json', '.claude-plugin', '.codex-plugin',
                'plugin.json', 'plugin.yaml')
TABLES = ('cases', 'project_cohort_exposure', 'project_cohort_assignments', 'project_gold_attempts',
          'learning_jobs', 'learning_evaluations', 'cycles')
EDIT_FILE = 'notes/pond.md'
EDIT_TEXT = ('Field notes for the pond survey. The emerald heron keeps the marker zqheronx '
             'beside the north pond, and the survey records each visit of the emerald heron '
             'with the date and the marker it carried. The marker zqheronx stays the same '
             'across every visit recorded in this survey.\n')
SURVEY_NOTE = 'The pond survey records each visit of the emerald heron.\n'
SURVEY_BODY = ('The pond survey records each visit of the emerald heron and the marker '
               'zqheronx it carried beside the north pond.\n')
CHILD_TIMEOUT = 120
_BUNDLES = {}


class Interrupted(Exception):
    pass


def old_bundle(tag):
    """The extracted bundle root of ``tag``; raises when the tag is missing."""
    if tag in _BUNDLES:
        return _BUNDLES[tag]
    listed = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '--name-only', tag],
                            capture_output=True, text=True)
    if listed.returncode != 0:
        raise AssertionError('old bundle tag %s unavailable: %s' % (tag, listed.stderr.strip()))
    paths = [path for path in BUNDLE_PATHS if path in listed.stdout.split('\n')]
    # Under the run HOME: kept for the whole process, removed with the run.
    dest = Path(tempfile.mkdtemp(prefix='ob-%s-' % tag, dir=str(require_fixture_env()))).resolve()
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', tag, *paths],
                             capture_output=True)
    if archive.returncode != 0:
        raise AssertionError('git archive %s failed: %s' % (tag, archive.stderr.decode('utf8', 'replace')))
    subprocess.run(['tar', '-x', '-C', str(dest)], input=archive.stdout, check=True)
    version = json.loads((dest / 'package.json').read_text(encoding='utf8'))['version']
    if 'v' + version != tag:
        raise AssertionError('bundle %s carries version %s' % (tag, version))
    _BUNDLES[tag] = dest
    return dest


def edit_payload(session, rel=EDIT_FILE, text=EDIT_TEXT):
    return {'hook_event_name': 'PostToolUse', 'session_id': session, 'tool_name': 'Write',
            'tool_input': {'file_path': rel, 'content': text}, 'tool_use_id': 'tool-' + session}


class EnabledInstall(RenewalFixture):
    """A selected install whose default namespace is default-teacher after the enable."""

    # A write name of ``default_teacher_enable.writes`` (or 'intent'): the enable
    # stops there and its intent stays (G-3). None finishes the enable.
    enable_fault = None

    def setUp(self):
        FixtureCase.setUp(self)
        from memvane.kernel import hook_budget
        original_shared = shared.__file__
        self.addCleanup(setattr, shared, '__file__', original_shared)
        budget = mock.patch.object(hook_budget, '_deadline', None)
        budget.start()
        self.addCleanup(budget.stop)
        self.fixture = make_project(self.home)
        note = self.fixture.write_note('notes/heron.md', NOTE)
        self.fixture.write_card('heron.md', title='Emerald heron', body=CARD_BODY, sources=[note])
        # A second matching card: edit capture needs a pool of at least two.
        survey = self.fixture.write_note('notes/survey.md', SURVEY_NOTE)
        self.fixture.write_card('survey.md', title='Pond survey', body=SURVEY_BODY, sources=[survey])
        self.fixture.commit('fixture')
        self.project = Path(self.fixture.root).resolve()
        self.settings = self.project / '.memvane/settings.json'
        self.built = build_install(self, self.fixture)
        self.built.settle()
        self.decision = self.built.select()
        self.install_id = self.decision['installId']
        self.state = self.built.state
        self.root = default_enrollment.default_namespace(self.project, self.install_id)
        self.model = self.home / 'model'
        for name in adapter.MODEL_FILES:
            self.write(self.model / name, {})
        self.write(self.model / 'rl_agent_config.json', {
            'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024, 'head_max_len': 256})
        self.runtime_file = self.home / 'python'
        self.runtime_file.write_text('synthetic interpreter', encoding='utf8')
        self.runtime_file.chmod(0o700)
        self.version = plugin_version()
        self.select(host='claude', version=self.version, marker='A')
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patches = stack
        for target, kwargs in (
            ('memvane.learning.runtime_activation.ensure_pending', {}),
            ('memvane.learning.context_learning.host_inventory._project_listing', {'side_effect': self.listing}),
            ('memvane.learning.context_learning.adapter_binding._codex_installed',
             {'side_effect': lambda *a: dict(self.codex)}),
            ('memvane.learning.context_learning.project_scope._code_snapshot', {'side_effect': self.snapshot}),
            ('memvane.learning.context_learning.project_scope.code_identity', {'side_effect': self.code}),
            ('memvane.learning.context_learning.inference_identity.code_identity', {'side_effect': self.code}),
            ('memvane.learning.context_learning.selector_heal.running_adapter',
             {'side_effect': lambda: self.running / 'memvane/learning/context_learning/adapter.py'}),
            ('memvane.learning.context_learning.selector_heal.official_runtime',
             {'return_value': (str(self.runtime_file), str(self.model))}),
            ('memvane.learning.context_learning.selector_heal.pressure', {'return_value': None}),
            ('memvane.learning.context_learning.selector_heal.heal', {'return_value': {'status': 'selector_ready'}}),
            ('memvane.learning.context_learning.selector_heal.warm',
             {'return_value': {'status': 'selector_warm_current'}}),
        ):
            stack.enter_context(mock.patch(target, **kwargs))
        self.root.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.root.parent, self.root):
            directory.mkdir(mode=0o700, exist_ok=True)
        self.assertTrue(teacher_state.write_switch(self.root, False))
        prepared = default_enrollment.prepare(self.project, self.state, self.install_id, lease_fd=-1)
        self.assertEqual(prepared['status'], 'selected', prepared)
        self.write(self.root / 'laya-inference-manifest.json',
                   {str(self.runtime_file): [[], file_sha(self.runtime_file)]})
        (self.root / teacher_state.SWITCH_NAME).unlink()
        self.assertTrue(default_enrollment.default_local_only(self.root, self.project, self.policy()))
        fault = self.enable_fault

        def stop(reached):
            name = enable._shared(self.project) if fault == 'shared' else fault
            if reached == name:
                raise Interrupted(reached)
        if fault is None:
            result = enable.run(self.root, self.project, host='claude', host_version=self.version,
                                install_id=self.install_id)
            self.assertEqual(result['status'], 'enabled', result)
            self.assertTrue(default_enrollment.default_teacher(self.root, self.project, self.policy()))
        else:
            with self.assertRaises(Interrupted):
                enable.run(self.root, self.project, host='claude', host_version=self.version,
                           install_id=self.install_id, fault=stop)
            self.assertTrue((self.root / enable.INTENT).is_file())
        if not (self.root / teacher_state.STATE_NAME).exists():
            self.assertTrue(teacher_state.write_state(self.root, teacher_state.read_state(self.root)))
        # The 1.0.19+ capture writes this record once when it is missing (§18-2).
        edit_git_migration.record_fix_time(self.root, self.project)
        self.assertTrue((self.root / edit_git_migration._name(
            edit_git_migration.FIX_PREFIX, self.project)).is_file())
        self.bin = self.home / 'bin'
        self.bin.mkdir(mode=0o700)
        self.logs = self.home / 'cli-logs'
        self.logs.mkdir(mode=0o700)
        for host in ('claude', 'codex'):
            install_fake(self.bin, host, self.logs)
        (self.bin / 'python3').write_text('#!/bin/sh\nexec %s "$@"\n' % json.dumps(sys.executable),
                                          encoding='utf8')
        (self.bin / 'python3').chmod(0o700)

    # -- state ---------------------------------------------------------------------
    def policy(self):
        return self.read('shared-policy.json')

    def key(self):
        return shared._project_key(self.project)

    def namespace_files(self):
        """Every regular file under the namespace (relative path -> bytes), DB files apart."""
        files = {}
        for path in sorted(self.root.rglob('*')):
            if path.is_file() and not path.is_symlink() and '.sqlite' not in path.name:
                files[str(path.relative_to(self.root))] = path.read_bytes()
        return files

    def watched_names(self):
        """§15 G-9 namespace files: the eight enable targets, the host state and the consents."""
        return (*enable.targets(self.project), teacher_state.STATE_NAME, teacher_consent.NAME)

    def rows(self):
        """Every row of the four G-9 tables, in rowid order (a missing table is None)."""
        from memvane.kernel.errors import _query_only_uri
        if not (self.root / shared.DATABASE_NAME).is_file():
            return dict.fromkeys(TABLES)
        with closing(sqlite3.connect(_query_only_uri(self.root / shared.DATABASE_NAME), uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: (list(db.execute('SELECT * FROM %s ORDER BY rowid' % table))
                            if table in names else None) for table in TABLES}

    def seed_rows(self):
        """Queued, gold and trained v5 cases plus cohort and ledger rows."""
        key = self.key()
        with shared._db(self.root) as db:
            general_cohort._tables(db)
            ledger.ensure(db)
            for index, status in enumerate(('queued', 'queued', 'gold', 'trained')):
                rid = 'seed-%d' % index
                sample, bodies = edit_sample(rid, host=('claude', 'codex')[index % 2])
                input_sha = digest(canonical(sample))
                gold = (canonical({'selected_ids': [sample['candidates'][0]['candidate_id']],
                                   'input_sha256': input_sha, 'review': 'single_pass'})
                        if status in ('gold', 'trained') else None)
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                           'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold,'
                           'trained_cycle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key, str(self.project), rid, input_sha, canonical(sample), canonical(bodies),
                            'corpus', 1000.0 + index, 'train', '[]', 'active', status, gold,
                            'cycle-0' if status == 'trained' else None))
                db.execute('INSERT INTO project_cohort_assignments VALUES (?,?,?,?,?)',
                           ('scope-0', rid, input_sha, 'train', 'family-%d' % index))
                db.execute('INSERT INTO project_cohort_exposure VALUES (?,?,?,?)',
                           ('scope-0', 'question:' + digest(rid), 'train', 'family-%d' % index))
                if status != 'queued':
                    ledger.reserve(db, key, rid, 'first', input_sha, None, rearm=True)
                    db.execute("UPDATE project_gold_attempts SET stage='committed' WHERE request_id=?", (rid,))
        before = self.rows()
        self.assertEqual([len(before[table]) for table in TABLES[:4]], [4, 4, 4, 2])
        return before

    def seed_122_rows(self, *, include_queued=True):
        """Seed 1.0.22 rows (``first_done``, ``answer_parked``, ``lineage`` job, ``blocked`` evaluation)."""
        from memvane.learning.context_learning import candidate_close
        from memvane.learning.context_learning.evaluation_queue import EvaluationQueue
        from memvane.learning.context_learning.learning_queue import LearningQueue
        key = self.key()
        statuses = (('queued', 'queued', 'first_done', 'gold', 'trained') if include_queued
                    else ('first_done', 'gold', 'trained'))
        with shared._db(self.root) as db:
            general_cohort._tables(db)
            ledger.ensure(db)
            LearningQueue(self.root).schema(db)
            EvaluationQueue(self.root).schema(db)
            for index, status in enumerate(statuses):
                rid = 'seed122-%d' % index
                sample, bodies = edit_sample(rid, host=('claude', 'codex')[index % 2])
                input_sha = digest(canonical(sample))
                gold = (canonical({'selected_ids': [sample['candidates'][0]['candidate_id']],
                                   'input_sha256': input_sha, 'review': 'single_pass'})
                        if status in ('gold', 'trained') else None)
                first = None
                if status == 'first_done':
                    choice = {'selected_ids': [sample['candidates'][0]['candidate_id']], 'usage': {}}
                    first = canonical({'choice': choice,
                                       'teacher': {'host': 'claude', 'model': 'claude-haiku-5-5', 'tier': 1},
                                       'modelReported': 'claude-haiku-5-5', 'toolWarnings': [],
                                       'pluginWarnings': [], 'event': {'teacherHost': 'claude', 'tier': 1},
                                       'rawBytes': 128})
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                           'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,first,gold,'
                           'trained_cycle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key, str(self.project), rid, input_sha, canonical(sample), canonical(bodies),
                            'corpus', 1000.0 + index, 'train', '[]', 'active', status, first, gold,
                            'cycle-0' if status == 'trained' else None))
                db.execute('INSERT INTO project_cohort_assignments VALUES (?,?,?,?,?)',
                           ('scope-0', rid, input_sha, 'train', 'family-%d' % index))
                db.execute('INSERT INTO project_cohort_exposure VALUES (?,?,?,?)',
                           ('scope-0', 'question:' + digest(rid), 'train', 'family-%d' % index))
                if status == 'first_done':
                    payload_sha = digest(canonical({'instruction': 'i', 'text': sample['prompt']['text'],
                                                    'candidates': []}))
                    ledger.reserve(db, key, rid, 'first', payload_sha, None, rearm=True)
                    ledger.transition(db, key, rid, 'first', ('reserved',), 'answer_parked',
                                      answer_sha256=digest(canonical(choice)))
                elif status != 'queued':
                    ledger.reserve(db, key, rid, 'first', input_sha, None, rearm=True)
                    db.execute("UPDATE project_gold_attempts SET stage='committed' WHERE request_id=?", (rid,))
            cycle_id = 'cohort-' + 'a' * 20
            grant = {'projectRoot': str(self.project), 'namespaceRoot': str(self.root),
                     'operations': ['gold_default', 'local_train', 'quality_evaluate'],
                     'cohort': {'id': 'default-v1'}}
            manifest = {'id': cycle_id, 'namespace': LearningQueue(self.root).scope,
                        'approval': 'b' * 64, 'cohort': 'default-v1', 'family': 'c' * 64,
                        'members': [], 'lineage': candidate_close.lineage(grant)}
            db.execute("INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,'staged',NULL,0,0,0,NULL)",
                       (cycle_id, manifest['namespace'], manifest['approval'], manifest['cohort'],
                        manifest['family'], canonical(manifest)))
            db.execute("INSERT INTO cycles VALUES (?,'staged',?,?,NULL)",
                       (cycle_id, canonical({'schema_version': 1, 'authorizationSha256': manifest['approval']}),
                        1000.0))
        EvaluationQueue(self.root).reject(cycle_id, 'apply_quality_regression')
        return self.rows()

    # -- children --------------------------------------------------------------------
    def child_env(self, patched=False, *, bundle=ROOT, resource_ready=False):
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONPATH', 'CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT', 'QMD_SANDBOX',
                              'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CODEX_SANDBOX', 'GEMINI_SANDBOX',
                              'QMD_HOOK_DEADLINE_MONO', 'MEMVANE_TEACHER', 'MEMVANE_CAPTURE_FDS')}
        env.update(PATH='%s:/usr/bin:/bin' % self.bin, PYTHONDONTWRITEBYTECODE='1')
        if patched:
            stub = self.tmp / 'stub'
            if not stub.is_dir():
                stub.mkdir(mode=0o700)
                (stub / 'sitecustomize.py').write_text(SITECUSTOMIZE, encoding='utf8')
            env.update(PYTHONPATH='%s:%s' % (stub, bundle), MV121_CHILD=json.dumps({
                'home': str(self.home), 'running': str(self.running), 'codex': self.codex,
                'claude': self.claude, 'resource_ready': resource_ready}))
        return env

    def launches(self):
        """Every fake ``claude``/``codex`` launch (any stage) recorded so far."""
        path = self.logs / 'calls.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]

    def run_child(self, argv, *, stdin=None, patched=False, bundle=ROOT, resource_ready=False):
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=str(self.project),
                                env=self.child_env(patched, bundle=bundle, resource_ready=resource_ready),
                                timeout=CHILD_TIMEOUT)
        return result.returncode, result.stdout, result.stderr

    def entry(self, bundle, *args, patched=False, resource_ready=False):
        return self.run_child([str(self.bin / 'python3'), '-B', str(Path(bundle) / 'entry/run.py'), *args],
                              patched=patched, bundle=bundle, resource_ready=resource_ready)

    def hook(self, bundle, action, payload, host='claude'):
        """``hooks/run-hook`` of ``bundle``; this bundle's children carry the stand-ins."""
        return self.run_child(['bash', str(Path(bundle) / 'hooks/run-hook'), action, host],
                              stdin=json.dumps({'cwd': str(self.project), **payload}),
                              patched=Path(bundle) == ROOT)

    def driver(self, bundle, code):
        """``code`` in a child whose ``memvane`` is ``bundle``'s; prints its JSON ``result``."""
        script = self.tmp / 'driver.py'
        script.write_text('import json, sys\nsys.path.insert(0, %r)\n%s\nprint(json.dumps(result))\n'
                          % (str(bundle), code), encoding='utf8')
        code_, out, err = self.run_child([str(self.bin / 'python3'), '-B', str(script)])
        self.assertEqual(code_, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def events(self):
        return self.built.events()

    def wait_capture(self, before, session=None):
        """The install event log's ``learning.capture`` lines after the first ``before`` lines."""
        return wait_until(lambda: [row for row in self.events()[before:]
                                   if row.get('event') == 'learning.capture'
                                   and (session is None or row.get('session') == session)], timeout=90)

    # -- this bundle's capture -------------------------------------------------------
    def capture_edit(self, session, host='claude'):
        """This bundle's posttool hook on a fresh edit: (answer, capture outcomes).

        The hook exits 0; its detached capture children are awaited through
        their ``learning.capture`` event line.
        """
        self.fixture.write_note(EDIT_FILE, EDIT_TEXT)
        before = len(self.events())
        code, out, err = self.hook(ROOT, 'posttool', edit_payload(session), host)
        self.assertEqual(code, 0, err)
        return json.loads(out), [row['outcome'] for row in self.wait_capture(before, session)]

    def learning_main(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(out):
            os.environ.pop('MEMVANE_TEACHER', None)
            code = __import__('memvane.learning.api', fromlist=['learning_main']).learning_main(list(argv))
        return code, json.loads(out.getvalue())
