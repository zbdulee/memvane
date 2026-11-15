#!/usr/bin/env python3
"""Run the strict G1/G5/G9 structure detectors over a synthetic source tree.

Usage: guard_rules.py <root>. Every ``*.py`` under root is classified by path:
``memvane/wiki/<x>/...`` belongs to ``wiki/<x>``, ``memvane/<d>/...`` to ``<d>``, anything
else to ``app``. Prints ``{"findings": ["<rule>:<file>:<line>:<target>:<kind>:<name>"]}``.

``guard_rules.py --tests <root>`` runs the test-file detectors instead: product modules
are the ``*.py`` files outside test/, tests/ and memvane/**/tests/, and the output is
``{"G8": [...], "unresolved": [...], "G1": [...], "exceptions": [...]}``. A third
argument, a JSON list of ``[test file, dotted name]`` pairs, replaces the test G1
exception table so the exception mechanism can be checked on synthetic files.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys

GUARD = Path(__file__).resolve().parent / 'v1_guard.py'


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


def _is_test(path):
    parts = path.split('/')
    return parts[0] in ('test', 'tests') or (parts[0] == 'memvane' and 'tests' in parts[1:-1])


def main(root, tests=False, exceptions=None):
    base = Path(root).resolve()
    trees, contexts = {}, {}
    for file in sorted(base.rglob('*.py')):
        path = file.relative_to(base).as_posix()
        if tests and _is_test(path):
            continue
        trees[path] = ast.parse(file.read_text(encoding='utf8'), filename=path)
        contexts[path] = _context(path)
    guard = _guard()
    if exceptions is not None:
        guard._TEST_G1_EXCEPTIONS = {tuple(pair): 'synthetic' for pair in json.loads(exceptions)}
    if tests:
        g8, unresolved, g1 = guard.test_checks(base, trees, contexts)
        print(json.dumps({'G8': g8, 'unresolved': unresolved, 'G1': g1,
                          'exceptions': sorted('%s:%s' % key for key in guard._TEST_G1_EXCEPTIONS)},
                         sort_keys=True))
        return 0
    findings, metrics = guard.strict_checks(trees, contexts)
    print(json.dumps({'findings': [f['rule'] + ':' + guard.strict_line(f) for f in findings],
                      'metrics': metrics}, sort_keys=True))
    return 0


if __name__ == '__main__':
    if sys.argv[1] == '--tests':
        sys.exit(main(sys.argv[2], tests=True, exceptions=sys.argv[3] if len(sys.argv) > 3 else None))
    sys.exit(main(sys.argv[1]))
