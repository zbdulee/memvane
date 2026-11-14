"""Selected SessionStart runs one lifecycle pass before compile work."""
import os
from unittest.mock import patch

from memvane.host import api as host
from memvane.runtime import api as runtime
from memvane.wiki.compile import api as compile_api
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class LifecycleCompilePass(FixtureCase):
    def test_session_start_retires_backfills_and_regenerates_in_one_compile_pass(self):
        project = make_project(self.home)
        gone = project.write_note('docs/gone.md', 'source to retire\n')
        changed = project.write_note('docs/changed.md', 'before edit\n')
        legacy_source = project.write_note('docs/legacy.md', 'legacy source\n')
        marker = '<!-- qmd:auto:start id="main" sourceHash="a" -->\nfact\n<!-- qmd:auto:end -->\n'
        gone_card = project.write_card('concepts/gone.md', title='Gone', body=marker,
                                       sources=(gone,), status='generated', created_by='qmd-auto-context')
        changed_card = project.write_card('concepts/changed.md', title='Changed', body=marker,
                                          sources=(changed,), status='generated', created_by='qmd-auto-context')
        legacy = project.wiki / 'concepts/legacy.md'
        legacy.write_text('---\ntitle: Legacy\nstatus: generated\ncreatedBy: qmd-auto-context\n'
                          'sources:\n  - {kind: "file", path: "docs/legacy.md", collection: "'
                          + project.raw_name + '"}\n---\n' + marker)
        legacy_before = legacy.read_bytes()
        project.git('add', '--', '.memvane/settings.json', '.memvane/wiki/concepts/gone.md',
                    '.memvane/wiki/concepts/changed.md', '.memvane/wiki/concepts/legacy.md',
                    'docs/gone.md', 'docs/changed.md', 'docs/legacy.md')
        project.git('commit', '-qm', 'initial fixture')
        install = build_install(self, project)
        project.git('rm', '-q', '--', 'docs/gone.md')
        project.git('commit', '-qm', 'delete source')
        changed.write_text('new source revision\n')
        seen = self.home / 'extractor-called'
        fake = self.home / 'fake-extractor'
        fake.write_text('#!/usr/bin/env python3\nimport json,os\n'
                        'open(os.environ["FIXTURE_SEEN"],"a").write("called\\n")\n'
                        'print(json.dumps({"candidates": []}))\n')
        fake.chmod(0o700)
        env = {'QMD_ENGINE': 'claude', 'QMD_SYNC_STATE_DIR': str(self.home / 'scan-state'),
               'FIXTURE_SEEN': str(seen), 'QMD_WIKI_LIFECYCLE_LOG': str(self.home / 'lifecycle.log'),
               'QMD_EXTRACTOR_CLAUDE_BIN': str(fake),
               'QMD_EXTRACTOR_CODEX_BIN': str(fake), 'QMD_EXTRACTOR_HERMES_BIN': str(fake)}
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(project.root), 'session_id': 'fixture'}
        with patch.dict(os.environ, env), patch.object(runtime, 'request', return_value={'status': 'queued'}):
            host.serve_selected('update-queued', 'claude', install.select(), payload)
            result = compile_api.run(project.root)
        self.assertIn(result['state'], ('drained', 'pending'), result)
        self.assertFalse(gone_card.exists())
        self.assertEqual(len(list((project.root / '.memvane/compile/retired-wiki').glob('*.md'))), 1)
        self.assertEqual(legacy.read_bytes(), legacy_before)
        self.assertIn('schema3_selected', (self.home / 'lifecycle.log').read_text())
        self.assertTrue(changed_card.exists())
        self.assertTrue(seen.exists(), 'regeneration never reached the synthetic extractor')
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(queue.exists() and queue.read_text().strip())

    def test_rebase_in_progress_retires_nothing_and_acks_the_pass(self):
        from memvane.wiki.workspace import api as workspace
        project = make_project(self.home)
        source = project.write_note('docs/source.md', 'source\n')
        marker = '<!-- qmd:auto:start id="main" sourceHash="a" -->\nfact\n<!-- qmd:auto:end -->\n'
        card = project.write_card('concepts/card.md', title='Card', body=marker, sources=(source,),
                                  status='generated', created_by='qmd-auto-context')
        project.write_note('conflict.txt', 'base\n')
        project.git('add', '--', '.memvane/settings.json', '.memvane/wiki/concepts/card.md',
                    'docs/source.md', 'conflict.txt')
        project.git('commit', '-qm', 'initial fixture')
        install = build_install(self, project)
        project.git('checkout', '-qb', 'topic')
        project.write_note('conflict.txt', 'topic\n')
        project.git('add', '--', 'conflict.txt')
        project.git('commit', '-qm', 'topic edit')
        project.git('checkout', '-q', 'main')
        project.write_note('conflict.txt', 'main\n')
        project.git('add', '--', 'conflict.txt')
        project.git('rm', '-q', '--', 'docs/source.md')
        project.git('commit', '-qm', 'main edit and deletion')
        project.git('checkout', '-q', 'topic')
        project.git('rebase', 'main', check=False)
        try:
            self.assertTrue((project.root / '.git/rebase-merge').exists()
                            or (project.root / '.git/rebase-apply').exists())
            env = {'QMD_SYNC_STATE_DIR': str(self.home / 'scan-state'),
                   'QMD_WIKI_LIFECYCLE_LOG': str(self.home / 'lifecycle.log')}
            with patch.dict(os.environ, env), patch.object(runtime, 'request', return_value={'status': 'queued'}):
                self.assertEqual(workspace.request_lifecycle(install.select(), 'session_start'), 'recorded')
                result = compile_api.run(project.root)
                self.assertIsNone(workspace.claim_lifecycle(project.root))
            self.assertEqual(result, {'state': 'drained', 'processed': 0})
            self.assertTrue(card.exists())
            self.assertIn('in progress', (self.home / 'lifecycle.log').read_text())
        finally:
            project.git('rebase', '--abort', check=False)
