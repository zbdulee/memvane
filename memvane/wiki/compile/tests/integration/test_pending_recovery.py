"""A pending refresh is recovered after the separate queue append crashes."""
import json

from memvane.wiki.card import api as card
from memvane.wiki.workspace import api as workspace
from memvane.wiki.compile import wiki_compile_worker as worker
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class PendingRecovery(FixtureCase):
    def test_pending_only_is_enqueued_and_replays_coalesce(self):
        project = make_project(self.home, git=False)
        project.write_note('docs/source.md', 'synthetic source\n')
        config = expanded(project)
        pending = card.record_pending_refresh(project.root, 'docs/source.md', 'claude')
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertEqual(worker.recover_pending_refreshes(project.root, config, queue), 1)
        self.assertEqual(worker.recover_pending_refreshes(project.root, config, queue), 1)
        self.assertFalse(queue.exists())
        native = workspace.safe_queue_path(project.root, native=True)
        lines = native.read_text().splitlines()
        kept, _dropped = worker.dedup_jobs([(line, json.loads(line)) for line in lines])
        self.assertEqual(len(kept), 1)
        row = kept[0][1]
        self.assertEqual(row['source']['path'], 'docs/source.md')
        self.assertEqual(row['pendingRefresh']['eventId'], pending['eventId'])
        self.assertEqual(row['ts'], pending['ts'])
