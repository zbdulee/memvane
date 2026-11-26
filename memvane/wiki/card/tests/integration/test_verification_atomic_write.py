from unittest.mock import patch

from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded
from tests.support.engines_fake import FakeEngines
from memvane.wiki.verify import api as verify


class VerificationAtomicWriteTests(FixtureCase):
    def prepare_case(self, name='project'):
        project = make_project(self.home, name=name, dirname=name, git=False)
        source = project.write_note('docs/source.md', 'Durable claim: a synthetic fact.\n')
        card = project.wiki / 'concepts/card.md'
        card.parent.mkdir(parents=True)
        body = ('---\ntitle: "Card"\nstatus: generated\ncreatedBy: memvane\n---\n\n'
                '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\n'
                '## Summary\nDurable claim: a synthetic fact.\n<!-- qmd:auto:end -->\n')
        card.write_text(body, encoding='utf-8')
        card.chmod(0o600)
        job = {'ts': '2026-07-04T00:00:00Z',
               'targetPath': card.relative_to(project.root).as_posix(),
               'sources': [{'kind': 'file', 'path': 'docs/source.md', 'collection': project.raw_name}],
               'sourceRevisions': [project.revision(source)], 'sourceHash': 'abc123',
               'engine': 'claude', 'trigger': 'post_tool_source'}
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline(project.root) as store:
            store.append_jobs('verify', [job])
        cfg = expanded(project)
        cfg['compile']['extractor']['builtins'] = ['claude']
        FakeEngines({'verify': {'verdict': 'pass', 'claims': [{'claim': 'synthetic fact',
                    'supported': True, 'quote': 'Durable claim: a synthetic fact.',
                    'sourcePath': 'docs/source.md'}], 'reasons': []}}).install(self)
        return project, cfg, card, body

    def test_failed_stamp_keeps_original_bytes_and_cleans_temporary_file(self):
        project, cfg, card, original = self.prepare_case()
        from memvane.wiki.pipeline import api as pipeline
        with patch.object(pipeline, 'publish_card', side_effect=OSError('synthetic publish failure')) as publish:
            verify.run(project.root, cfg, cfg['compile'])
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(card.read_text(encoding='utf-8'), original)
        # The next run settles the interrupted publish and reuses the recorded verdict.
        verify.run(project.root, cfg, cfg['compile'])
        self.assertIn('status: verified', card.read_text(encoding='utf-8'))
        self.assertEqual([p.name for p in card.parent.iterdir() if p.name.startswith('.')], [])

    def test_successful_stamp_preserves_the_card_mode(self):
        for mode in (0o600, 0o644):
            with self.subTest(mode=oct(mode)):
                project, cfg, card, _ = self.prepare_case('mode-%o' % mode)
                card.chmod(mode)
                verify.run(project.root, cfg, cfg['compile'])
                self.assertIn('status: verified', card.read_text(encoding='utf-8'))
                self.assertEqual(card.stat().st_mode & 0o777, mode)
