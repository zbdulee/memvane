"""Reject direct SQLite read-only URI modes in product Python code."""
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
DIRECT_MODE = re.compile(r'\bmode\s*=\s*ro\b', re.IGNORECASE)
# The journal's _open must refuse hot rollback recovery in hook/read-only paths.
JOURNAL_EXCEPTION = ROOT / 'memvane/journal/install_journal.py'


def inspect(path):
    tree = ast.parse(path.read_text(encoding='utf8'), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    allowed = set()
    if path.resolve() == JOURNAL_EXCEPTION:
        for function in tree.body:
            if isinstance(function, ast.FunctionDef) and function.name == '_open':
                allowed.update(id(node) for node in ast.walk(function)
                               if isinstance(node, ast.Constant) and node.value == '?mode=ro')
    findings, exceptions = [], []
    for node in ast.walk(tree):
        if (not isinstance(node, ast.Constant) or not isinstance(node.value, str)
                or id(node) in docstrings or not DIRECT_MODE.search(node.value)):
            continue
        (exceptions if id(node) in allowed else findings).append(f'{path}:{node.lineno}')
    return findings, exceptions


def product_files():
    for name in ('memvane', 'entry', 'hermes_adapter', 'hooks', 'skills', 'scripts'):
        root = ROOT / name
        if root.exists():
            for path in sorted(root.rglob('*.py')):
                if 'tests' not in path.relative_to(root).parts:
                    yield path


def main():
    files = [Path(arg) for arg in sys.argv[1:]] or list(product_files())
    results = [inspect(path) for path in files]
    findings = [item for rows, _ in results for item in rows]
    exceptions = [item for _, rows in results for item in rows]
    print(json.dumps({'files': len(files), 'findings': findings, 'exceptions': exceptions}))
    return bool(findings)


if __name__ == '__main__':
    sys.exit(main())
