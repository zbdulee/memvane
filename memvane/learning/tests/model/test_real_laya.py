"""Opt-in checks against an existing local Laya runtime and model (``QMD_LAYA_LIVE=1``).

They load a real model, so they stay out of the domain runner's tiers and run
only under the development guard's model-test kind, for example::

    QMD_LAYA_LIVE=1 QMD_LAYA_PYTHON=<laya python> QMD_LAYA_MODEL=<model dir> \\
      npm run dev:guard -- --kind model-test -- \\
      python3 -B -m unittest discover -s memvane/learning/tests/model -t . -p 'test_*.py'
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from memvane.learning.tests.support import REPO

HERE = Path(__file__).resolve().parent
LIVE = os.environ.get('QMD_LAYA_LIVE') == '1'
HANG_LIMIT = 180


@unittest.skipUnless(LIVE, 'set QMD_LAYA_LIVE=1 with QMD_LAYA_PYTHON and QMD_LAYA_MODEL')
class RealLaya(unittest.TestCase):
    def test_actual_synthetic_smoke_and_trainer_protocol(self):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        raw = subprocess.check_output([sys.executable, '-B', str(HERE / 'real_laya_smoke.py')], cwd=REPO,
                                      env=env, text=True, timeout=HANG_LIMIT)
        report = json.loads(raw)
        self.assertEqual(report['external_calls'], 0)
        self.assertEqual(report['runtime_selected'], 'explicit_reuse')
        self.assertGreater(report['body_bytes'], 600)
        self.assertGreater(report['head_bytes'], 0)
        self.assertTrue(all(v is True for v in report['synthetic_proof'].values()))

    def test_over_budget_cards_are_scored_from_their_leading_prefix(self):
        env = {key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR', 'QMD_LAYA_MODEL') if key in os.environ}
        env.update(PYTHONPATH=str(REPO), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        raw = subprocess.check_output([os.environ['QMD_LAYA_PYTHON'], '-s', '-B', str(HERE / 'real_laya_pairing.py')],
                                      cwd=REPO, env=env, text=True, timeout=HANG_LIMIT)
        report = json.loads(raw.strip().split('\n')[-1])
        self.assertTrue(all(n > report['max_len'] for n in report['long_card_tokens']))
        self.assertEqual(report['strict_budget_refused'], 2)
        self.assertEqual(report['pairs'], 15)
        self.assertEqual(report['truncated_pairs'], 2)
        self.assertLessEqual(report['max_pair_tokens'], report['max_len'])
        self.assertTrue(isinstance(report['selected_ids'], list) and len(report['selected_ids']) <= 3)
        self.assertIs(report['deterministic'], True)
        self.assertEqual(report['probes'], {'1024': [True], '8192': [True]})
        # Moved as-is from the old wrapper; a wall-clock bound (timing-flaky-deferred list).
        self.assertLess(report['heavy_pairing_seconds'], 1)


if __name__ == '__main__':
    unittest.main()
