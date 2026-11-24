"""Run a writer or recovery in its own process and stop at the actual queue rename."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace


def main(request):
    root = Path(request['root'])
    assert root.resolve().is_relative_to(Path.home().resolve())
    bound, config = request['bound'], request['config']
    queue = root / '.memvane/compile/verify-queue.jsonl'
    phase = request['phase']
    replace = os.replace

    def boundary(src, dst, *args, **kwargs):
        if Path(dst) == queue and phase == 'before':
            os._exit(17)
        replace(src, dst, *args, **kwargs)
        if Path(dst) == queue and phase == 'after':
            os._exit(17)

    with patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'), \
         patch.object(workspace, 'request_sync'), patch.object(os, 'replace', side_effect=boundary):
        if phase == 'recover':
            return verify.prepare_queue(root, queue, bound, config)
        with workspace.card_write_lock(root), workspace.mutation_lock(root), redirect_stdout(io.StringIO()):
            return writer._compile_locked(root, config, config['compile'], root / config['wikiPath'],
                                          request['card'], False)


if __name__ == '__main__':
    print(json.dumps(main(json.load(sys.stdin))))
