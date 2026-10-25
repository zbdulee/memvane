"""Fixed one-work-item bootstrap; imports workers only from the sealed target."""
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
    if len(argv) != 6 or argv[0] not in ('laya-sync', 'laya-compile'):
        return 2
    kind, project, root, commit, bundle_sha, owner_sha = argv
    target = Path(root)
    project_path = Path(project)
    if (not target.is_absolute() or target.is_symlink()
            or str(target.resolve(strict=False)) != root
            or not project_path.is_absolute() or project_path.is_symlink()
            or str(project_path.resolve(strict=False)) != project):
        return 3
    for path in (target, project_path):
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
    # This bootstrap belongs to the invoking dispatcher. Verify target bytes
    # with its already trusted inspector before target Python can execute.
    from laya_product_bundle import inspect_full_plugin_candidate
    proof = inspect_full_plugin_candidate(target)
    if (proof['sourceCommit'] != commit or
            proof['bundleManifestSha256'] != bundle_sha or
            proof['ownershipManifestSha256'] != owner_sha or
            proof['pluginRoot'] != root):
        return 3
    # The inspector has only stdlib dependencies. Remove its cached module so
    # the target worker's strict executing-root check sees target-owned code.
    del sys.modules['laya_product_bundle']
    sys.path[0] = str(target / 'core')
    if kind == 'laya-sync':
        from laya_product_sync_worker import run
        result = run(Path(project))
    else:
        from wiki_selected_laya import run
        result = run(Path(project))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
