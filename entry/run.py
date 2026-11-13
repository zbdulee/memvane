#!/usr/bin/env python3
"""Dispatch plugin process entry points without eagerly importing domains."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[0] = str(ROOT)
# The remaining flat modules are moved into packages during S3.
sys.path.insert(1, str(ROOT / 'core'))
sys.dont_write_bytecode = True

TARGETS = {
    'hook-classify': ('core/laya_index_host_route.py', 'classify-hook'),
    'classify': ('core/laya_index_host_route.py', 'classify'),
    'onboarding-notice': ('core/laya_index_host_route.py', 'onboarding-notice'),
    'hook-serve': ('core/laya_host_response.py', 'serve-stdin'),
    'host-ack': ('core/context_learning_host_ack.py', None),
    'setup': ('core/setup_entry.py', None),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        raise SystemExit('unknown entry point')
    target, action = TARGETS[sys.argv[1]]
    path = str(ROOT / target)
    sys.argv = [path] + ([action] if action else []) + sys.argv[2:]
    runpy.run_path(path, run_name='__main__')


if __name__ == '__main__':
    main()
