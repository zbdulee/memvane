"""A checkout with an unfinished Git operation holds lifecycle work."""
from pathlib import Path

from memvane.wiki.lifecycle import git_branch_safety as safety
from tests.support.case import FixtureCase


class GitOperationGuards(FixtureCase):
    def test_operation_markers_are_detected_in_checkout_and_common_git_dirs(self):
        checkout = self.home / 'checkout-git'
        common = self.home / 'common-git'
        checkout.mkdir()
        common.mkdir()
        facts = {'gitDir': checkout, 'commonDir': common}
        self.assertIsNone(safety.operation_in_progress(facts))
        for base in (checkout, common):
            for name in safety.OPERATION_MARKERS:
                with self.subTest(directory=base.name, marker=name):
                    marker = Path(base) / name
                    marker.touch()
                    self.assertEqual(safety.operation_in_progress(facts), name)
                    marker.unlink()
        self.assertIsNone(safety.operation_in_progress(facts))
