"""Module-level import closure of the hook entry modules (structure X-1).

Follows only imports that run when a module is imported: statements at module
level and inside module-level ``if``/``try``/``with`` blocks and class bodies.
Imports inside functions are lazy and are not followed. Importing ``a.b.c``
also runs the ``a`` and ``a.b`` package ``__init__`` files.

usage: hook_import_graph.py [--root DIR] [--entry MODULE ...] [--forbid MODULE ...]
Prints JSON ``{entries, reached, forbidden}``; exit 1 when a forbidden module is reached.
"""
import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ('memvane.host.host_response', 'memvane.host.route_input',
           'memvane.host.onboarding_notice')
FORBIDDEN = ('memvane.learning.context_learning.teacher_hosts',
             'memvane.learning.context_learning.teacher_isolation',
             'memvane.learning.context_learning.teacher_model_tiers',
             'memvane.learning.default_teacher_enable',
             'memvane.learning.context_learning.general_teacher',
             'memvane.learning.context_learning.default_teacher')
_DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _path(root, name):
    base = root.joinpath(*name.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


def _eager_imports(tree):
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _DEFERRED):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt) or isinstance(child, ast.excepthandler):
                stack.append(child)


def _targets(root, name, path, node):
    if isinstance(node, ast.Import):
        return [item.name for item in node.names]
    base = node.module or ''
    if node.level:
        package = name.split('.') if path.name == '__init__.py' else name.split('.')[:-1]
        package = package[:len(package) - (node.level - 1)]
        base = '.'.join(package + ([base] if base else []))
    found = [base] if base else []
    for item in node.names:
        child = base + '.' + item.name if base else item.name
        if _path(root, child) is not None:
            found.append(child)
    return found


def closure(root, entries):
    reached, pending = set(), list(entries)
    while pending:
        name = pending.pop()
        parts = name.split('.')
        for index in range(1, len(parts) + 1):
            module = '.'.join(parts[:index])
            if module in reached:
                continue
            path = _path(root, module)
            if path is None:
                continue
            reached.add(module)
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in _eager_imports(tree):
                pending.extend(target for target in _targets(root, module, path, node)
                               if target and target != '__future__')
    return reached


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--entry', action='append')
    parser.add_argument('--forbid', action='append')
    args = parser.parse_args(argv)
    root = Path(args.root)
    entries = args.entry or list(ENTRIES)
    forbidden = args.forbid or list(FORBIDDEN)
    reached = closure(root, entries)
    hit = sorted(set(forbidden) & reached)
    print(json.dumps({'entries': entries, 'reached': sorted(reached), 'forbidden': hit}))
    return 1 if hit else 0


if __name__ == '__main__':
    sys.exit(main())
