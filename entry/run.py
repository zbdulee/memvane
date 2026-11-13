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
    'hook-classify': ('memvane/route/index_host_route.py', 'classify-hook'),
    'classify': ('memvane/route/index_host_route.py', 'classify'),
    'onboarding-notice': ('memvane/route/index_host_route.py', 'onboarding-notice'),
    'hook-serve': ('memvane/host/host_response.py', 'serve-stdin'),
    'host-ack': ('memvane/learning/context_learning_host_ack.py', None),
    'setup': ('memvane/app/setup_entry.py', None),
    'wake': ('memvane/app/wake_entry.py', None),
    'selector-maintenance': ('memvane/learning/selector_maintenance.py', None),
    'learning-shared': ('memvane/learning/context_learning_shared.py', None),
    'prepare-inference': ('memvane/learning/prepare_laya_inference.py', None),
    'resident-service': ('memvane/resident/resident_service.py', None),
    'runtime-work': ('memvane/runtime/runtime_work.py', None),
    'runtime-monitor': ('memvane/runtime/runtime_monitor.py', None),
    'wiki-compile': ('memvane/wiki/compile/wiki_compile.py', None),
    'wiki-compile-worker': ('memvane/wiki/compile/wiki_compile_worker.py', None),
    'wiki-extract': ('memvane/wiki/compile/wiki_extract.py', None),
    'wiki-dedup-resolve': ('memvane/wiki/dedup/wiki_dedup_resolve.py', None),
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
