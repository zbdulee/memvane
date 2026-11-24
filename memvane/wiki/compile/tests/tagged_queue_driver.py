"""Drive one archived writer, source worker or verifier with a synthetic engine."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer, wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.verify import api as verifier
from memvane.wiki.workspace import api as workspace


def run(request):
    root = Path(request['root'])
    cfg, bound = request['config'], request['bound']
    settings = cfg['compile']
    verify_queue = root / '.memvane/compile/verify-queue.jsonl'

    def write(project, candidate):
        output = io.StringIO()
        with workspace.card_write_lock(project), workspace.mutation_lock(project), redirect_stdout(output):
            code = writer._compile_locked(project, cfg, settings, project / cfg['wikiPath'], candidate, False)
        return {'code': code, 'result': json.loads(output.getvalue()) if output.getvalue().strip() else {}}

    action = request['action']
    with patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'), \
         patch.object(workspace, 'request_sync'), patch.object(workspace, 'request_compile'):
        if action == 'write':
            with patch.object(writer, 'now_iso', return_value=request['stamp']):
                return write(root, request['card'])
        if action == 'compile':
            job = request['job']
            job['selectedRoute'] = workspace.route_identity(bound)
            candidate = {'title': 'Source observation', 'summary': 'Source observation.'}
            with patch.object(worker, 'gather_similar_pages', return_value=[]), \
                 patch.object(worker, 'run_extractor', return_value=({'candidates': [candidate]}, None, 0)) as paid, \
                 patch.object(worker, 'compile_candidate', side_effect=lambda p, c: write(p, c)['result']):
                worker.process_job(root, cfg, settings, job)
            return {'main': paid.call_count}
        if action == 'prepare':
            return verifier.prepare_queue(root, verify_queue, bound, cfg)
        if action == 'claim':
            claim = workspace.claim_queue(verify_queue)
            return claim.name
        if action == 'verify':
            answer = {'verdict': 'pass', 'claims': [{'claim': 'Decision', 'supported': True,
                      'quote': 'Newest source', 'sourcePath': 'docs/source.md'}], 'reasons': []}
            with patch.object(engines, 'run_extractor', return_value=(answer, None, 0)) as paid:
                result = verifier.run(root, cfg, settings)
            return {'calls': paid.call_count, 'remaining': result['remaining']}
        raise ValueError(action)


if __name__ == '__main__':
    request = json.load(sys.stdin)
    bundle = Path.cwd().resolve()
    assert Path.home().resolve() == Path(request['home']).resolve()
    assert Path.home().resolve() != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    with redirect_stdout(io.StringIO()):
        outcome = run(request)
    for name, module in sys.modules.copy().items():
        if name == 'memvane' or name.startswith('memvane.'):
            assert Path(module.__file__).resolve().is_relative_to(bundle), name
    print(json.dumps(outcome))
