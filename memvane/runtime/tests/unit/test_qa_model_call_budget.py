import os
import threading
from unittest.mock import patch

from tests.support.case import FixtureCase
from memvane.runtime import api
from memvane.runtime import runtime_admission
from memvane.runtime.types import ResourceDeferred


class QaModelBudgetTests(FixtureCase):
    def test_limit_cannot_be_reset_or_expanded_and_corruption_fails_closed(self):
        ledger = self.tmp / 'calls.log'
        api.qa_budget_initialize(ledger, limit=6, used=4)
        with patch.dict(os.environ, {'QMD_QA_CALL_LEDGER': str(ledger), 'QMD_QA_CALL_LIMIT': '6'}):
            api.reserve_qa_budget()
            api.reserve_qa_budget()
            self.assertEqual(api.qa_budget_status(ledger, 6), 6)
            with self.assertRaisesRegex(ResourceDeferred, 'qa_call_budget_exhausted'):
                api.reserve_qa_budget()
            with patch.object(runtime_admission.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ResourceDeferred, 'qa_call_budget_exhausted'):
                    api.provider_run(['/synthetic/provider'], timeout=1)
                spawn.assert_not_called()
            with self.assertRaises(FileExistsError):
                api.qa_budget_initialize(ledger, limit=6, used=0)
            with patch.dict(os.environ, {'QMD_QA_CALL_LIMIT': '7'}):
                with self.assertRaisesRegex(ResourceDeferred, 'qa_call_budget_invalid'):
                    api.reserve_qa_budget()
        broken = self.tmp / 'broken.log'
        api.qa_budget_initialize(broken, limit=2, used=0)
        with broken.open('ab') as out:
            out.write(b'R')
        with patch.dict(os.environ, {'QMD_QA_CALL_LEDGER': str(broken), 'QMD_QA_CALL_LIMIT': '2'}):
            with self.assertRaisesRegex(ResourceDeferred, 'qa_call_budget_invalid'):
                api.reserve_qa_budget()

    def test_simultaneous_reservations_cannot_both_claim_last_call(self):
        ledger = self.tmp / 'race.log'
        api.qa_budget_initialize(ledger, limit=1, used=0)
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve():
            barrier.wait()
            try:
                api.reserve_qa_budget()
                outcomes.append('ok')
            except ResourceDeferred as error:
                outcomes.append(str(error))

        with patch.dict(os.environ, {'QMD_QA_CALL_LEDGER': str(ledger), 'QMD_QA_CALL_LIMIT': '1'}):
            workers = [threading.Thread(target=reserve) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertCountEqual(outcomes, ['ok', 'qa_call_budget_exhausted'])
        self.assertEqual(api.qa_budget_status(ledger, 1), 1)
