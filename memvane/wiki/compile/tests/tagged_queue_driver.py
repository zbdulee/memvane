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


def raw_queue_controls(root, cfg):
    from types import SimpleNamespace

    results = []
    for channel in ('source', 'verify'):
        queue = root / ('.memvane/compile/' + channel + '-queue.jsonl')
        queue.parent.mkdir(exist_ok=True, parents=True)
        for char in ('\u0085', '\u2028', '\u2029'):
            for escaped in (False, True):
                def payload(label):
                    return {'probe': label, 'source': {'path': label}, 'targetPath': '.memvane/wiki/' + label + '.md'}
                split = dict(payload('split'), field='left' + char + 'right')
                intact = json.dumps(payload('intact')).encode() + b'\n'
                raw = json.dumps(split, ensure_ascii=escaped).encode() + b'\n' + b'{"torn":"\xc3\n' + intact
                queue.write_bytes(raw)
                def labels(path):
                    return [j['probe'] for _, j in workspace.read_queue(path) if j is not None]
                original = labels(queue)
                if channel == 'source':
                    with patch.object(worker, 'process_job', return_value=(True, False, [])) as consume, \
                         patch.object(worker, '_run_verify_pass'), patch.object(workspace, 'selected', return_value=None):
                        worker._main_locked(SimpleNamespace(json=False, flush_all=True), root, cfg, cfg['compile'], queue, None)
                    dispatched = [c.args[3]['probe'] for c in consume.call_args_list]
                else:
                    audit = queue.with_name('verify-log.jsonl')
                    before = audit.stat().st_size if audit.exists() else 0
                    verifier.run(root, cfg, cfg['compile'])
                    verdicts = [json.loads(line) for line in audit.read_bytes()[before:].splitlines()]
                    dispatched = [Path(row['targetPath']).stem for row in verdicts]
                queue.write_bytes(b'')
                orphan = queue.with_name(queue.name + '.claimed.1234567.probe')
                orphan.write_bytes(raw)
                with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '1234567'}):
                    claim = workspace.claim_queue(queue)
                recovered = labels(claim)
                pieces = sum('left' in line or 'right' in line for line, job in workspace.read_queue(claim) if job is None)
                claim.unlink()
                results.append([channel, ord(char), escaped, original, dispatched, recovered, pieces])
    return results


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
        if action == 'raw-controls':
            return raw_queue_controls(root, cfg)
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
