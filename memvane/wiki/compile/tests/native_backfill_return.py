"""Run the source worker up to an actual private claim return boundary."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer, wiki_compile_worker as worker
from memvane.wiki.workspace import api as workspace


def run(request):
    queue = Path(request['queue'])
    bound, config, phase = request['bound'], request['config'], request['phase']
    root = Path(bound[0]['projectRoot'])
    assert root.is_relative_to(Path.home())
    settings = config['compile']
    calls = []
    answer = {'candidates': [{'title': title, 'summary': 'A durable fact about ' + title + '.'}
                             for title in ('First finding', 'Second finding')]}

    def write(project, candidate):
        title = candidate['title']
        calls.append(title)
        if phase != 'recover' and title == 'Second finding':
            return {'action': 'rejected'}
        output = io.StringIO()
        with workspace.card_write_lock(project), workspace.mutation_lock(project), redirect_stdout(output):
            code = writer._compile_locked(project, config, settings, project / config['wikiPath'], candidate, False)
        if code:
            raise AssertionError(output.getvalue())
        return json.loads(output.getvalue())

    finish = worker._commit_source_claim
    def boundary(live, claim, remaining, selected, *, stale_lines, snapshot):
        if live == queue and phase != 'recover':
            assert claim.parent != queue.parent and len(remaining) == 1
            if phase == 'after':
                finish(live, claim, remaining, selected, stale_lines=stale_lines, snapshot=snapshot)
            print(json.dumps([paid.call_count, calls]), flush=True)
            os._exit(17)
        return finish(live, claim, remaining, selected, stale_lines=stale_lines, snapshot=snapshot)

    with patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'), \
         patch.object(workspace, 'schema3_checkout', return_value=None), patch.object(workspace, 'request_sync'), \
         patch.object(workspace, 'request_compile'), patch.object(worker, 'gather_similar_pages', return_value=[]), \
         patch.object(worker, 'run_extractor', return_value=(answer, None, 0)) as paid, \
         patch.object(worker, '_commit_source_claim', side_effect=boundary), \
         patch.object(worker, 'compile_candidate', side_effect=write), patch.object(worker, '_run_verify_pass'):
        worker._main_locked(SimpleNamespace(json=False, flush_all=True), root, config, settings, queue, bound)
    return [paid.call_count, calls]


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin))))
