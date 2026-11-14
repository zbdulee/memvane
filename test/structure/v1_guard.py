#!/usr/bin/env python3
"""Import-free structural checks for the v1 domain boundaries."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'test' / 'helpers'))
import structure_guard as old_guard


def _symbol_table(tree):
    result = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(node.name)
        elif isinstance(node, ast.Import):
            result.update(a.asname or a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.update(a.asname or a.name for a in node.names if a.name != '*')
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                result.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return result


def _module_name(path):
    return old_guard.name_for(path)


def _import_targets(tree, path, names):
    """Resolve import statements, including delayed and relative imports."""
    module = _module_name(path)
    is_package = path.endswith('/__init__.py')
    for node in ast.walk(tree):
        candidates = []
        if isinstance(node, ast.Import):
            candidates = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                package = module.split('.') if is_package else module.split('.')[:-1]
                drop = node.level - 1
                package = package[:len(package) - drop] if drop <= len(package) else []
                base = '.'.join(package + ([base] if base else []))
            for item in node.names:
                child = base + '.' + item.name if base else item.name
                candidates.append(child if child in names else base)
        for candidate in set(candidates):
            parts = candidate.split('.')
            while parts and '.'.join(parts) not in names:
                parts.pop()
            target = names.get('.'.join(parts)) if parts else None
            if target:
                yield node.lineno, target


def _hook_closures(snapshot, trees):
    target = trees.get('entry/run.py')
    rows = {}
    if target:
        for node in target.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TARGETS' for t in node.targets):
                try:
                    rows = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    names = {_module_name(path): path for path in trees}
    top = {}
    for src, dst, count in snapshot['graph']['top']:
        top.setdefault(src, set()).add(dst)
    result = {}
    for action in ('hook-classify', 'hook-serve'):
        row = rows.get(action)
        module = row[0].split(':', 1)[0] if isinstance(row, tuple) and len(row) == 3 else ''
        start = names.get(module)
        seen, todo = set(), [start] if start else []
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            todo.extend(top.get(path, ()))
        result[action] = sorted(seen)
    return result


def _patch_checks(trees):
    names = {_module_name(path): path for path in trees}
    symbols = {path: _symbol_table(tree) | {'__file__', '__name__', '__package__'} for path, tree in trees.items()}
    bad, unknown = [], []
    for path in ROOT.joinpath('test').rglob('*.py'):
        try:
            tree = ast.parse(path.read_text(encoding='utf8'), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split('.')[0]] = item.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    aliases[item.asname or item.name] = node.module + '.' + item.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            target = None
            if isinstance(func, ast.Name) and func.id == 'patch' and isinstance(node.args[0], ast.Constant):
                target = node.args[0].value
            elif (isinstance(func, ast.Attribute) and func.attr == 'object' and len(node.args) > 1
                  and isinstance(node.args[0], ast.Name) and isinstance(node.args[1], ast.Constant)):
                module = aliases.get(node.args[0].id)
                if module:
                    target = module + '.' + str(node.args[1].value)
            if not isinstance(target, str) or '.' not in target:
                continue
            parts = target.split('.')
            location = str(path.relative_to(ROOT)) + ':' + str(node.lineno)
            module_path, remaining = None, []
            for index in range(len(parts) - 1, 0, -1):
                candidate = '.'.join(parts[:index])
                if candidate in names:
                    module_path, remaining = names[candidate], parts[index:]
                    break
            if module_path is None:
                if target.startswith('memvane.'):
                    bad.append(location + ':' + target + ':missing_module')
                else:
                    unknown.append(location + ':' + target)
                continue
            if remaining[0] not in symbols[module_path]:
                bad.append(location + ':' + target + ':missing_attribute')
                continue
            current = trees[module_path]
            for attr in remaining:
                definition = next((n for n in current.body if isinstance(n, ast.ClassDef) and n.name == attr), None)
                if definition is None:
                    break  # Imported objects and instances cannot be resolved statically here.
                current = definition
            else:
                continue
            if isinstance(current, ast.ClassDef) and attr not in _symbol_table(current):
                bad.append(location + ':' + target + ':missing_attribute')
    return sorted(set(bad)), sorted(set(unknown))


# Strict rules (S4b): api passages that the import-path checks of G1/G5 do not see,
# and SQL that names a table owned by another domain. Reported separately until the
# product reaches zero; `--strict` folds them into the exit status.

_TUPLE_ATTRS = {'_replace', '_asdict', '_fields', '_make', '_field_defaults'}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_API_PARTS = ('api', 'types')
# api.py name limit: 30, journal excepted at its exact count. journal owns both
# selection/reader proofs and initial-build/writer records; no other domain uses
# only the build/writer half, the halves import each other (selection -> build
# lookup/record in route_selection, install_journal x2, dispatch_target; build ->
# install_journal and journal_reader in initial_build, writer_scope), and the
# selection half alone needs 41 names, so splitting would not meet the limit.
# runtime excepted at its exact count: every name is one distinct implementation
# function with another domain's caller (learning alone uses 30 of them: the
# admission, provider, CPU-share, coordination and calibration primitives of the
# trainer and inference service, plus load/sha behind the calibration adapter
# that scripts/development_guard.py imports; wake queue/sync owner serve host,
# app, sync, wiki, journal and install; the QA ledger trio serves the wiki CLI).
# No two names forward to the same callee, so no merge keeps behavior.
# wiki/workspace excepted at its exact count: 32 functions, each one distinct
# workspace implementation (card/run/mutation locks, queue paths and claims,
# lifecycle requests, the selected-route proof and checkout, scan cursors,
# atomic writes, the activity log) with a caller in wiki/compile, wiki/dedup,
# wiki/intake, wiki/lifecycle, host, sync or app; no two share a callee. The
# 33rd name is UNRESOLVED_BINDING, the activity-log sentinel append_activity
# needs as its default so an omitted binding stays distinguishable from None.
_API_NAME_LIMITS = {'journal': 51, 'runtime': 42, 'wiki/workspace': 33}
_CREATE_TABLE = re.compile(r'\bCREATE\s+(?:TEMP(?:ORARY)?\s+|VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                           r'(?!IF\b)["`\[]?(?:\w+\.)?(\w+)', re.I)
_TABLE_USE = re.compile(r'\b(DELETE\s+FROM|FROM|INTO|UPDATE|JOIN)\s+["`\[]?(?:main\.)?(\w+)')
# Resource handles (S4b rule 2): an object holding a connection or lock may cross
# the boundary when its owner declares the public members in types.py as a
# typing.Protocol. Each api function below hands out such a handle: its result
# (a str value) or the listed positions of the tuple it yields. Members of a
# handle outside the Protocol are findings; '_' members stay findings. Each
# Protocol names the class whose method signatures it must repeat.
_HANDLE_SOURCES = {
    'memvane.store.api.open_store': 'memvane.store.types.ViewStoreHandle',
    'memvane.journal.api.pin_schema3': ('memvane.store.types.ViewStoreHandle', None, None),
    'memvane.runtime.api.coordinator': 'memvane.runtime.types.CoordinatorHandle',
    'memvane.runtime.api.wake_queue': 'memvane.runtime.types.WakeQueueHandle',
    'memvane.runtime.api.supervisor': 'memvane.runtime.types.SupervisorHandle',
    'memvane.runtime.api.training_guard': 'memvane.runtime.types.TrainingGuardHandle',
}
_HANDLE_CLASSES = {
    'memvane.store.types.ViewStoreHandle': 'memvane.store.view.view_store.ViewStore',
    'memvane.runtime.types.CoordinatorHandle': 'memvane.runtime.resource_coordination.Coordinator',
    'memvane.runtime.types.WakeQueueHandle': 'memvane.runtime.runtime_work.WakeQueue',
    'memvane.runtime.types.SupervisorHandle': 'memvane.runtime.runtime_monitor.Supervisor',
    'memvane.runtime.types.TrainingGuardHandle': 'memvane.runtime.resource_coordination.TrainingGuard',
}
# Direct reads of another domain's table that the layer order keeps off that
# domain's api (S4b rule 3). Only a FROM/JOIN in a string that writes nothing
# is excused; an exception that matches no string is itself a finding.
_G9_READ_EXCEPTIONS = {
    # route (layer 2) reads (incarnation, scope_id) from the store header while it
    # classifies a local single-store route (_store_identity). store is layer 3 and
    # imports route, so route cannot call memvane.store.api without a layer
    # violation and an import cycle.
    ('memvane/route/index_host_route.py', 'store_meta'),
}
_SQL_WRITE = re.compile(r'\b(INSERT|UPDATE|DELETE|REPLACE|UPSERT|CREATE|DROP|ALTER)\b', re.I)


def _dunder(name):
    return name.startswith('__') and name.endswith('__')


def _private(name):
    return name.startswith('_') and not _dunder(name)


def _finding(rule, kind, path, line, source, target, name):
    return {'rule': rule, 'kind': kind, 'file': path, 'line': line, 'source': source,
            'target': target, 'name': name}


def _class_getattr(node):
    return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
               and item.name in ('__getattr__', '__getattribute__') for item in node.body)


def _protocols(tree):
    """``{class name: {member names}}`` of the module's ``typing.Protocol`` classes."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
                (isinstance(b, ast.Name) and b.id == 'Protocol')
                or (isinstance(b, ast.Attribute) and b.attr == 'Protocol') for b in node.bases):
            members = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(item.name)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    members.add(item.target.id)
            found[node.name] = {m for m in members if not _dunder(m)}
    return found


def _protocol_members(apis, dotted):
    """Members of a declared Protocol ``module.Class``; empty (all refused) when undeclared."""
    module, _, name = dotted.rpartition('.')
    return frozenset(apis.get(module, {}).get('protocols', {}).get(name, ()))


def _api_modules(trees, contexts):
    """Describe each domain api.py/types.py: defined names, proxies, module __getattr__."""
    result = {}
    for path, tree in trees.items():
        parts = path.split('/')
        if parts[0] != 'memvane' or Path(path).stem not in _API_PARTS or path not in contexts:
            continue
        proxies = {node.name for node in tree.body if isinstance(node, ast.ClassDef) and _class_getattr(node)}
        instances = set()
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id in proxies):
                instances.update(t.id for t in node.targets if isinstance(t, ast.Name))
        result[_module_name(path)] = {
            'path': path, 'domain': contexts[path], 'symbols': _symbol_table(tree),
            'module': _module_name(path), 'protocols': _protocols(tree),
            'proxies': instances | proxies,
            'module_getattr': any(isinstance(n, ast.FunctionDef) and n.name == '__getattr__' for n in tree.body)}
    return result


def _resolve_from(node, module, is_package):
    base = node.module or ''
    if node.level:
        package = module.split('.') if is_package else module.split('.')[:-1]
        drop = node.level - 1
        package = package[:len(package) - drop] if drop <= len(package) else []
        base = '.'.join(package + ([base] if base else []))
    return base


def _dotted(node):
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return '.'.join([node.id] + names[::-1])
    return None


def _scope_children(node):
    """Yield the nodes evaluated in this scope and collect nested scopes."""
    nested, stack = [], list(ast.iter_child_nodes(node))
    if isinstance(node, ast.Lambda):
        stack = [node.body]
    elif isinstance(node, _SCOPES):
        stack = list(node.body)
    seen = []
    while stack:
        item = stack.pop()
        seen.append(item)
        if isinstance(item, _SCOPES):
            nested.append(item)
            outer = list(getattr(item, 'decorator_list', [])) + list(getattr(item, 'bases', []))
            outer += [k.value for k in getattr(item, 'keywords', [])]
            if not isinstance(item, ast.ClassDef):
                outer += list(item.args.defaults) + [d for d in item.args.kw_defaults if d is not None]
            stack.extend(outer)
            continue
        stack.extend(ast.iter_child_nodes(item))
    return seen, nested


def _bound_names(target):
    """Names a binding target rebinds; attribute and subscript targets bind none."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _bound_names(item)]
    return []


def _g1_strict(trees, contexts, apis):
    findings = []
    for path, tree in trees.items():
        source = contexts.get(path)
        if source is None:
            continue
        module, is_package = _module_name(path), path.endswith('/__init__.py')
        crossing = lambda api: api['domain'] != 'kernel' and (
            api['domain'] != source or path.startswith('hermes_adapter/'))

        def import_bindings(node):
            """Return {local name: (kind, api, name)} for api imports of another domain."""
            bound = {}
            if isinstance(node, ast.Import):
                for item in node.names:
                    api = apis.get(item.name)
                    if api and item.asname and crossing(api):
                        bound[item.asname] = ('module', api, '')
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from(node, module, is_package)
                for item in node.names:
                    local = item.asname or item.name
                    api = apis.get(base + '.' + item.name)
                    if api and crossing(api):
                        bound[local] = ('module', api, '')
                        continue
                    api = apis.get(base)
                    if api and crossing(api):
                        kind = 'proxy' if item.name in api['proxies'] else 'value'
                        bound[local] = (kind, api, item.name)
            return bound

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _resolve_from(node, module, is_package)
            api = apis.get(base)
            if not api or not crossing(api):
                continue
            for item in node.names:
                name = item.name
                if name == '*':
                    kind = 'star_import'
                elif _private(name):
                    kind = 'import_private'
                elif name not in api['symbols']:
                    kind = 'getattr_forward'
                elif name in api['proxies']:
                    kind = 'proxy_import'
                else:
                    continue
                findings.append(_finding('G1', kind, path, node.lineno, source, api['domain'], name))

        def classify(expr, env):
            if isinstance(expr, ast.Name):
                return env.get(expr.id)
            if isinstance(expr, ast.Attribute):
                dotted = _dotted(expr)
                api = apis.get(dotted) if dotted else None
                if api and crossing(api):
                    return ('module', api, '')
                base = classify(expr.value, env)
                if base is None:
                    return None
                if base[0] == 'module':
                    kind = 'proxy' if expr.attr in base[1]['proxies'] else 'value'
                    return (kind, base[1], expr.attr)
                return ('value', base[1], base[2] + '.' + expr.attr)
            if isinstance(expr, ast.Call):
                base = classify(expr.func, env)
                if base is None:
                    return None
                spec = None
                if base[0] == 'value' and base[2] and '.' not in base[2] and '(' not in base[2]:
                    spec = _HANDLE_SOURCES.get(base[1]['module'] + '.' + base[2])
                if isinstance(spec, str):
                    return ('handle', base[1], base[2] + '()', _protocol_members(apis, spec))
                if spec is not None:
                    return ('handles', base[1], base[2] + '()',
                            tuple(None if item is None else _protocol_members(apis, item) for item in spec))
                return ('value', base[1], base[2] + '()')
            if isinstance(expr, (ast.Subscript, ast.Await, ast.Starred)):
                base = classify(expr.value, env)
                return ('value', base[1], base[2]) if base else None
            return None

        def visit(scope, inherited):
            nodes, nested = _scope_children(scope)
            local = set()
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = scope.args
                for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                    if arg is not None:
                        local.add(arg.arg)
            pairs, env = [], {}
            for node in nodes:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.asname or a.name.split('.')[0] for a in node.names if a.name != '*']
                    local.update(names)
                    env.update(import_bindings(node))
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        local.update(_bound_names(target))
                        pairs.append((target, node.value))
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    local.update(_bound_names(node.target))
                    if node.value is not None:
                        pairs.append((node.target, node.value))
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                    local.update(_bound_names(node.target))
                    pairs.append((node.target, node.iter))
                elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                    local.update(_bound_names(node.optional_vars))
                    pairs.append((node.optional_vars, node.context_expr))
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    local.add(node.name)
            for item in nested:
                if not isinstance(item, ast.Lambda):
                    local.add(item.name)
            env = {**{k: v for k, v in inherited.items() if k not in local}, **env}
            changed = True
            while changed:
                changed = False
                for target, value in pairs:
                    desc = classify(value, env)
                    if desc is None:
                        continue
                    if (desc[0] == 'handles' and isinstance(target, (ast.Tuple, ast.List))
                            and len(target.elts) == len(desc[3])):
                        bound = [(element, ('value', desc[1], desc[2]) if members is None
                                  else ('handle', desc[1], desc[2], members))
                                 for element, members in zip(target.elts, desc[3])]
                    else:
                        bound = [(target, desc)]
                    for element, item in bound:
                        if not isinstance(element, ast.Name) or item[0] == 'handles':
                            item = ('value', item[1], item[2])
                        for name in _bound_names(element):
                            if name not in env:
                                env[name] = item
                                changed = True
            for node in nodes:
                if not isinstance(node, ast.Attribute):
                    continue
                base = classify(node.value, env)
                if base is None:
                    continue
                attr, api = node.attr, base[1]
                label = (base[2] + '.' if base[2] else '') + attr
                if _private(attr) and attr not in _TUPLE_ATTRS:
                    kind = 'private_attr'
                elif base[0] == 'handle' and attr not in base[3]:
                    kind = 'undeclared_handle_member'
                elif base[0] == 'module' and attr not in api['symbols']:
                    kind = 'getattr_forward'
                elif base[0] == 'proxy':
                    kind = 'proxy_attr'
                else:
                    continue
                findings.append(_finding('G1', kind, path, node.lineno, source, api['domain'], label))
            for item in nested:
                visit(item, env)

        visit(tree, {})
    return findings


def _signature(function, constants):
    """Parameter kinds, names and defaults (module constants resolved), annotations ignored."""
    args = function.args

    def default(node):
        if node is None:
            return None
        if isinstance(node, ast.Name) and node.id in constants:
            return repr(constants[node.id])
        return ast.unparse(node)
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    shape = [('positional', a.arg, default(d)) for a, d in zip(positional, defaults)]
    shape += [('vararg', args.vararg.arg, None)] if args.vararg else []
    shape += [('keyword', a.arg, default(d)) for a, d in zip(args.kwonlyargs, args.kw_defaults)]
    shape += [('kwarg', args.kwarg.arg, None)] if args.kwarg else []
    return shape


def _handle_signatures(trees, contexts, apis):
    """Each handle Protocol member must repeat its class method's signature (S4b rule 2)."""
    modules = {_module_name(path): (path, tree) for path, tree in trees.items()}
    findings = []

    def find_class(module, name, seen):
        entry = modules.get(module)
        if entry is None or (module, name) in seen:
            return None
        seen.add((module, name))
        for node in entry[1].body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return module, node
        for node in entry[1].body:
            if isinstance(node, ast.ImportFrom):
                base = _resolve_from(node, module, entry[0].endswith('/__init__.py'))
                for item in node.names:
                    if (item.asname or item.name) == name:
                        return find_class(base, item.name, seen)
        return None

    def method(module, cls, name, seen):
        """Depth-first over bases, first definition wins (the mixin order ViewStore uses)."""
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                return module, item
        for base in cls.bases:
            if isinstance(base, ast.Name):
                found = find_class(module, base.id, set())
                if found and (found[0], found[1].name) not in seen:
                    seen.add((found[0], found[1].name))
                    hit = method(found[0], found[1], name, seen)
                    if hit:
                        return hit
        return None

    def constants(module):
        tree = modules[module][1]
        return {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}

    for protocol, implementation in sorted(_HANDLE_CLASSES.items()):
        module, _, name = protocol.rpartition('.')
        if module not in modules:
            continue
        path, tree = modules[module]
        node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name), None)
        domain = contexts.get(path, '')
        if node is None:
            findings.append(_finding('G1', 'handle_protocol_missing', path, 1, domain, domain, name))
            continue
        cls_module, _, cls_name = implementation.rpartition('.')
        found = find_class(cls_module, cls_name, set())
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hit = method(found[0], found[1], item.name, set()) if found else None
            if hit is None or _signature(item, {}) != _signature(hit[1], constants(hit[0])):
                findings.append(_finding('G1', 'handle_signature', path, item.lineno, domain, domain,
                                         name + '.' + item.name))
    return findings


def _signature_only_varargs(node, method):
    args = node.args
    named = args.posonlyargs + args.args
    if method and named and named[0].arg in ('self', 'cls'):
        named = named[1:]
    return not named and not args.kwonlyargs and (args.vararg is not None or args.kwarg is not None)


def _g5_strict(trees, contexts):
    findings, attributes = [], {}
    for path, tree in sorted(trees.items()):
        if not (path.startswith('memvane/') and path.endswith('/api.py')) or path not in contexts:
            continue
        domain = contexts[path]
        names = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                continue
            names.update(n for n in _symbol_table(ast.Module(body=[node], type_ignores=[])) if not _dunder(n))

        def members(cls, prefix):
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound = [item.name]
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    bound = [n for t in targets for n in _bound_names(t)]
                else:
                    bound = []
                for name in bound:
                    if not _dunder(name):
                        names.add(prefix + name)
                if isinstance(item, ast.ClassDef):
                    members(item, prefix + item.name + '.')

        def walk(body, prefix, in_class):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    if _class_getattr(node):
                        findings.append(_finding('G5', 'proxy_class', path, node.lineno, domain, domain, prefix + node.name))
                    walk(node.body, prefix + node.name + '.', True)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not prefix and node.name == '__getattr__':
                        findings.append(_finding('G5', 'module_getattr', path, node.lineno, domain, domain, node.name))
                    elif _signature_only_varargs(node, in_class):
                        findings.append(_finding('G5', 'varargs_forwarder', path, node.lineno, domain, domain, prefix + node.name))
                    walk(node.body, prefix + node.name + '.', False)
                else:
                    for child in ast.walk(node):
                        if isinstance(child, ast.Lambda) and _signature_only_varargs(child, False):
                            target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else None
                            label = prefix + (target.id if isinstance(target, ast.Name) else '<lambda>')
                            findings.append(_finding('G5', 'varargs_forwarder', path, child.lineno, domain, domain, label))

        walk(tree.body, '', False)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                members(node, node.name + '.')
        for node in ast.walk(tree):
            label = None
            if isinstance(node, ast.ImportFrom) and node.module == 'importlib':
                label = next((a.name for a in node.names if a.name == 'import_module'), None)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '__import__':
                label = '__import__'
            elif isinstance(node, ast.Attribute) and node.attr == 'import_module':
                label = 'import_module'
            if label:
                findings.append(_finding('G5', 'dynamic_import', path, node.lineno, domain, domain, label))
        attributes[path] = len(names)
        if len(names) > _API_NAME_LIMITS.get(domain, 30):
            findings.append(_finding('G5', 'public_attributes', path, 1, domain, domain, str(len(names))))
    return findings, attributes


def _g9_strict(trees, contexts):
    docstrings = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
    strings = []
    for path, tree in trees.items():
        if path in contexts:
            strings.extend((path, node) for node in ast.walk(tree)
                           if isinstance(node, ast.Constant) and isinstance(node.value, str)
                           and id(node) not in docstrings)
    owners = {}
    for path, node in strings:
        for table in _CREATE_TABLE.findall(node.value):
            owners.setdefault(table, set()).add(contexts[path])
    findings, excused = [], set()
    for path, node in strings:
        source = contexts[path]
        for keyword, table in _TABLE_USE.findall(node.value):
            owned = owners.get(table)
            if (owned and source not in owned and (path, table) in _G9_READ_EXCEPTIONS
                    and keyword.upper() in ('FROM', 'JOIN') and not _SQL_WRITE.search(node.value)):
                excused.add((path, table))
                continue
            if owned and source not in owned:
                for owner in sorted(owned):
                    findings.append(_finding('G9', ' '.join(keyword.split()).lower(), path, node.lineno,
                                             source, owner, table))
    for path, table in sorted(_G9_READ_EXCEPTIONS - excused):
        if path in contexts:
            for owner in sorted(owners.get(table, ())) or [contexts[path]]:
                findings.append(_finding('G9', 'unused_read_exception', path, 1, contexts[path], owner, table))
    return findings, {table: sorted(domains) for table, domains in sorted(owners.items())}


def strict_checks(trees, contexts):
    """Return (findings, metrics) for the strict G1/G5/G9 rules over parsed modules."""
    apis = _api_modules(trees, contexts)
    g5, attributes = _g5_strict(trees, contexts)
    g9, owners = _g9_strict(trees, contexts)
    findings = _g1_strict(trees, contexts, apis) + _handle_signatures(trees, contexts, apis) + g5 + g9
    unique = {(f['rule'], f['kind'], f['file'], f['line'], f['name'], f['target']): f for f in findings}
    ordered = sorted(unique.values(), key=lambda f: (f['rule'], f['target'], f['file'], f['line'], f['kind'], f['name']))
    return ordered, {'api_attributes': attributes, 'table_owners': owners}


def strict_line(finding):
    return '{file}:{line}:{target}:{kind}:{name}'.format(**finding)


def inspect(config, baseline=None):
    snapshot, io, errors = old_guard.scan(ROOT, config)
    files, ranks = snapshot['policy']['modules'], snapshot['policy']['contexts']
    trees = {p: ast.parse((ROOT / p).read_text(encoding='utf8'), filename=p)
             for p in files if (ROOT / p).is_file()}
    issues = {f'G{i}': [] for i in range(1, 9)}
    stdlib_dir = Path(sysconfig.get_path('stdlib'))
    stdlib = set(sys.builtin_module_names) | {p.stem for p in stdlib_dir.iterdir()
              if p.name not in ('site-packages', 'dist-packages') and
              (p.suffix == '.py' or (p.is_dir() and (p / '__init__.py').is_file()))}
    issues['G1'].extend(errors)
    names = {_module_name(path): path for path in trees}
    for src, tree in trees.items():
        for lineno, dst in _import_targets(tree, src, names):
            if dst not in files:
                continue
            left, right = files[src]['context'], files[dst]['context']
            hermes_boundary = src.startswith('hermes_adapter/') and dst.startswith('memvane/')
            if right == 'kernel' or (left == right and not hermes_boundary):
                continue
            edge = f'{src}:{lineno}:{dst}'
            if dst.startswith('memvane/') and not dst.endswith(('/api.py', '/types.py')):
                issues['G1'].append(edge)
            if not hermes_boundary and left != 'app' and ranks[right] >= ranks[left]:
                issues['G2'].append(edge)
    kernel = [p for p in trees if p.startswith('memvane/kernel/') and not p.endswith('/__init__.py')]
    size = sum(snapshot['file_sizes'][p] for p in kernel)
    if size > 1500:
        issues['G3'].append(f'kernel_lines:{size}')
    if len(kernel) > 14:
        issues['G3'].append(f'kernel_modules:{len(kernel)}')
    for path in kernel:
        if snapshot['file_sizes'][path] > 250:
            issues['G3'].append(f'{path}:lines:{snapshot["file_sizes"][path]}')
        for node in ast.walk(trees[path]):
            if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('memvane.') and not node.module.startswith('memvane.kernel'):
                issues['G3'].append(f'{path}:{node.lineno}:domain_import:{node.module}')
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name.startswith('memvane.') and not item.name.startswith('memvane.kernel'):
                        issues['G3'].append(f'{path}:{node.lineno}:domain_import:{item.name}')
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r'\bCREATE\s+TABLE\b', node.value, re.I):
                issues['G3'].append(f'{path}:{node.lineno}:sqlite_ddl')
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.fullmatch(r'(?:memvane|laya|qmd)-[a-z0-9-]+-v[0-9]+', node.value)):
                issues['G3'].append(f'{path}:{node.lineno}:schema_id')
    domain_lines = Counter()
    for path, lines in snapshot['file_sizes'].items():
        if path.startswith('memvane/') and path in files:
            domain_lines[files[path]['context']] += lines
    for domain, lines in domain_lines.items():
        if domain not in ('kernel', 'learning') and lines > 8000:
            issues['G4'].append(f'{domain}:lines:{lines}')
    old = baseline or {}
    for kind, ceiling in (('file_sizes', 800), ('function_sizes', 150)):
        prior = old.get('size_exceptions', {}).get(kind, {})
        for name, amount in snapshot[kind].items():
            if amount > max(ceiling, prior.get(name, 0)):
                issues['G4'].append(f'{kind}:{name}:{amount}')
    domains = [x for x in ranks if x not in ('kernel', 'app')]
    for domain in domains:
        directory = ROOT / 'memvane' / domain
        for required in ('README.md', 'api.py', 'types.py'):
            if not (directory / required).is_file():
                issues['G5'].append(f'{domain}:missing:{required}')
        path = 'memvane/' + domain + '/api.py'
        if path not in trees:
            continue
        if snapshot['file_sizes'][path] > 250:
            issues['G5'].append(f'{path}:lines:{snapshot["file_sizes"][path]}')
        exported = {name for name in _symbol_table(trees[path]) if not name.startswith('_')}
        if len(exported) > _API_NAME_LIMITS.get(domain, 30):
            issues['G5'].append(f'{path}:public_names:{len(exported)}')
        own = 'memvane.' + domain.replace('/', '.') + '.types'
        for node in trees[path].body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    package = _module_name(path).split('.')[:-1]
                    package = package[:len(package) - node.level + 1]
                    module = '.'.join(package + ([module] if module else []))
                if (module not in ('__future__', own) and not module.startswith('memvane.kernel')
                        and module.split('.')[0] not in stdlib):
                    issues['G5'].append(f'{path}:{node.lineno}:top_import:{module}')
            elif isinstance(node, ast.Import):
                for item in node.names:
                    if (not item.name.startswith('memvane.kernel')
                            and item.name.split('.')[0] not in stdlib):
                        issues['G5'].append(f'{path}:{node.lineno}:top_import:{item.name}')
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == '__all__' for t in targets):
                    try:
                        exported = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        issues['G5'].append(f'{path}:{node.lineno}:dynamic_exports')
                    else:
                        if not isinstance(exported, (tuple, list)) or any(
                                not isinstance(x, str) or x.startswith('_') for x in exported):
                            issues['G5'].append(f'{path}:{node.lineno}:private_exports')
    closures = _hook_closures(snapshot, trees)
    for action, closure in closures.items():
        if not closure:
            issues['G6'].append(f'{action}:missing_target')
        if baseline:
            issues['G6'].extend(f'{action}:new:{p}' for p in sorted(set(closure) - set(baseline.get('hook_closures', {}).get(action, []))))
    for path, findings in io.items():
        if path.endswith('/types.py') or '/domain/' in path:
            issues['G7'].extend(path + ':' + finding for finding in findings)
            for node in ast.walk(trees[path]):
                if isinstance(node, ast.Call) and (
                        isinstance(node.func, ast.Name) and node.func.id == '__import__'
                        or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'):
                    issues['G7'].append(f'{path}:{node.lineno}:dynamic_import')
    for path, tree in trees.items():
        if '/adapters/' not in path:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and '.application' in (node.module or ''):
                issues['G7'].append(f'{path}:{node.lineno}:adapter_to_application')
            if isinstance(node, ast.Import):
                issues['G7'].extend(f'{path}:{node.lineno}:adapter_to_application' for item in node.names if '.application' in item.name)
    issues['G8'], unknown = _patch_checks(trees)
    strict = strict_checks(trees, {p: files[p]['context'] for p in trees})
    return snapshot, closures, {key: sorted(set(value)) for key, value in issues.items()}, unknown, strict


def _csv(value):
    return [item.strip() for item in value.split(',') if item.strip()] if value else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', default=str(ROOT / 'test/structure/v1-baseline.json'))
    parser.add_argument('--write-baseline', action='store_true')
    parser.add_argument('--rules', type=_csv, default=[],
                        help='comma-separated rules to report, e.g. G1,G5,G9 (default: all)')
    parser.add_argument('--domain', type=_csv, default=[],
                        help='report only strict findings whose crossed api or table owner is one of these domains')
    parser.add_argument('--source', type=_csv, default=[],
                        help='report only strict findings located in these domains')
    parser.add_argument('--strict', action='store_true',
                        help='count strict G1/G5/G9 findings toward the exit status')
    args = parser.parse_args()
    config = json.loads((ROOT / 'test/structure/context-map.json').read_text(encoding='utf8'))
    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text(encoding='utf8')) if baseline_path.exists() else None
    snapshot, closures, issues, unknown, strict = inspect(config, baseline)
    if args.write_baseline:
        exceptions = {kind: {p: n for p, n in snapshot[kind].items() if n > limit}
                      for kind, limit in (('file_sizes', 800), ('function_sizes', 150))}
        baseline = {'size_exceptions': exceptions, 'hook_closures': closures}
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
        snapshot, closures, issues, unknown, strict = inspect(config, baseline)
    findings, strict_metrics = strict
    findings = [f for f in findings if (not args.domain or f['target'] in args.domain)
                and (not args.source or f['source'] in args.source)]
    strict_issues = {rule: [strict_line(f) for f in findings if f['rule'] == rule] for rule in ('G1', 'G5', 'G9')}
    by_domain = {}
    for finding in findings:
        row = by_domain.setdefault(finding['target'], {})
        row[finding['rule']] = row.get(finding['rule'], 0) + 1
    if args.rules:
        issues = {k: v for k, v in issues.items() if k in args.rules}
        strict_issues = {k: v for k, v in strict_issues.items() if k in args.rules}
        by_domain = {d: {k: n for k, n in row.items() if k in args.rules} for d, row in by_domain.items()}
        by_domain = {d: row for d, row in by_domain.items() if row}
    print(json.dumps({'counts': {k: len(v) for k, v in issues.items()},
                      'issues': issues, 'unknown_patch_targets': unknown,
                      'strict_counts': {k: len(v) for k, v in strict_issues.items()},
                      'strict_issues': strict_issues, 'strict_by_domain': by_domain,
                      'strict_metrics': strict_metrics,
                      'limitations': ['G1 and G2 cover resolved import statements in classified source modules; test imports and nonliteral dynamic imports are not yet classified', 'G6 closure follows static top-level imports and requires hook target verification', 'G7 adapter relative imports and indirect I/O require further analysis', 'G8 examines statically identifiable Python patch calls; JavaScript embedded Python and dynamic targets require further analysis', 'strict G1 follows names bound from another domain api through assignment, call, subscript, with and for within a scope; values passed as arguments or returned through other modules are not followed', 'strict G9 reads Python string literals only; table names built at run time and SQL in JavaScript are not checked'],
                      'metrics': snapshot['metrics']}, ensure_ascii=False, sort_keys=True))
    failing = any(issues.values()) or (args.strict and any(strict_issues.values()))
    return 1 if failing else 0


if __name__ == '__main__':
    sys.exit(main())
