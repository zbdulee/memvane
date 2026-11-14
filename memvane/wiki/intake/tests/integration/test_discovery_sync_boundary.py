"""Selected hook grants and sync discovery share the same source cursor."""
import hashlib
import json
from unittest.mock import patch

from memvane.host import api as host
from memvane.runtime import api as runtime
from memvane.wiki.intake import wiki_discovery as discovery
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class DiscoverySyncBoundary(FixtureCase):
    def test_ungranted_revision_waits_for_a_successful_bash_grant(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('changed source\n')
        first = install.run_sync()
        self.assertIn(first['state'], ('indexed', 'unchanged'), first)
        cursor = discovery._paths(install.select())[0]
        key = project.raw_name + '/docs/note.md'
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(discovery._read(cursor)['pending'], {key: revision})
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(queue.exists() and queue.read_text().strip())
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', install.select(), payload), {})
        self.assertEqual(len(discovery.observe_grants(install.select())['grants']['sync']), 1)
        second = install.run_sync()
        self.assertIn(second['state'], ('indexed', 'unchanged'), second)
        self.assertEqual(discovery._read(cursor)['pending'], {})
        jobs = [json.loads(line) for line in queue.read_text().splitlines()]
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['source']['path'], jobs[0]['engine']), ('docs/note.md', 'claude'))
        self.assertEqual(discovery.observe_grants(install.select())['grants']['sync'], [])
