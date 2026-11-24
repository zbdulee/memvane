"""Reproduce the public return/unlink crash window in an archived release."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import sys
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from memvane.kernel.atomic_rename import fsync_dir
from memvane.runtime import api as runtime
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace


def durable_file(path, raw):
    with path.open('xb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def prepare_return(queue, claim, phase):
    raw = claim.read_bytes()
    private = queue.parent / '.handoff/v1/case'
    private.mkdir(mode=0o700, parents=True)
    durable_file(private / 'raw', raw)
    durable_file(private / 'work', json.dumps({'remaining': ['B'], 'claim': claim.name}).encode())
    for directory in (private, private.parent, private.parent.parent, queue.parent):
        fsync_dir(directory)
    swap = queue.with_name('.return-swap')
    durable_file(swap, (json.dumps(dict(json.loads(raw), partialCompile=True)) + '\n').encode())
    os.replace(swap, queue)
    fsync_dir(queue.parent)
    durable_file(private / 'committed', claim.name.encode())
    fsync_dir(private)
    if phase == 'after-unlink':
        claim.unlink()
        fsync_dir(queue.parent)


def process_refresh(queue, bound, config, invalid):
    from memvane.wiki.compile import wiki_compile as writer, wiki_compile_worker as worker
    root = Path(bound[0]['projectRoot'])
    settings = config['compile']
    candidate = {'title': 'Old refresh', 'summary': 'Public decision.'}

    def write(project, card):
        output = io.StringIO()
        with workspace.card_write_lock(project), workspace.mutation_lock(project), redirect_stdout(output):
            status = writer._compile_locked(project, config, settings, project / config['wikiPath'], card, False)
        assert status == 0, output.getvalue()
        return json.loads(output.getvalue())

    def provider(argv, **kwargs):
        output = 'invalid JSON' if invalid else json.dumps({'candidates': [candidate]})
        return subprocess.run([sys.executable, '-c', 'import sys; sys.stdin.read(); print(sys.argv[1])', output],
                              **kwargs)

    report = io.StringIO()
    with patch.object(workspace, 'schema3_checkout', return_value=None), patch.object(workspace, 'request_sync'), \
         patch.object(workspace, 'request_compile'), patch.object(worker, '_run_verify_pass'), \
         patch.object(worker, 'gather_similar_pages', return_value=[]), \
         patch.object(runtime, 'provider_run', side_effect=provider) as paid, \
         patch.object(worker, 'compile_candidate', side_effect=write), \
         patch.object(intake, 'settle_job', wraps=intake.settle_job) as settle, redirect_stdout(report):
        status = worker._main_locked(SimpleNamespace(json=True, flush_all=True), root, config, settings, queue, bound)
    assert status == 0, report.getvalue()
    return {'processed': json.loads(report.getvalue())['processed'], 'extracts': paid.call_count,
            'settlements': [call.args[1] for call in settle.call_args_list]}


def main():
    request = json.load(sys.stdin)
    phase = request['phase']
    assert phase in ('before-unlink', 'after-unlink', 'consume', 'refresh', 'record-edit', 'dispatch')
    home = Path.home().resolve()
    assert home == Path(request['home']).resolve()
    account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert not home.is_relative_to(account)
    queue = Path(request['queue'])
    assert queue.resolve().is_relative_to(home)
    bound = request['bound']
    with patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'):
        if phase == 'record-edit':
            from memvane.wiki.card import api as card
            result = card.record_pending_refresh(Path(bound[0]['projectRoot']), 'one.md', 'codex')
        elif phase == 'dispatch':
            from memvane.settings import api as settings_api
            decision = bound[0]
            seen = request['snapshot']
            intake.seed_source_snapshot(decision, seen, generation='g1')
            grant = intake.record_grant(decision, 'sync', engine='codex')
            assert intake.activate_grant(decision, 'sync', grant)
            current = {'config': request['config'], 'projectRoot': decision['projectRoot']}
            with patch.object(settings_api, 'find_project_config', return_value=current), \
                 patch.object(settings_api, 'load_project_config', return_value=request['config']), \
                 patch.object(workspace, 'request_compile'):
                result = intake.drain_after_sync(decision, intake.observe_grants(decision),
                                                 recheck=lambda: None, view_generation='g1', raw_snapshot=seen)
        elif phase == 'refresh':
            result = process_refresh(queue, bound, request['config'], request.get('invalid', False))
        else:
            claim = workspace.claim_queue(queue, receipt_writer=intake.write_discard_receipt)
            if phase == 'consume':
                intake.reconcile_discards(bound)
                result = [row for _, row in workspace.read_queue(claim)]
            else:
                prepare_return(queue, claim, phase)
                result = {'claim': claim.name, 'pid': os.getpid()}
    for name, module in sys.modules.copy().items():
        if name == 'memvane' or name.startswith('memvane.'):
            assert Path(module.__file__).resolve().is_relative_to(Path.cwd()), name
    print(json.dumps(result), flush=True)
    if phase in ('before-unlink', 'after-unlink'):
        os._exit(17)


if __name__ == '__main__':
    main()
