"""A bounded similarity round survives a worker restart without a hot loop."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import topical_hook_queue as queue
import setup_guard
import topical_story_scope

with tempfile.TemporaryDirectory(prefix='qmd-review-resume-') as name:
    root = Path(name).resolve()
    jobs = root / queue.QUEUE
    jobs.mkdir(mode=0o700)
    queue._atomic(jobs / 'first.json', {'schema': 'topical-hook-job-v1',
        'action': 'reconcile', 'turnKey': 'recovery'})
    calls = []
    wakes = []
    def run(_root, _job):
        calls.append('run')
        if len(calls) <= 2:
            return {'status': 'pending_review',
                    'reason': 'similarity_review_deferred',
                    'reviewedThisPass': 8}
        return {'status': 'no_net_changes'}
    with patch.object(queue, '_root', return_value=root), \
         patch.object(queue, '_run', side_effect=run), \
         patch.object(queue, '_spawn_worker', side_effect=lambda _: wakes.append('wake')), \
         patch.object(setup_guard, 'status', return_value={'status': 'ready'}), \
         patch.object(topical_story_scope, 'exact_story', return_value=False):
        queue.worker(root)
        assert len(calls) == 1 and len(wakes) == 1
        first_successor = [p.name for p in jobs.glob('*.json')]
        assert len(first_successor) == 1 and first_successor[0] != 'first.json'
        queue.worker(root)
        second_successor = [p.name for p in jobs.glob('*.json')]
        assert len(calls) == 2 and len(second_successor) == 1
        assert second_successor != first_successor
        queue.worker(root)
    assert len(calls) == 3
    assert not list(jobs.glob('*.json'))
    status = json.loads((root / 'topical-hook-status.json').read_text())
    assert status['result']['status'] == 'no_net_changes'
print(json.dumps({'durableSuccessor': True, 'noHotLoop': True,
                  'settledAfterRestart': True}))
