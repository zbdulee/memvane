"""Reproduce the public return/unlink crash window in an archived release."""
import json
import os
from pathlib import Path
import pwd
import sys
from unittest.mock import patch

from memvane.kernel.atomic_rename import fsync_dir
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


def main():
    request = json.load(sys.stdin)
    phase = request['phase']
    assert phase in ('before-unlink', 'after-unlink', 'consume')
    home = Path.home().resolve()
    assert home == Path(request['home']).resolve()
    account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert not home.is_relative_to(account)
    queue = Path(request['queue'])
    assert queue.resolve().is_relative_to(home)
    bound = request['bound']
    with patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'):
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
    if phase != 'consume':
        os._exit(17)


if __name__ == '__main__':
    main()
