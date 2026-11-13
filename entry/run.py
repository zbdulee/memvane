#!/usr/bin/env python3
"""Dispatch one process command through a lazy module:function table."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[0] = str(ROOT)
sys.dont_write_bytecode = True

# Each row is (module:function, prepended arguments, pass argv to function).
TARGETS = {
    'hook-classify': ('memvane.route.index_host_route:main', ('classify-hook',), True),
    'classify': ('memvane.route.index_host_route:main', ('classify',), True),
    'onboarding-notice': ('memvane.route.index_host_route:main', ('onboarding-notice',), True),
    'hook-serve': ('memvane.host.host_response:main', ('serve-stdin',), True),
    'host-ack': ('memvane.app.entry_commands:host_ack', (), False),
    'setup': ('memvane.app.setup_entry:main', (), True),
    'wake': ('memvane.app.wake_entry:main', (), True),
    'selector-maintenance': ('memvane.app.entry_commands:selector_maintenance', (), False),
    'learning-shared': ('memvane.app.entry_commands:learning_shared', (), False),
    'prepare-inference': ('memvane.learning.prepare_laya_inference:main', (), False),
    'resident-service': ('memvane.resident.resident_service:main', (), True),
    'runtime-work': ('memvane.app.entry_commands:runtime_work', (), False),
    'runtime-monitor': ('memvane.runtime.runtime_monitor:main', (), True),
    'extractor-claude': ('memvane.wiki.engines.extractors.claude_adapter:main', (), False),
    'extractor-codex': ('memvane.wiki.engines.extractors.codex_adapter:main', (), False),
    'extractor-hermes': ('memvane.wiki.engines.extractors.hermes_adapter:main', (), False),
    'registry': ('memvane.app.entry_commands:registry', (), False),
    'wiki-card-budget': ('memvane.wiki.compile.wiki_card_budget:main', (), False),
    'wiki-compile': ('memvane.wiki.compile.wiki_compile:main', (), False),
    'wiki-compile-worker': ('memvane.wiki.compile.wiki_compile_worker:main', (), False),
    'wiki-extract': ('memvane.wiki.compile.wiki_extract:main', (), False),
    'wiki-lifecycle': ('memvane.wiki.lifecycle.wiki_lifecycle:main', (), False),
    'wiki-dedup-resolve': ('memvane.wiki.dedup.wiki_dedup_resolve:main', (), False),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        raise SystemExit('unknown entry point')
    target, prefix, pass_argv = TARGETS[sys.argv[1]]
    module_name, function_name = target.split(':', 1)
    argv = [*prefix, *sys.argv[2:]]
    sys.argv = [str(ROOT / (module_name.replace('.', '/') + '.py')), *argv]
    function = getattr(importlib.import_module(module_name), function_name)
    return function(argv) if pass_argv else function()


if __name__ == '__main__':
    raise SystemExit(main())
