"""LP-1: the learning path end to end through real entry points, with fakes only at the edges.

An enabled default-teacher install (``EnabledInstall``). Edits enter through
``hooks/run-hook posttool`` and its detached capture child; every pass is the
worker command (``run-project``). The fakes are the host CLIs on the child
``PATH`` (the teacher), the trainer (it writes the candidate artifact and
answers predictions from the gold) and the resident's apply transport. The
driver keeps no state of its own: everything it reports is read back from the
namespace, so a run can be continued from the same directories.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import tempfile
import time
from unittest import mock

from memvane.learning.context_learning import (automatic_staging, candidate_apply, general_teacher,
                                               shared)
from memvane.learning.tests.old_bundle_121 import ROOT, EnabledInstall, edit_payload

READY = {'ready': True, 'available_mib': 10000, 'load_per_cpu': 0.1, 'total_mib': 16384,
         'query_status': {'memory_pressure': 'ok'},
         'memory_pressure': {'level': 'normal', 'system_free_percent': 70}}


def edit_text(index):
    """One edit of the fixture project: it matches both cards and no other edit."""
    return ('Field notes %04d for the pond survey. The emerald heron keeps the marker zqheronx '
            'beside the north pond, and the survey records visit number %d of the emerald heron '
            'with the date and the marker it carried. The marker zqheronx stays the same '
            'across every visit recorded in this survey, including visit %d.\n' % (index, index, index))


class FakeTrainer:
    """Writes the candidate artifact; predicts the gold for a candidate, nothing for the base."""

    def __init__(self, case):
        self.case = case

    def call(self, mode, request, result_path, *, cwd):
        if mode == 'train':
            self.case.trained.append(len(request['train']))
            with open(request['artifact_path'], 'wb') as out:
                out.write(b'candidate-' + request['manifest_sha256'].encode())
            os.chmod(request['artifact_path'], 0o600)
            return {'schema_version': 1, 'status': 'trained'}, {'fake': True}
        base = request['artifact_path'] == self.case.policy()['baseArtifact']
        self.case.predicted.append(len(request['cases']))
        gold = {} if base else self.case.gold_choices()
        return {'predictions': [{'request_id': c['request_id'], 'input_sha256': c['input_sha256'],
                                 'selected_ids': gold.get(c['request_id'].split(':', 1)[-1], [])}
                                for c in request['cases']]}, {'fake': True}


class Lp1Fixture(EnabledInstall):
    def setUp(self):
        super().setUp()
        self.user = self.home / 'user'
        self.user.mkdir(mode=0o700)
        outside = Path(tempfile.mkdtemp(prefix='t', dir=str(self.home.parent))).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        self.patches.enter_context(mock.patch.object(tempfile, 'tempdir', str(outside)))
        self.environ = {'HOME': str(self.user), 'PATH': '%s:/usr/bin:/bin' % self.bin,
                        'TMPDIR': str(outside), 'LANG': 'C', 'USER': 'fixture', 'LOGNAME': 'fixture'}
        self.trained, self.predicted, self.edits = [], [], 0
        real = general_teacher.process_pending_default
        report = mock.Mock(side_effect=lambda *a, **k: dict(READY, observed_at_mono=time.monotonic()))
        for target, name, value in (
                (general_teacher, 'process_pending_default',
                 lambda root, project, policy, **k: real(root, project, policy, environ=self.environ, **k)),
                (shared, 'resource_report', report),
                (general_teacher, 'resource_report', report),
                (automatic_staging, '_trainer', lambda *a, **k: FakeTrainer(self)),
                (candidate_apply, 'service_transport', lambda *a: self.transport)):
            self.patches.enter_context(mock.patch.object(target, name, value))
        self.warm_receipt()

    def warm_receipt(self):
        """The inference receipt a selector warm writes for the base pointer (the warm is a stand-in here)."""
        from memvane.learning.context_learning import inference_identity, learning_profile
        profile = self.read(learning_profile.NAME)['proposal']
        receipt = {'schema': inference_identity.SCHEMA, 'view': profile['view'],
                   'verified': {'base_model_sha256': profile['modelIdentitySha256']},
                   'checkpoint_sha256': learning_profile.digest(self.pointer()), 'createdAt': 1}
        for name, value in ((inference_identity.RECEIPT, receipt),
                            (inference_identity.BINDING, {'schema': inference_identity.SCHEMA,
                                                          'receipt_sha256': learning_profile.digest(receipt)})):
            if not (self.root / name).exists():
                self.write(self.root / name, value)

    def transport(self, command, record):
        return {'status': 'prepared' if command == 'prepare' else 'activated', 'token': record['apply_id']}

    # -- stimuli: real entry points --------------------------------------------------
    def edit(self, count):
        """``count`` new edits through the posttool hook; the capture outcomes."""
        outcomes = []
        for _ in range(count):
            self.edits += 1
            session, name = 'lp1-%04d' % self.edits, 'notes/pond-%04d.md' % self.edits
            self.fixture.write_note(name, edit_text(self.edits))
            before = len(self.events())
            code, _out, err = self.hook(ROOT, 'posttool', edit_payload(session, name, edit_text(self.edits)))
            self.assertEqual(code, 0, err)
            outcomes += [row['outcome'] for row in self.wait_capture(before, session)]
        return outcomes

    def run_project(self):
        out = io.StringIO()
        with redirect_stdout(out):
            shared.main(['run-project', str(self.project)])
        return json.loads(out.getvalue())

    # -- what the namespace says ------------------------------------------------------
    def counts(self):
        with shared._db(self.root) as db:
            cases = dict(db.execute('SELECT status,COUNT(*) FROM cases GROUP BY status'))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            jobs = (dict(db.execute('SELECT status,COUNT(*) FROM learning_jobs GROUP BY status'))
                    if 'learning_jobs' in tables else {})
            cycles = dict(db.execute('SELECT status,COUNT(*) FROM cycles GROUP BY status'))
        return {'cases': cases, 'jobs': jobs, 'cycles': cycles}

    def gold_choices(self):
        with shared._db(self.root) as db:
            return {rid: json.loads(gold)['selected_ids'] for rid, gold in
                    db.execute("SELECT request_id,gold FROM cases WHERE status='gold'")}

    def pointer(self):
        return json.loads((self.root / 'active-checkpoint.json').read_text())

    def jobs(self):
        with shared._db(self.root) as db:
            return [row[0] for row in db.execute('SELECT job_id FROM learning_jobs ORDER BY rowid')]
