"""Enumerate native and released pending occurrences for consumer integration assertions."""
from memvane.wiki.workspace.handoff.api import native_live


def pending_files(queue):
    paths = [queue, *queue.parent.glob(queue.name + '.claimed.*')]
    live = native_live(queue)
    if live:
        paths.extend([live, *live.parent.glob('ready-*'), *live.parent.glob('native.claimed.*')])
    return [path for path in paths if path.exists()]


def pending_text(queue):
    return ''.join(path.read_bytes().decode('utf-8') for path in pending_files(queue))
