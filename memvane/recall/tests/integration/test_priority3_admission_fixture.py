"""Query admission refused under resource pressure: one admission, one lexical query, the card serves.

The fixture provider's query admission is the product seam; the counting
wrappers observe it and the endpoint query without changing their answers.
"""
import unittest
from unittest import mock

from memvane.recall import api as recall_api
from memvane.recall.tests.support import RecallInstallCase
from memvane.resident import api as resident

PROMPT = 'what marker does the heron protocol keep?'


class AdmissionFallback(RecallInstallCase):
    def observed(self):
        calls = {'admission': 0, 'query': []}
        real_admit, real_query = resident.admit_query, recall_api.query_endpoint

        def admit(*args, **kwargs):
            calls['admission'] += 1
            return real_admit(*args, **kwargs)

        def query(*args, **kwargs):
            calls['query'].append(kwargs.get('query_vector'))
            return real_query(*args, **kwargs)
        for target, side_effect in (('memvane.resident.api.admit_query', admit),
                                    ('memvane.recall.api.query_endpoint', query)):
            patcher = mock.patch(target, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_refused_admission_serves_the_card_from_one_lexical_query(self):
        calls = self.observed()
        context, err, event = self.recall(PROMPT)
        self.assertIn('zqheronx', context)
        self.assertEqual(calls['admission'], 1)
        self.assertEqual(calls['query'], [None])
        self.assertEqual(event['vector_fallback'], 'selected_provider_resource_deferred')
        self.assertIn('selected_provider_resource_deferred; serving lexical results', err)


if __name__ == '__main__':
    unittest.main()
