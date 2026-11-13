#!/usr/bin/env python3
"""Static, import-free structure baseline and ratchet (Python 3.9+)."""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import sys

IO_MODULES = {'os', 'sqlite3', 'subprocess', 'socket', 'fcntl', 'time', 'importlib'}
IO_METHODS = {'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes',
              'unlink', 'mkdir', 'rmdir', 'rename', 'touch', 'stat',
              'lstat', 'glob', 'rglob', 'iterdir', 'resolve', 'exists'}
LAYERS = {'domain': 0, 'adapter': 1, 'application': 2, 'entry': 3}


def name_for(path):
    parts = list(Path(path).with_suffix('').parts)
    if parts[0] == 'core':
        parts = parts[1:]
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts) or '__root__'


def scope_files(root):
    paths = []
    for folder in ('core', 'hermes_adapter'):
        base = root / folder
        if base.is_dir():
            paths.extend(p.relative_to(root).as_posix() for p in base.rglob('*.py')
                         if '__pycache__' not in p.parts and not p.is_symlink())
    if (root / '__init__.py').is_file():
        paths.append('__init__.py')
    return sorted(paths)


class Imports(ast.NodeVisitor):
    def __init__(self, module, names, is_package):
        self.module, self.names, self.is_package = module, names, is_package
        self.depth = 0
        self.edges = []
        self.io = []
        self.functions = {}
        self.stack = []
        self.function_counts = Counter()
        self.open_aliases = {'open'}

    def visit_FunctionDef(self, node):
        self._function(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _function(self, node):
        qualified = '.'.join(self.stack + [node.name])
        self.function_counts[qualified] += 1
        key = qualified + '#' + str(self.function_counts[qualified])
        self.functions[key] = node.end_lineno - node.lineno + 1
        self.stack.append(node.name)
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        self.stack.pop()

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _target(self, name):
        parts = name.split('.')
        while parts:
            target = '.'.join(parts)
            if target in self.names:
                return self.names[target]
            parts.pop()
        return '@' + name.split('.')[0]

    def _add(self, name):
        if name and name != '__future__':
            self.edges.append((self._target(name), self.depth == 0))

    def visit_Import(self, node):
        for alias in node.names:
            self._add(alias.name)
            top = alias.name.split('.')[0]
            if top in IO_MODULES or top == 'pathlib':
                self.io.append(f'{node.lineno}:import:{alias.name}')
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ''
        if node.level:
            package = self.module.split('.') if self.is_package else self.module.split('.')[:-1]
            drop = node.level - 1
            prefix = package[:len(package) - drop] if drop <= len(package) else []
            module = '.'.join(prefix + ([module] if module else []))
        if module:
            self._add(module)
        for alias in node.names:
            full = module + '.' + alias.name if module else alias.name
            if full in self.names:
                self._add(full)
        raw = (node.module or '').split('.')[0]
        if raw in IO_MODULES:
            self.io.append(f'{node.lineno}:import:{node.module}')
        if raw == 'pathlib' and any(x.name not in ('PurePath', 'PurePosixPath', 'PureWindowsPath') for x in node.names):
            self.io.append(f'{node.lineno}:import:pathlib')
        if raw == 'builtins':
            self.open_aliases.update(x.asname or x.name for x in node.names if x.name == 'open')
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id in self.open_aliases:
            self.io.append(f'{node.lineno}:call:{f.id}')
        elif isinstance(f, ast.Name) and f.id == '__import__':
            name = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else 'dynamic'
            self.io.append(f'{node.lineno}:dynamic_import:{name}')
        elif (isinstance(f, ast.Attribute) and f.attr == 'import_module'
              and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)
              and node.args[0].value.split('.')[0] in IO_MODULES | {'pathlib'}):
            self.io.append(f'{node.lineno}:dynamic_import:{node.args[0].value}')
        elif isinstance(f, ast.Attribute) and f.attr in IO_METHODS:
            self.io.append(f'{node.lineno}:call:{f.attr}')
        self.generic_visit(node)


def graph_scc(nodes, edges):
    graph = {n: [] for n in nodes}
    reverse = {n: [] for n in nodes}
    for src, dst in edges:
        if dst in graph:
            graph[src].append(dst)
            reverse[dst].append(src)
    seen, order = set(), []
    def visit(start, links, record):
        stack = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                record.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((n, False) for n in links[node] if n not in seen)
    for n in sorted(nodes):
        if n not in seen:
            visit(n, graph, order)
    seen.clear()
    result = []
    for n in reversed(order):
        if n not in seen:
            component = []
            visit(n, reverse, component)
            result.append(sorted(component))
    return sorted(result, key=lambda x: (-len(x), x))


def scan(root, config):
    modules = config['modules']
    files = scope_files(root)
    names = {name_for(p): p for p in modules}
    if len(names) != len(modules):
        raise ValueError('duplicate_module_name')
    errors = []
    errors.extend('unclassified:' + p for p in sorted(set(files) - set(modules)))
    errors.extend('missing_module:' + p for p in sorted(set(modules) - set(files)))
    edges, top = Counter(), defaultdict(set)
    io, sizes, functions = {}, {}, {}
    for path in sorted(set(files) & set(modules)):
        data = (root / path).read_text(encoding='utf-8')
        tree = ast.parse(data, filename=path)
        visitor = Imports(name_for(path), names, path.endswith('/__init__.py'))
        visitor.visit(tree)
        sizes[path] = len(data.splitlines())
        functions.update({path + ':' + k: n for k, n in visitor.functions.items()})
        for target, is_top in visitor.edges:
            if target == path:
                continue
            edges[(path, target, 'top' if is_top else 'lazy')] += 1
            if is_top:
                top[path].add(target)
        if modules[path]['layer'] == 'domain':
            io[path] = sorted(set(visitor.io))
    violations = Counter()
    all_project_edges = set()
    for (src, dst, when), count in edges.items():
        if dst not in modules:
            continue
        all_project_edges.add((src, dst))
        left, right = modules[src], modules[dst]
        if (config['contexts'][left['context']] < config['contexts'][right['context']]
                or LAYERS[left['layer']] < LAYERS[right['layer']]):
            violations[src + ' -> ' + dst + ' [' + when + ']'] += count
    closures = {}
    for entry in config['entrypoints']:
        seen, todo = set(), list(top[entry])
        while todo:
            item = todo.pop()
            if item not in seen:
                seen.add(item)
                todo.extend(top.get(item, ()))
        closures[entry] = sorted(seen)
    scc = graph_scc(files, all_project_edges)
    top_project_edges = {(src, dst) for (src, dst, when) in edges
                         if when == 'top' and dst in modules}
    top_scc = graph_scc(files, top_project_edges)
    context_edges = sorted({(modules[x]['context'], modules[y]['context'])
                            for x, y in all_project_edges if modules[x]['context'] != modules[y]['context']})
    result = {'violations': dict(sorted(violations.items())), 'closures': closures,
              'graph': {when: [[src, dst, count] for (src, dst, scope), count in sorted(edges.items())
                               if scope == when] for when in ('top', 'lazy')},
              'policy': {'contexts': config['contexts'], 'modules': modules,
                         'entrypoints': sorted(config['entrypoints']),
                         'size_enforce_from': config['size_enforce_from']},
              'pinned_paths': sorted(config['pinned_paths']), 'file_sizes': sizes,
              'function_sizes': functions,
              'metrics': {'modules': len(files), 'lines': sum(sizes.values()),
                          'project_edges': len(all_project_edges),
                          'top_project_edges': len(top_project_edges),
                          'top_imports': sum(count for (_, _, k), count in edges.items() if k == 'top'),
                          'lazy_imports': sum(count for (_, _, k), count in edges.items() if k == 'lazy'),
                          'largest_scc': len(scc[0]) if scc else 0,
                          'largest_top_scc': len(top_scc[0]) if top_scc else 0,
                          'context_edges': [list(x) for x in context_edges],
                          'two_way_context_pairs': sum(1 for a, b in context_edges if a < b and (b, a) in context_edges),
                          'files_over_1000': sum(n > 1000 for n in sizes.values()),
                          'functions_over_150': sum(n > 150 for n in functions.values())}}
    return result, io, errors


def check(root, config, baseline, today):
    current, io, errors = scan(root, config)
    warnings = []
    policy = baseline['policy']
    for path, previous in policy['modules'].items():
        if path in config['modules'] and config['modules'][path] != previous:
            errors.append('classification_changed:' + path)
    for name, rank in policy['contexts'].items():
        if config['contexts'].get(name) != rank:
            errors.append('context_direction_changed:' + name)
    if sorted(config['entrypoints']) != policy['entrypoints']:
        errors.append('entrypoints_changed')
    if config['size_enforce_from'] > policy['size_enforce_from']:
        errors.append('size_enforcement_postponed')
    for path in sorted(set(config['pinned_paths']) | set(baseline['pinned_paths'])):
        if not (root / path).exists():
            errors.append('pinned_path:' + path)
    for path, issues in io.items():
        errors.extend('domain_io:' + path + ':' + issue for issue in issues)
    for edge, count in current['violations'].items():
        old = baseline['violations'].get(edge, 0)
        if count > old:
            errors.append(f'import_direction:{edge}:{old}->{count}')
    for entry, closure in current['closures'].items():
        for item in sorted(set(closure) - set(baseline['closures'].get(entry, []))):
            errors.append(f'hook_closure:{entry}:{item}')
    size_errors = []
    for key, limit, label in [('file_sizes', 800, 'file_size'), ('function_sizes', 150, 'function_size')]:
        for path, amount in current[key].items():
            old = baseline[key].get(path, 0)
            if amount > max(limit, old):
                size_errors.append(f'{label}:{path}:{old}->{amount}')
    (errors if today >= date.fromisoformat(config['size_enforce_from']) else warnings).extend(size_errors)
    return {'errors': sorted(errors), 'warnings': sorted(warnings), 'metrics': current['metrics']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--map', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('snapshot')
    p = sub.add_parser('check')
    p.add_argument('--baseline', required=True)
    p.add_argument('--today')
    args = parser.parse_args()
    try:
        root = Path(args.root)
        config = json.loads(Path(args.map).read_text(encoding='utf-8'))
        if args.command == 'snapshot':
            result, io, errors = scan(root, config)
            errors.extend('domain_io:' + p + ':' + issue for p, issues in io.items() for issue in issues)
            output = {'snapshot': result, 'errors': sorted(errors)}
        else:
            baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
            output = check(root, config, baseline, date.fromisoformat(args.today) if args.today else date.today())
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 1 if output['errors'] else 0
    except (OSError, ValueError, KeyError, SyntaxError, TypeError) as exc:
        print(json.dumps({'errors': ['configuration_or_parse:' + str(exc)], 'warnings': []}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
