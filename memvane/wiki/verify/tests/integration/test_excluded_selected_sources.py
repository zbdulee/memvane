"""Selected verification never sends newly excluded sources to a host."""
import hashlib
import json
from unittest.mock import patch

from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project


class ExcludedSelectedSources(FixtureCase):
    def test_excluded_job_and_crlf_mixed_card_skip_before_snapshot(self):
        project = make_project(self.home, git=False)
        decision = {'recallSettings': {'skipPaths': ['build']}}
        bound = (decision, {'productRouteAllowed': True})
        def revision(path):
            return {'kind': 'file', 'path': path, 'collection': 'raw',
                    'sha256': hashlib.sha256(path.encode()).hexdigest(),
                    'size': len(path), 'mtimeNs': 1}
        included = revision('docs/keep/a.md')
        excluded = revision('docs/build/b.md')
        mixed = ('---\r\ntitle: Card\r\nstatus: generated\r\ncreatedBy: memvane\r\nsourceRevisions:\r\n  - '
                 + json.dumps(included) + '\r\n  - ' + json.dumps(excluded) + '\r\n---\r\n# Card\r\n')
        plain = '---\ntitle: Card\nstatus: generated\ncreatedBy: memvane\n---\n# Card\n'
        cases = [({'targetPath': '.memvane/wiki/mixed.md', 'sourceRevisions': [excluded]}, plain),
                 ({'targetPath': '.memvane/wiki/mixed.md', 'sourceRevisions': [included],
                   'sources': [{'kind': 'file', 'path': included['path']}]}, mixed)]
        card = project.root / '.memvane/wiki/mixed.md'
        card.parent.mkdir(parents=True, exist_ok=True)
        for job, text in cases:
            card.write_bytes(text.encode('utf-8'))
            with self.subTest(job=job), patch.object(worker, 'card_fields', return_value=(
                    {'createdBy': 'qmd-auto-context'}, 'generated', '')), \
                 patch.object(worker, 'snapshot_revision_evidence') as snapshot, \
                 patch.object(worker, 'log_verdict') as log:
                result = worker.process_row(project.root, {}, {}, {}, None, 'tok', {'jobId': 'j', 'payload': job},
                                            self.home / 'verify.log', bound)
            self.assertEqual(result, ('done', 'source_folder_excluded', True))
            self.assertEqual(log.call_args.args[1]['reason'], 'source_folder_excluded')
            snapshot.assert_not_called()

    def test_reindex_wakes_only_the_selected_sync(self):
        project = make_project(self.home, git=False)
        bound = ({'projectRoot': str(project.root)}, {'productRouteAllowed': True})
        with patch.object(workspace, 'find_wiki_collection', return_value=('wiki', '.memvane/wiki')), \
             patch.object(workspace, 'selected', return_value=bound), \
             patch.object(workspace, 'request_sync') as sync:
            worker.reindex_wiki(project.root, {})
        sync.assert_called_once_with(project.root, bound)
