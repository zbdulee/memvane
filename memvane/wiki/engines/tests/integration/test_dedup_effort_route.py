from tests.support.case import FixtureCase
from tests.support.engines_fake import FakeEngines
from tests.support.project import make_project
from tests.support.settings_fake import expanded
from memvane.wiki.dedup import api as dedup


class DedupEffortRouteTests(FixtureCase):
    def test_semantic_effort_reaches_engine_and_diagnostic_audit(self):
        project = make_project(self.home, git=False)
        cfg = expanded(project)['compile']
        cfg['extractor']['builtins'] = ['claude']
        cfg['reasoningEffort']['engines'] = {'claude': {'semanticDedup': 'xhigh'}}
        observed = []
        def response(payload, argv):
            observed.append(payload['_qmd']['reasoningEffort']['requested'])
            return {'verdict': 'distinct', 'reason': 'different',
                    '_qmd': {'reasoningEffort': {'requested': 'xhigh', 'applied': 'xhigh',
                                                'status': 'applied', 'reason': 'capability_flag'}}}
        fake = FakeEngines({'semantic_dedup': response, 'dedup': response}).install(self)
        verdict, info = dedup.judge_pair(project.root, cfg,
                                         {'path': 'a', 'content': 'A'},
                                         {'path': 'b', 'content': 'B'}, engine='claude')
        self.assertEqual(verdict, 'distinct', info)
        self.assertEqual(observed, ['xhigh'])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(info['_qmd']['reasoningEffort']['status'], 'applied')
        self.assertEqual(dedup.diagnostics(info)['_qmd']['reasoningEffort']['status'], 'applied')
        cfg['extractor']['builtins'] = []
        cfg['extractor']['backends'] = {'claude': ['python3', 'stub']}
        verdict, info = dedup.judge_pair(project.root, cfg,
                                         {'path': 'a', 'content': 'A'},
                                         {'path': 'b', 'content': 'B'}, engine='claude')
        self.assertIsNone(verdict)
        self.assertEqual(info['reason'], 'producing_engine_unavailable')
        self.assertEqual(observed, ['xhigh'])
        self.assertEqual(len(fake.calls), 1)
