import json
import os
from unittest.mock import patch

from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded
from tests.support.engines_fake import FakeEngines
from memvane.wiki.verify import api as verify


class VerificationAtomicWriteTests(FixtureCase):
    def prepare_case(self):
        project = make_project(self.home, git=False)
        source = project.write_note('docs/source.md', 'Durable claim: a synthetic fact.\n')
        card = project.wiki / 'concepts/card.md'
        card.parent.mkdir(parents=True)
        body = ('---\ntitle: "Card"\nstatus: generated\ncreatedBy: memvane\n---\n\n'
                '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\n'
                '## Summary\nDurable claim: a synthetic fact.\n<!-- qmd:auto:end -->\n')
        card.write_text(body, encoding='utf-8')
        card.chmod(0o600)
        queue = project.root / '.memvane/compile/verify-queue.jsonl'
        queue.parent.mkdir(parents=True)
        job = {'ts': '2026-07-04T00:00:00Z',
               'targetPath': card.relative_to(project.root).as_posix(),
               'sources': [{'kind': 'file', 'path': 'docs/source.md', 'collection': project.raw_name}],
               'sourceRevisions': [project.revision(source)], 'sourceHash': 'abc123',
               'engine': 'claude', 'trigger': 'post_tool_source'}
        queue.write_text(json.dumps(job) + '\n', encoding='utf-8')
        cfg = expanded(project)
        cfg['compile']['extractor']['builtins'] = ['claude']
        FakeEngines({'verify': {'verdict': 'pass', 'claims': [{'claim': 'synthetic fact',
                    'supported': True, 'quote': 'Durable claim: a synthetic fact.',
                    'sourcePath': 'docs/source.md'}], 'reasons': []}}).install(self)
        return project, cfg, card, body

    def test_failed_stamp_keeps_original_bytes_and_cleans_temporary_file(self):
        project, cfg, card, original = self.prepare_case()
        real_replace = os.replace
        attempted = []
        def fail_card(src, dst, *args, **kwargs):
            if str(dst) == str(card):
                attempted.append(str(dst))
                raise OSError('synthetic write failure')
            return real_replace(src, dst, *args, **kwargs)
        with patch('os.replace', side_effect=fail_card):
            verify.run(project.root, cfg, cfg['compile'])
        self.assertEqual(attempted, [str(card)])
        self.assertEqual(card.read_text(encoding='utf-8'), original)
        self.assertEqual(list(card.parent.glob('.card.md.tmp-*')), [])

    def test_successful_stamp_preserves_private_mode(self):
        project, cfg, card, _ = self.prepare_case()
        verify.run(project.root, cfg, cfg['compile'])
        self.assertIn('status: verified', card.read_text(encoding='utf-8'))
        self.assertEqual(card.stat().st_mode & 0o777, 0o600)
