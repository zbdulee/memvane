"""Script-type learning checks (``memvane/learning/tests/scripts``), one test per script.

Each script runs as ``python -B <script>`` from the repository root in the
test's private HOME, must exit 0, and its stdout is checked the way its old
node wrapper checked it (plan R6: the only learning tests that start a process
per check).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.learning.tests.support import REPO
from tests.support.case import FixtureCase

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
HANG_LIMIT = 300


def _all_true(*keys, zero=()):
    def check(test, out):
        report = json.loads(out)
        for key in keys:
            test.assertIs(report.get(key), True, key)
        for key in zero:
            test.assertEqual(report.get(key), 0, key)
    return check


def _no_external_calls(test, out):
    report = json.loads(out)
    test.assertTrue(report)
    test.assertEqual(report.get('external_calls', 0), 0)


def _equal(expected):
    def check(test, out):
        test.assertEqual(json.loads(out), expected)
    return check


def _proof_cache(test, out):
    result = json.loads(out)
    keys = ('freshReuseWithoutSmoke', 'ageInvalidates', 'missingDenied', 'modelChangeInvalidates',
            'adapterChangeInvalidates', 'runtimeChangeInvalidates', 'unsafeProofDenied',
            'permissionDenied', 'malformedDenied', 'checksPreserved', 'cacheRevocationHonored',
            'memoryProofExpires', 'explicitTrainThenPredict', 'stateUnchanged',
            'noPredictionAttestation', 'expiryDuringProbeDenied', 'revocationDuringProbeDenied',
            'inferenceOnlyAfterTrainingExpiry', 'inferenceOnlyWarmNoMetadata',
            'inferenceOnlyMutationDenied', 'inferenceOnlyBindingRevocation')
    for host in ('claude', 'codex'):
        report = result['hosts'][host]
        for key in keys:
            test.assertIs(report[key], True, f'{host}:{key}')
        test.assertEqual(report['actualModelCalls'], 0)
        test.assertEqual(report['teacherCalls'], 0)


def _shared_local(test, out):
    report = json.loads(out)
    test.assertEqual(report['shared_queue'], 'passed')
    test.assertEqual(report['local_trial'], 'passed')
    test.assertEqual(report['external_calls'], 0)


# script -> (stdout check or None, extra environment)
TABLE = {
    'context_learning_adapter_binding.py': (_all_true(
        'bothHosts', 'versionPaths', 'wrongAdapterDenied', 'inventoryRequired', 'originRequired',
        'manifestRequired', 'unsafePathsDenied', 'policyAndCheckpointUnchanged', 'realTrainerConstructor',
        'exactPathCompatible', 'deadlineBounded', 'claudeScopesUnambiguous',
        zero=('modelCalls', 'providerCalls')), {}),
    'context_learning_adaptive_pool.py': (_all_true('adaptive', 'moreThanFifteen'), {}),
    'context_learning_auto_staging.py': (_all_true(
        'staged_train', 'backlog_preserved', 'evaluation_no_pointer', 'revoke_blocks'),
        {'QMD_SKIP_BACKGROUND_WORKER': '1'}),
    'context_learning_cycle.py': (_no_external_calls, {}),
    'context_learning_cycle_e2e.py': (_no_external_calls, {}),
    'context_learning_teacher_budget.py': (_no_external_calls, {}),
    'context_learning_runtime_setup.py': (_no_external_calls, {}),
    'context_learning_runtime_installer.py': (_no_external_calls, {}),
    'context_learning_history_claude_directory.py': (None, {}),
    'context_learning_quality_retention.py': (_equal(
        {'aggregate': 'retained', 'project': 'retained', 'improved': 'complete'}), {}),
    'context_learning_storage_layout.py': (None, {}),
    'synthetic_rss_ceiling.py': (_equal({
        'boundary2304': 'accepted', 'measured2033': 'accepted', 'otherBoundsUnchanged': True,
        'over2305': 'setup_synthetic_measured_profile_required'}), {}),
    'context_learning_live_selector.py': (_all_true(
        'fullBodyInput', 'rankedSelection', 'semanticZero', 'invalidOutputFallback', 'timeoutFallback',
        'checkpointPin', 'corpusGate', zero=('externalCalls',)), {}),
    'context_learning_default_state_root.py': (_all_true(
        'existingStateReused', 'differentHomes', 'spaces', 'projectIsolation', 'descendantScope',
        'explicitRootCompatible', 'invalidExplicitDenied', 'unsafeRootsDenied',
        'missingPreparationUnchanged', zero=('externalCalls',)), {}),
    'context_learning_history_activation.py': (None, {}),
    'context_learning_history_readers.py': (None, {}),
    'context_learning_history_real_home_approval.py': (None, {}),
    'context_learning_inference_argv.py': (_all_true(
        'retiredAdapterRejected', 'currentAdapterHashAndViewPinned',
        'invalidAdaptersRejectedBeforeExecution', 'trainingNotResolved'), {}),
    'context_learning_project_scope.py': (None, {}),
    'context_learning_proof_cache.py': (_proof_cache, {}),
    'project_learning_recovery_lock.py': (None, {}),
    'project_learning_setup.py': (None, {}),
    'context_learning_shared_local.py': (_shared_local, {}),
    'context_learning_shared_family.py': (_all_true('independent_holdout', 'no_false_quality'), {}),
}


class ScriptChecks(FixtureCase):
    def run_script(self, name):
        check, extra = TABLE[name]
        env = dict(os.environ, HOME=str(self.home), TMPDIR=str(self.tmp),
                   MEMVANE_TEST_RUNTIME_ROOT=str(self.runtime), PYTHONPATH=str(REPO),
                   PYTHONDONTWRITEBYTECODE='1', QMD_RECALL_LOG='', **extra)
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / name)], cwd=REPO, env=env,
                                capture_output=True, text=True, timeout=HANG_LIMIT)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-8000:])
        if check is not None:
            check(self, result.stdout)


def _method(name):
    return lambda self: self.run_script(name)


for _name in TABLE:
    setattr(ScriptChecks, 'test_' + _name[:-3], _method(_name))


class TableComplete(FixtureCase):
    def test_every_script_has_a_row(self):
        self.assertEqual(sorted(p.name for p in SCRIPTS.glob('*.py')), sorted(TABLE))
