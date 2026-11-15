"""Fixed one-work-item bootstrap of the executing plugin."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading


def _watch_owner_pipe():
    """An inherited stdin pipe closes when the UID worker dies unexpectedly."""
    while True:
        try:
            if not os.read(0, 1):
                break
        except OSError:
            break
    # The dispatcher starts this bootstrap in its own session. Signal only
    # that exact process group, including a synchronous nested compile child.
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGTERM)


def main(argv):
    if len(argv) != 2 or argv[0] not in ('laya-sync', 'laya-compile'):
        return 2
    kind, project = argv
    project_path = Path(project)
    if (not project_path.is_absolute() or project_path.is_symlink()
            or str(project_path.resolve(strict=False)) != project):
        return 3
    for path in (project_path,):
        try:
            info = path.stat()
        except OSError:
            return 3
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return 3
    if os.environ.get('QMD_LAYA_WAKE_OWNER_PIPE') == '1':
        if os.getpgrp() != os.getpid() or not stat.S_ISFIFO(os.fstat(0).st_mode):
            return 3
        threading.Thread(target=_watch_owner_pipe, daemon=True).start()
    if kind == 'laya-sync':
        from memvane.sync.api import run
        result = run(Path(project))
    else:
        from memvane.wiki.compile.api import run
        result = run(Path(project))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
