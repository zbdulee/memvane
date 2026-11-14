from unittest.mock import patch
import os

from memvane.kernel import hook_budget

from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from memvane.host import api as host
from memvane.recall.types import HostResponseError, RecallAnswer


class RecallRetryEventTests(FixtureCase):
    def test_recall_reselection_records_one_event(self):
        project = make_project(self.home, git=False)
        install = build_install(self, project)
        decision = install.decision
        from memvane.journal import api as journal
        fresh = journal.select_route(str(project.root), action='recall')
        for key in ('state', 'projectRoot', 'installId', 'scopeId', 'journalPath', 'storePath', 'storeIncarnation'):
            self.assertEqual(fresh.get(key), decision.get(key), (key, fresh, decision))
        before = len([r for r in install.events() if r.get('event') == 'recall'])
        proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        with patch.object(hook_budget, '_deadline', None), \
             patch.dict(os.environ, {'QMD_HOOK_DEADLINE_MONO': ''}), \
             patch('memvane.journal.api.verify_selected_route',
                   side_effect=[HostResponseError('selected_proof_changed'), proof]) as verify, \
             patch('memvane.recall.api.serve_endpoint_recall', return_value=RecallAnswer(decision)):
            host.serve_selected('recall', 'claude', decision,
                                {'prompt': 'synthetic question', 'cwd': str(project.root)})
        self.assertEqual(verify.call_count, 2)
        rows = [r for r in install.events() if r.get('event') == 'recall'][before:]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['retry'], 'selected_proof_changed')
        self.assertIn('laya_recall_proof_retry', rows[0]['notes'])
        self.assertEqual(rows[0]['outcome'], 'empty')
