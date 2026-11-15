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


_PROXIES = {}
_MODULE_ALIASES = {}


def _module_aliases(dotted):
    """Top-level names of a module bound to another product module by import."""
    if dotted not in _MODULE_ALIASES:
        found = {}
        path = _module_file(dotted)
        tree = ast.parse(path.read_text(encoding='utf8'), str(path)) if path else ast.Module(body=[])
        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.asname and item.name.startswith('memvane.'):
                        found[item.asname] = item.name
            elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or '').startswith('memvane'):
                for item in node.names:
                    if _module_file(node.module + '.' + item.name):
                        found[item.asname or item.name] = node.module + '.' + item.name
        _MODULE_ALIASES[dotted] = found
    return _MODULE_ALIASES[dotted]


def _proxies(dotted):
    """Top-level ``NAME = Class(...)`` operation proxies of a module and the names they answer.

    A class with ``__getattr__`` called with one string forwards to the sibling
    module of that name; any other class answers its own attributes.
    """
    if dotted not in _PROXIES:
        found = {}
        path = _module_file(dotted)
        tree = ast.parse(path.read_text(encoding='utf8'), str(path)) if path else ast.Module(body=[])
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        package = dotted if path and path.name == '__init__.py' else dotted.rpartition('.')[0]
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in classes):
                continue
            cls = classes[node.value.func.id]
            members = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            members |= {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
            args = node.value.args
            if '__getattr__' in members:
                target = (_module_file(package + '.' + args[0].value)
                          if len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)
                          else None)
                found[node.targets[0].id] = _symbols(target) if target else None
            else:
                found[node.targets[0].id] = members
        _PROXIES[dotted] = found
    return _PROXIES[dotted]


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
                elif _module_file(node.module) and item.name in _proxies(node.module):
                    found[item.asname or item.name] = (node.module, item.name)
        return found

    def answers(target, attr):
        if isinstance(target, tuple):
            names = _proxies(target[0])[target[1]]
        else:
            names = _symbols(_module_file(target))
        return names is None or attr in names

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
        # The last binding of a name in source order is what later reads see.
        shadow = set()
        for node in sorted((n for n in own if hasattr(n, 'lineno')), key=lambda n: (n.lineno, n.col_offset)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound = aliases_of(node)
                for name in ((a.asname or a.name).split('.')[0] for a in node.names):
                    if name in bound:
                        aliases[name] = bound[name]
                        shadow.discard(name)
                    else:
                        aliases.pop(name, None)
                        shadow.add(name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadow.add(node.id)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = scope.args
            shadow.update(a.arg for a in args.args + args.kwonlyargs + getattr(args, 'posonlyargs', []))
            shadow.update(a.arg for a in (args.vararg, args.kwarg) if a)
        for name in shadow:
            aliases.pop(name, None)
        for node in own:
            if not isinstance(node, ast.Attribute):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                if not answers(aliases[node.value.id], node.attr):
                    findings.add(f'{filename}:{node.lineno}:{node.value.id}.{node.attr}')
            elif (isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name)
                    and isinstance(aliases.get(node.value.value.id), str)):
                outer = aliases[node.value.value.id]
                if node.value.attr in _proxies(outer):
                    target = (outer, node.value.attr)
                elif node.value.attr in _module_aliases(outer):
                    target = _module_aliases(outer)[node.value.attr]
                else:
                    continue
                if not answers(target, node.attr):
                    findings.add(f'{filename}:{node.lineno}:{node.value.value.id}.{node.value.attr}.{node.attr}')
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
