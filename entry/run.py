#!/usr/bin/env python3
"""Dispatch plugin process entry points without eagerly importing domains."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[0] = str(ROOT)
sys.dont_write_bytecode = True

TARGETS = {
    'hook-classify': ('memvane.route.index_host_route', 'classify-hook'),
    'classify': ('memvane.route.index_host_route', 'classify'),
    'onboarding-notice': ('memvane.route.index_host_route', 'onboarding-notice'),
    'hook-serve': ('memvane.host.host_response', 'serve-stdin'),
    'host-ack': ('memvane.learning.context_learning_host_ack', None),
    'setup': ('memvane.app.setup_entry', None),
    'wake': ('memvane.app.wake_entry', None),
    'selector-maintenance': ('memvane.learning.selector_maintenance', None),
    'learning-shared': ('memvane.learning.context_learning_shared', None),
    'prepare-inference': ('memvane.learning.prepare_laya_inference', None),
    'resident-service': ('memvane.resident.resident_service', None),
    'runtime-work': ('memvane.runtime.runtime_work', None),
    'runtime-monitor': ('memvane.runtime.runtime_monitor', None),
    'extractor-claude': ('memvane.wiki.engines.extractors.claude_adapter', None),
    'extractor-codex': ('memvane.wiki.engines.extractors.codex_adapter', None),
    'extractor-hermes': ('memvane.wiki.engines.extractors.hermes_adapter', None),
    'wiki-compile': ('memvane.wiki.compile.wiki_compile', None),
    'wiki-compile-worker': ('memvane.wiki.compile.wiki_compile_worker', None),
    'wiki-extract': ('memvane.wiki.compile.wiki_extract', None),
    'wiki-dedup-resolve': ('memvane.wiki.dedup.wiki_dedup_resolve', None),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        raise SystemExit('unknown entry point')
    target, action = TARGETS[sys.argv[1]]
    sys.argv = [target] + ([action] if action else []) + sys.argv[2:]
    runpy.run_module(target, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
