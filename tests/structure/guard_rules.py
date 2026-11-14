#!/usr/bin/env python3
"""Run the strict G1/G5/G9 structure detectors over a synthetic source tree.

Usage: guard_rules.py <root>. Every ``*.py`` under root is classified by path:
``memvane/wiki/<x>/...`` belongs to ``wiki/<x>``, ``memvane/<d>/...`` to ``<d>``, anything
else to ``app``. Prints ``{"findings": ["<rule>:<file>:<line>:<target>:<kind>:<name>"]}``.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys

GUARD = Path(__file__).resolve().parents[2] / 'test' / 'structure' / 'v1_guard.py'


def _guard():
    spec = importlib.util.spec_from_file_location('v1_guard', GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(path):
    parts = path.split('/')
    if parts[0] != 'memvane' or len(parts) < 3:
        return 'app'
    if parts[1] == 'wiki' and len(parts) > 3:
        return 'wiki/' + parts[2]
    return parts[1]


def main(root):
    base = Path(root).resolve()
    trees, contexts = {}, {}
    for file in sorted(base.rglob('*.py')):
        path = file.relative_to(base).as_posix()
        trees[path] = ast.parse(file.read_text(encoding='utf8'), filename=path)
        contexts[path] = _context(path)
    guard = _guard()
    findings, metrics = guard.strict_checks(trees, contexts)
    print(json.dumps({'findings': [f['rule'] + ':' + guard.strict_line(f) for f in findings],
                      'metrics': metrics}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
