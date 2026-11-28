#!/usr/bin/env python3
"""Forward to ``entry/run.py ui``: the MemVane local web dashboard.

usage: ui.py open [--cwd DIR] [--tab overview|recall|wiki|runtime] [--no-open]
       ui.py status | stop | pref [--auto-open on|off] [--idle-minutes N]

The plugin root is this file's location, so the wrapper works without
``CLAUDE_PLUGIN_ROOT`` and without a shell.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    command = [sys.executable, '-B', str(ROOT / 'entry' / 'run.py'), 'ui', *args]
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
