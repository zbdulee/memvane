import json
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded
from tests.support.engines_fake import FakeEngines
from memvane.wiki.verify import api as verify


class VerifyEffortRouteTests(FixtureCase):
    def test_verify_sends_selected_engine_effort_to_fake_extractor(self):
        project = make_project(self.home, git=False)
        source = project.write_note('docs/source.md', 'Durable claim: a synthetic fact.\n')
        card = project.wiki / 'concepts/card.md'
        card.parent.mkdir(parents=True)
        card.write_text('---\ntitle: "Card"\nstatus: generated\ncreatedBy: memvane\n---\n\n'
                        '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\n'
                        '## Summary\nDurable claim: a synthetic fact.\n<!-- qmd:auto:end -->\n')
        queue = project.root / '.memvane/compile/verify-queue.jsonl'
        queue.parent.mkdir(parents=True)
        relative = card.relative_to(project.root).as_posix()
        job = {'ts': '2026-07-04T00:00:00Z', 'targetPath': relative,
               'sources': [{'kind': 'file', 'path': 'docs/source.md', 'collection': project.raw_name}],
               'sourceRevisions': [project.revision(source)], 'sourceHash': 'abc123',
               'engine': 'claude', 'trigger': 'post_tool_source'}
        queue.write_text(json.dumps(job) + '\n', encoding='utf-8')
        cfg = expanded(project)
        cfg['compile']['extractor']['builtins'] = ['claude']
        seen = []
        def response(payload, _argv):
            seen.append(payload['_qmd']['reasoningEffort'])
            return {'verdict': 'pass', 'claims': [{'claim': 'synthetic fact', 'supported': True}],
                    'reasons': []}
        fake = FakeEngines({'verify': response}).install(self)
        result = verify.run(project.root, cfg, cfg['compile'])
        self.assertEqual(result['processed'], 1, result)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]['task'], 'verify')
        self.assertEqual(fake.calls[0]['engine'], 'claude')
        self.assertEqual(seen[0]['requested'], cfg['compile']['reasoningEffort']['verify'])
