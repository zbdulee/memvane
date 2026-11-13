"""Static check: names that no scope binds (NameError at call time).

Uses ``symtable`` so a name imported inside one function does not hide its
use in another. A module that rebinds globals dynamically (``import *`` or
``globals()`` writes) is reported as unchecked instead of passing silently.
Prints JSON ``{"findings": [...], "unchecked": [...]}``; exit 1 on a finding.
"""
import ast
import builtins
import json
import subprocess
import sys
import symtable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('memvane', 'entry', 'hooks', 'hermes_adapter', 'skills', 'scripts')
IMPLICIT = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__spec__', '__path__', '__builtins__',
                                 '__loader__', '__package__', '__annotations__', '__qualname__', '__module__',
                                 '__class__', '__dict__'}


def _dynamic(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == '*' for a in node.names):
            return 'star_import'
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('update', 'setdefault', '__setitem__', 'pop', '__delitem__')
                and isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == 'globals'):
            return 'globals_write'
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id == 'globals':
            return 'globals_write'
    return None


def _tables(table):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def check_source(source, filename):
    """Return (findings, unchecked_reason) for one module's source text."""
    tree = ast.parse(source, filename)
    reason = _dynamic(tree)
    if reason:
        return [], reason
    top = symtable.symtable(source, filename, 'exec')
    bound = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    for table in _tables(top):
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                bound.add(symbol.get_name())
    lines = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            lines.setdefault(node.id, []).append(node.lineno)
    findings = set()
    for table in _tables(top):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced() or name in bound or name in IMPLICIT:
                continue
            if table is top or symbol.is_global():
                at = ','.join(str(n) for n in sorted(set(lines.get(name, [0]))))
                findings.add(f'{filename}:{at}:{name}')
    return sorted(findings), None


def main(argv):
    if argv:
        files = argv
    else:
        listed = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', *SOURCES],
                                         cwd=ROOT, text=True).split()
        files = sorted(f for f in listed if f.endswith('.py') and '/tests/' not in f)
    findings, unchecked = [], []
    for name in files:
        path = ROOT / name
        if not path.is_file():
            continue
        found, reason = check_source(path.read_text(encoding='utf8'), name)
        findings.extend(found)
        if reason:
            unchecked.append(f'{name}:{reason}')
    print(json.dumps({'files': len(files), 'findings': findings, 'unchecked': unchecked}, indent=1))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
