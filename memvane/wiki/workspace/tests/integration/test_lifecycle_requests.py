"""Coalesced lifecycle requests survive until the compile owner claims them."""
import os
from unittest.mock import patch

from memvane.runtime import api as runtime
from memvane.wiki.workspace import api as workspace, types as workspace_types
from memvane.wiki.workspace import lifecycle_requests
from tests.support.case import FixtureCase
from tests.support.project import make_project


class LifecycleRequests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                         'collections': [{'role': 'wiki'}]}
        env = patch.dict(os.environ, {'QMD_SYNC_STATE_DIR': str(self.home / 'scan-state')})
        env.start()
        self.addCleanup(env.stop)
        self.wake_patch = patch.object(runtime, 'request', return_value={'status': 'queued'})
        self.wake = self.wake_patch.start()
        self.addCleanup(self.wake_patch.stop)

    def test_repeated_request_coalesces_until_claim_then_rearms(self):
        self.assertEqual(workspace.request_lifecycle(self.decision, 'session_start'), 'recorded')
        self.assertEqual(workspace.request_lifecycle(self.decision, 'source_edit'), 'coalesced')
        self.wake.assert_called_once_with('laya-compile', str(self.project.root), start=False)
        claim = workspace.claim_lifecycle(self.project.root)
        self.assertEqual(claim['trigger'], 'session_start')
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))
        self.assertEqual(workspace.request_lifecycle(self.decision, 'source_edit'), 'recorded')
        self.assertEqual(workspace.claim_lifecycle(self.project.root)['trigger'], 'source_edit')
        self.assertEqual(self.wake.call_count, 2)

    def test_stale_pending_request_offers_wake_again(self):
        initial = 10**15
        later = initial + (workspace_types.STALE_SECONDS + 1) * 10**9
        with patch.object(lifecycle_requests.time, 'time_ns', return_value=initial):
            self.assertEqual(workspace.request_lifecycle(self.decision, 'session_start'), 'recorded')
        with patch.object(lifecycle_requests.time, 'time_ns', return_value=later):
            self.assertEqual(workspace.request_lifecycle(self.decision, 'session_start'), 'stale')
        self.assertEqual(self.wake.call_count, 2)
        self.assertEqual(workspace.claim_lifecycle(self.project.root)['trigger'], 'session_start')

    def test_unselected_or_no_wiki_route_never_records(self):
        for decision in ({'state': 'legacy'}, {'state': 'worktree'},
                         {**self.decision, 'collections': [{'role': 'raw'}]}):
            self.assertIsNone(workspace.request_lifecycle(decision, 'session_start'))
        self.assertIsNone(workspace.claim_lifecycle(self.project.root))
        self.wake.assert_not_called()
