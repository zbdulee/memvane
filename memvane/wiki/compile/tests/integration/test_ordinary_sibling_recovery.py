"""Compare an ordinary source retry to the v1.1.0 sibling-loss baseline (F-18)."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests import ordinary_siblings
from memvane.wiki.compile.tests.integration.test_pending_handoff import archived_tag
from tests.support.case import FixtureCase


class OrdinarySiblingRecovery(FixtureCase):
    def assert_passes(self, seen):
        self.assertEqual(seen['queued'], 1)
        self.assertEqual(seen['diskErrors'], [errno.ENOSPC])
        first, second, idle = seen['passes']
        self.assertEqual((first['completed'], first['remaining']), (0, 1))
        self.assertEqual((second['completed'], second['remaining']), (1, 0))
        self.assertEqual((idle['processed'], idle['remaining']), (0, 0))
        self.assertEqual(seen['writes'][:2], [['Alpha', 'created'], ['Beta', 'write-failed']])

    def test_released_worker_loses_beta_after_retry(self):
        repo = Path(__file__).resolve().parents[5]
        bundle = archived_tag(repo, self.tmp, 'v1.1.0')
        result = subprocess.run(
            [sys.executable, '-B', ordinary_siblings.__file__], cwd=bundle, capture_output=True,
            env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))},
            encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assert_passes(seen)
        self.assertEqual(seen['extractor'], 2)
        self.assertEqual(seen['writes'][2:], [['Alpha', 'updated']])
        self.assertEqual(seen['cards'], ['concepts/alpha.md'])

    def test_native_retry_recovers_only_beta(self):
        seen = ordinary_siblings.observe(released=False)
        self.assert_passes(seen)
        if (seen['extractor'] == 2 and seen['writes'][2:] == [['Alpha', 'updated']]
                and seen['cards'] == ['concepts/alpha.md']):
            self.skipTest('F-18 (1.1.x): native retries Alpha and loses Beta as in v1.1.0')
        self.assertEqual(seen['extractor'], 1)
        self.assertEqual(seen['writes'][2:], [['Beta', 'created']])
        self.assertEqual(seen['cards'], ['concepts/alpha.md', 'concepts/beta.md'])
