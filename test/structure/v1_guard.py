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
            if left == right or right == 'kernel':
                continue
            edge = f'{src}:{lineno}:{dst}'
            if dst.startswith('memvane/') and not dst.endswith(('/api.py', '/types.py')):
                issues['G1'].append(edge)
            if left != 'app' and ranks[right] >= ranks[left]:
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
        if len(exported) > 30:
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
    return snapshot, closures, {key: sorted(set(value)) for key, value in issues.items()}, unknown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', default=str(ROOT / 'test/structure/v1-baseline.json'))
    parser.add_argument('--write-baseline', action='store_true')
    args = parser.parse_args()
    config = json.loads((ROOT / 'test/structure/context-map.json').read_text(encoding='utf8'))
    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text(encoding='utf8')) if baseline_path.exists() else None
    snapshot, closures, issues, unknown = inspect(config, baseline)
    if args.write_baseline:
        exceptions = {kind: {p: n for p, n in snapshot[kind].items() if n > limit}
                      for kind, limit in (('file_sizes', 800), ('function_sizes', 150))}
        baseline = {'size_exceptions': exceptions, 'hook_closures': closures}
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
        snapshot, closures, issues, unknown = inspect(config, baseline)
    print(json.dumps({'counts': {k: len(v) for k, v in issues.items()},
                      'issues': issues, 'unknown_patch_targets': unknown,
                      'limitations': ['G1 and G2 cover resolved import statements in classified source modules; test imports and nonliteral dynamic imports are not yet classified', 'G6 closure follows static top-level imports and requires hook target verification', 'G7 adapter relative imports and indirect I/O require further analysis', 'G8 examines statically identifiable Python patch calls; JavaScript embedded Python and dynamic targets require further analysis'],
                      'metrics': snapshot['metrics']}, ensure_ascii=False, sort_keys=True))
    return 1 if any(issues.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
