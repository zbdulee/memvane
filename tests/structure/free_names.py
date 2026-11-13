"""Static check: names that no scope binds (NameError at call time) and
``module.name`` reads where the product module binds no ``name`` (AttributeError).

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


def _module_file(dotted):
    base = ROOT.joinpath(*dotted.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


_SYMBOLS = {}


def _symbols(path):
    """Names a module binds anywhere, or None when it rebinds globals dynamically."""
    if path not in _SYMBOLS:
        tree = ast.parse(path.read_text(encoding='utf8'), str(path))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split('.')[0] for a in node.names)
            elif isinstance(node, ast.Global):
                names.update(node.names)
        if path.name == '__init__.py':
            names.update(p.stem for p in path.parent.iterdir())
        _SYMBOLS[path] = None if _dynamic(tree) else names
    return _SYMBOLS[path]


def check_attributes(source, filename):
    """``alias.name`` where ``alias`` is a product module that binds no ``name``."""
    tree = ast.parse(source, filename)
    findings = set()

    def aliases_of(node):
        found = {}
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname and item.name.startswith('memvane.'):
                    found[item.asname] = item.name
        elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or '').startswith('memvane'):
            for item in node.names:
                if _module_file(node.module + '.' + item.name):
                    found[item.asname or item.name] = node.module + '.' + item.name
        return found

    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def split(scope):
        """Nodes owned by ``scope`` and the scopes directly nested in it."""
        own, nested, stack = [], [], list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, scopes):
                nested.append(node)
                # decorators, defaults and bases evaluate in the enclosing scope
                stack.extend(getattr(node, 'decorator_list', []) + getattr(node, 'bases', []))
                if not isinstance(node, ast.ClassDef):
                    stack.extend(d for d in node.args.defaults + node.args.kw_defaults if d)
                continue
            own.append(node)
            stack.extend(ast.iter_child_nodes(node))
        return own, nested

    def visit(scope, aliases):
        own, nested = split(scope)
        aliases = dict(aliases)
        for node in own:
            aliases.update(aliases_of(node))
        shadow = {n.id for n in own if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = scope.args
            shadow.update(a.arg for a in args.args + args.kwonlyargs + getattr(args, 'posonlyargs', []))
            shadow.update(a.arg for a in (args.vararg, args.kwarg) if a)
        for name in shadow:
            aliases.pop(name, None)
        for node in own:
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                names = _symbols(_module_file(aliases[node.value.id]))
                if names is not None and node.attr not in names:
                    findings.add(f'{filename}:{node.lineno}:{node.value.id}.{node.attr}')
        for child in nested:
            visit(child, aliases if not isinstance(scope, ast.ClassDef) else {k: v for k, v in aliases.items()})

    visit(tree, {})
    return sorted(findings)


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
        source = path.read_text(encoding='utf8')
        found, reason = check_source(source, name)
        findings.extend(found)
        findings.extend(check_attributes(source, name))
        if reason:
            unchecked.append(f'{name}:{reason}')
    print(json.dumps({'files': len(files), 'findings': findings, 'unchecked': unchecked}, indent=1))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
