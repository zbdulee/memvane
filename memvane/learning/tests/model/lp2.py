"""LP-2: the learning path of LP-1 with the real trainer, run in pieces from one directory.

The fixture and the entry points are LP-1's (``memvane/learning/tests/lp1.py``):
edits enter through ``hooks/run-hook posttool`` and every pass is the worker
command. Two edges are real here: the trainer (``VerifiedLayaTrainer`` with the
lease and control the product hands it, for training and for the evaluation's
predictions) and the resource report (so the product's own admission decides).
The teacher is LP-1's deterministic fake CLI and the resident's apply transport
is LP-1's stand-in.

One guard run is at most 30 minutes and a real training takes several
120-second slices, so a run is continued: everything is kept under the
directory given on the command line. The first call builds the fixture there;
every later call attaches to it and goes on from the durable state (cases,
gold, the job's cursor, the application journal). A call stops starting passes
when its time budget is spent and exits 0; the record says whether the path
reached its end::

    QMD_LAYA_LIVE=1 QMD_LAYA_PYTHON=<laya python> QMD_LAYA_MODEL=<model dir> \\
      npm run dev:guard -- --kind model-test --max-seconds 1800 -- \\
      python3 -B -m memvane.learning.tests.model.lp2 <directory> --seconds 1500

``<directory>/lp2-record.jsonl`` holds, per call, a first line (its number,
the commit, the plugin version, the model identity the profile binds and the
arguments) and a last line (why the call ended); one line per pass (result,
counts of rows, invariant violations, seconds), one per trainer call (mode,
cases, seconds or the error code) and, once the path ended, a ``summary``
line. The call exits 0 when the path ended applied without a violation or the
call only ran out of time, 2 when it stopped on repeated resource deferrals
and 1 otherwise.

The trainer of this driver is built by the driver (the fixture's enrollment
is synthetic, so the product's own trainer construction cannot run here): the
training and prediction protocol is real, the product's monitoring of the
trainer is not exercised. ``resident_loop.py`` runs the trainer the product
builds, under its resource watch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest import mock

from memvane.learning.context_learning import automatic_staging, candidate_apply, general_teacher, shared
from memvane.learning.tests import lp1
from memvane.learning.tests.old_bundle_121 import ROOT
from memvane.learning.tests.support import invariants

EDITS = 160
EDIT_BATCH = 16
MAX_PASSES = 400
# A pass that asks to be woken again is followed after this pause; this many in a row
# with one status and reason end the call (the next call goes on).
RETRY_PAUSE_SECONDS = 10
RETRY_REPEATS = 30
MARKER, RECORD = 'lp2-fixture.json', 'lp2-record.jsonl'
PROMPT = 'Which marker does the emerald heron keep?'


class RealTrainer:
    """The product trainer on the real runtime and model; it records every call.

    The fixture's base artifact is identity bytes (the enrollment binds their
    hash), so a prediction for the base is answered from a real base artifact
    prepared once from the model.
    """

    def __init__(self, fixture, policy, control, fds, coordination_fd):
        from memvane.learning.context_learning.local_cycle import VerifiedLayaTrainer
        self.fixture = fixture
        adapter = str(ROOT / 'memvane/learning/context_learning/adapter.py')
        self.real = VerifiedLayaTrainer([os.environ['QMD_LAYA_PYTHON'], adapter, os.environ['QMD_LAYA_MODEL']],
                                        proof_cache_dir=fixture.proof_cache(adapter),
                                        max_seconds=policy['safetySeconds'], max_rss_mib=policy['maxRssMib'])
        self.real.learning_control, self.real.inherited_fds = control, fds
        self.real.coordination_fd = coordination_fd

    def __getattr__(self, name):
        return getattr(self.real, name)

    def call(self, mode, request, result_path, *, cwd):
        base = mode != 'train' and request['artifact_path'] == self.fixture.policy()['baseArtifact']
        if base:
            request = dict(request, artifact_path=str(self.fixture.base_artifact()))
        note = {'trainer': mode, 'cases': len(request.get('train') or request.get('cases') or ()), 'base': base}
        began = time.monotonic()
        try:
            answer = self.real.call(mode, request, result_path, cwd=cwd)
        except Exception as exc:
            self.fixture.note(dict(note, seconds=round(time.monotonic() - began, 1), error=str(exc)[:80],
                                   errorCode=getattr(self.real, 'last_error_code', None)))
            raise
        self.fixture.note(dict(note, seconds=round(time.monotonic() - began, 1)))
        return answer


class Lp2Fixture(lp1.Lp1Fixture):
    """LP-1's fixture kept in ``directory`` across processes."""

    directory = None
    # False keeps LP-1's fake trainer and ready resources (the driver's own test).
    real = True

    def runTest(self):  # A ``TestCase`` needs a method name; the driver calls its steps itself.
        pass

    # -- building and attaching ------------------------------------------------------
    def open(self):
        marker = self.directory / MARKER
        if marker.exists():
            self.attach(json.loads(marker.read_text(encoding='utf8')))
        else:
            self.setUp()
            # Every fifth answer is an empty selection: the evaluation needs both kinds.
            (self.logs / 'claude-queue.json').write_text(json.dumps(
                [{'answer': 'empty'} if index % 5 == 0 else {} for index in range(EDITS)]))
            marker.write_text(json.dumps({'home': str(self.home), 'basePointer': self.pointer()}), encoding='utf8')
        self.base = json.loads(marker.read_text(encoding='utf8'))['basePointer']
        self.edits = sum(self.counts()['cases'].values())

    def attach(self, saved):
        """The attributes ``setUp`` leaves, on the directories an earlier process built."""
        from memvane.kernel import hook_budget
        from memvane.kernel.memvane_version import plugin_version
        from memvane.learning import default_enrollment
        from tests.support.install_builder import Install, fixture_provider
        from tests.support.project import Project
        self.home = Path(saved['home'])
        self.runtime, self.tmp = self.home / 'rt', self.home / 'tmp'
        os.environ.update(HOME=str(self.home), MEMVANE_TEST_RUNTIME_ROOT=str(self.runtime), TMPDIR=str(self.tmp))
        tempfile.tempdir = None
        mock.patch.object(hook_budget, '_deadline', None).start()
        self.fixture = Project(self.home / 'project', 'fixture', ())
        self.project = Path(self.fixture.root).resolve()
        self.settings = self.project / '.memvane/settings.json'
        self.built = Install(self, self.fixture, '4' * 32, fixture_provider(self))
        self.decision = self.built.select()
        self.install_id, self.state = self.decision['installId'], self.built.state
        self.root = default_enrollment.default_namespace(self.project, self.install_id)
        self.model, self.runtime_file = self.home / 'model', self.home / 'python'
        self.version = plugin_version()
        self.host_stand_ins()
        self.bin, self.logs, self.user = self.home / 'bin', self.home / 'cli-logs', self.home / 'user'
        outside = Path(tempfile.mkdtemp(prefix='t', dir=str(self.home.parent))).resolve()
        self.patches.enter_context(mock.patch.object(tempfile, 'tempdir', str(outside)))
        self.environ = {'HOME': str(self.user), 'PATH': '%s:/usr/bin:/bin' % self.bin,
                        'TMPDIR': str(outside), 'LANG': 'C', 'USER': 'fixture', 'LOGNAME': 'fixture'}
        self.trained, self.predicted = [], []
        self.worker_stand_ins()

    def worker_stand_ins(self):
        if not self.real:
            return super().worker_stand_ins()
        real = general_teacher.process_pending_default
        for target, name, value in (
                (general_teacher, 'process_pending_default',
                 lambda root, project, policy, **k: real(root, project, policy, environ=self.environ, **k)),
                (automatic_staging, '_trainer',
                 lambda policy, factory=None, *, root=None, control=None, fds=(), coordination_fd=None:
                 RealTrainer(self, policy, control, fds, coordination_fd)),
                (candidate_apply, 'service_transport', lambda *a: self.transport)):
            self.patches.enter_context(mock.patch.object(target, name, value))

    def base_artifact(self):
        from memvane.learning.context_learning.adapter import prepare_base_artifact
        path = self.directory / 'base.artifact'
        if not path.exists():
            prepare_base_artifact(os.environ['QMD_LAYA_MODEL'], path)
        return path

    def proof_cache(self, adapter):
        """The runtime's synthetic smoke receipt, made once: a prediction only reads it.

        An install gets it from the selector preparation; this fixture's preparation is a stand-in.
        """
        from memvane.learning.context_learning.local_cycle import _atomic_json
        from memvane.learning.context_learning.runtime_setup import attest_runtime
        folder = self.directory / 'proof'
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / 'laya-synthetic-attestation.json'
        if not path.exists():
            proof = attest_runtime(os.environ['QMD_LAYA_PYTHON'], adapter, os.environ['QMD_LAYA_MODEL'])
            _atomic_json(path, {'schema': 'qmd-laya-live-proof-v1', 'verifiedAt': time.time(),
                                'attestation': proof['attestation']})
            self.note({'attested': True})
        return folder

    def identity(self):
        """What a call ran on: the plugin version and the model the profile binds."""
        from memvane.kernel.memvane_version import plugin_version
        from memvane.learning.context_learning import learning_profile
        try:
            model = self.read(learning_profile.NAME)['proposal'].get('modelIdentitySha256')
        except (OSError, ValueError, KeyError, TypeError):
            model = None
        return {'pluginVersion': plugin_version(), 'modelIdentitySha256': model}

    def due(self):
        """The driver is the wake: a deferred job or evaluation is due for the next pass."""
        with shared._db(self.root) as db:
            for table in ('learning_jobs', 'learning_evaluations'):
                if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    db.execute("UPDATE %s SET retry_at=0 WHERE status IN ('queued','running','deferred')" % table)

    # -- the record --------------------------------------------------------------------
    def note(self, row):
        with open(self.directory / RECORD, 'a', encoding='utf8') as out:
            out.write(json.dumps(row, sort_keys=True) + '\n')

    def notes(self):
        path = self.directory / RECORD
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []

    def evaluations(self):
        with shared._db(self.root) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_evaluations'").fetchone():
                return []
            return [list(row) for row in db.execute('SELECT status,reason FROM learning_evaluations')]

    def ended(self):
        """How the path ended, or None while it has not: applied, or a judged candidate."""
        if self.counts()['cycles'].get('applied'):
            return 'applied'
        closed = [reason for status, reason in self.evaluations() if status in ('rejected', 'blocked')]
        return 'not_applied:%s' % closed[0] if closed else None

    def promotion(self):
        """The judged ``learning.promotion`` rows: outcome, reason, case counts and the two exact rates."""
        keep = ('outcome', 'reason', 'trainCases', 'evalCases', 'evalEmpty', 'evalNonEmpty',
                'candidateExact', 'activeExact')
        return [{key: row.get(key) for key in keep} for row in self.events()
                if row.get('event') == 'learning.promotion' and row.get('outcome') != 'waiting']

    def metrics(self):
        """The four rates of the incumbent and the candidate, and the training resources, per cycle."""
        rates = ('exact_rate', 'necessary_recall', 'none_exact_rate', 'irrelevant_injection_rate', 'questions')
        found = []
        with shared._db(self.root) as db:
            for status, raw in db.execute('SELECT status,evidence FROM cycles ORDER BY started_at'):
                evidence = json.loads(raw)
                measured = evidence.get('metrics') or {}
                resources = evidence.get('trainResources') or {}
                found.append({'cycle': status,
                              **{side: {key: measured[side].get(key) for key in rates}
                                 for side in ('incumbent', 'candidate') if isinstance(measured.get(side), dict)},
                              'trainSeconds': resources.get('elapsed_seconds'),
                              'trainPeakRssKib': resources.get('observed_peak_rss_kib')})
        return found

    def recall_checkpoint(self):
        """The ``selectorCheckpoint`` of a recall row written after the path ended."""
        before = len(self.events())
        code, _out, _err = self.hook(ROOT, 'recall', {'hook_event_name': 'UserPromptSubmit',
                                                      'session_id': 'lp2-recall', 'prompt': PROMPT})
        rows = [row for row in self.events()[before:] if row.get('event') == 'recall']
        return {'exit': code, 'selectorCheckpoint': rows[-1].get('selectorCheckpoint') if rows else None,
                'selectionFallback': rows[-1].get('selection_fallback') if rows else None}

    def summary(self, end):
        notes = self.notes()
        trains = [row for row in notes if row.get('trainer') == 'train']
        passes = [row for row in notes if 'pass' in row]
        waits = {}
        for row in passes:
            if row['class'] == 'retry':
                key = '%s:%s' % (row['status'], row.get('reason'))
                waits[key] = waits.get(key, 0) + 1
        pointer = self.pointer()
        return {'summary': end, 'passes': len(passes), 'trainSlices': len(trains),
                'trainSliceSeconds': [row['seconds'] for row in trains],
                'trainErrors': [row['error'] for row in trains if 'error' in row],
                'predictions': [[row['cases'], row['base'], row['seconds']] for row in notes
                                if row.get('trainer') == 'predict'],
                'deferrals': waits, 'violations': sum(row['violations'] for row in passes),
                'promotion': self.promotion(), 'metrics': self.metrics(), 'applied': pointer != self.base,
                'appliedArtifact': pointer['artifact_sha256'][:12] if pointer != self.base else None,
                'recall': self.recall_checkpoint(), 'counts': self.counts()}


def run(directory, *, seconds, real=True, edits=EDITS, max_passes=MAX_PASSES, clock=time.monotonic,
        pause=time.sleep, fixture_class=None, identity=None):
    """Go on from what ``directory`` holds for at most ``seconds``; the summary, or None when not ended.

    Every call leaves a first line (``call``: its number, what it ran on and with) and a
    last line (``callEnd``: ``ended``, ``time_budget``, ``max_passes`` or ``retry_repeats``).
    """
    from memvane.learning.context_learning.state_model import CYCLE_RESULTS
    directory = Path(directory).resolve()
    began = clock()
    fixture = (fixture_class or Lp2Fixture)()
    fixture.directory, fixture.real = directory, real
    fixture.open()
    done = [row for row in fixture.notes() if 'summary' in row]
    if done:
        return done[-1]
    fixture.note({'call': 1 + len([row for row in fixture.notes() if 'call' in row]), 'seconds': seconds,
                  'real': real, 'editTarget': edits, 'maxPasses': max_passes, **fixture.identity(), **(identity or {})})
    while fixture.edits < edits and clock() - began < seconds:
        outcomes = fixture.edit(min(EDIT_BATCH, edits - fixture.edits))
        fixture.note({'edits': fixture.edits, 'stored': outcomes.count('stored'), 'captures': len(outcomes)})
    passes = len([row for row in fixture.notes() if 'pass' in row])
    repeated = (None, 0)
    while fixture.edits >= edits and passes < max_passes and clock() - began < seconds:
        end = fixture.ended()
        if end is not None:
            summary = fixture.summary(end)
            fixture.note(summary)
            fixture.note({'callEnd': 'ended'})
            return summary
        started = clock()
        result = fixture.run_project()
        passes += 1
        cycle = result.get('cycle') or {}
        status = cycle.get('status', result.get('status'))
        reason, kind = cycle.get('reason', result.get('reason')), CYCLE_RESULTS.get(status, ('unclassified',))[0]
        fixture.note({'pass': passes, 'status': status, 'reason': reason, 'class': kind,
                      'teacherCalls': (result.get('teacher') or {}).get('calls'),
                      'counts': fixture.counts(), 'violations': len(invariants.check(fixture.root)),
                      'seconds': round(clock() - started, 1)})
        if kind == 'retry':
            repeated = (repeated[0], repeated[1] + 1) if repeated[0] == (status, reason) else ((status, reason), 1)
            if repeated[1] >= RETRY_REPEATS:
                fixture.note({'stopped': 'retry_repeats', 'status': status, 'reason': reason})
                fixture.note({'callEnd': 'retry_repeats'})
                return None
            pause(max(0, min(RETRY_PAUSE_SECONDS, seconds - (clock() - began))))
            fixture.due()
        else:
            repeated = (None, 0)
    fixture.note({'callEnd': 'max_passes' if passes >= max_passes and fixture.edits >= edits else 'time_budget'})
    return None


def exit_code(directory, summary, *, passed):
    """0: the path ended as ``passed`` says, or the call only ran out of time. 2: stopped on
    repeated resource deferrals. 1: anything else (violations, another end, the pass limit)."""
    if summary is not None:
        return 0 if summary.get('violations') == 0 and passed(summary) else 1
    path = Path(directory) / RECORD
    rows = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []
    end = next((row['callEnd'] for row in reversed(rows) if 'callEnd' in row), None)
    violations = sum(row.get('violations', 0) for row in rows if 'pass' in row)
    if violations:
        return 1
    return {'time_budget': 0, 'retry_repeats': 2}.get(end, 1)


def head_commit(root):
    import subprocess
    done = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('--seconds', type=float, default=1500)
    args = parser.parse_args(argv)
    if os.environ.get('QMD_LAYA_LIVE') != '1':
        raise SystemExit('set QMD_LAYA_LIVE=1 with QMD_LAYA_PYTHON and QMD_LAYA_MODEL')
    directory = Path(args.directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = directory / 'h'
    home.mkdir(mode=0o700, exist_ok=True)
    # The fixture run's environment (``tests.support.case.require_fixture_env``).
    os.environ.update(HOME=str(home), QMD_SETUP_GUARD_FIXTURE='1',
                      MEMVANE_TEST_RUNTIME_ROOT=str(home / 'rt'), PYTHONDONTWRITEBYTECODE='1')
    (home / 'rt').mkdir(mode=0o700, exist_ok=True)
    summary = run(directory, seconds=args.seconds, identity={'commit': head_commit(ROOT)})
    print(json.dumps(summary if summary is not None else {'summary': None, 'continue': str(directory)}, sort_keys=True))
    return exit_code(directory, summary, passed=lambda row: row.get('summary') == 'applied')


if __name__ == '__main__':
    sys.exit(main())
