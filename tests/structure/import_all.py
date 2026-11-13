"""Import every product module under the caller's (synthetic) HOME.

Package modules import in one process (order: sorted); script files
(entry/*.py, skills/**/scripts/*.py) load in their own subprocess so their
sys.path edits cannot mask a package failure. Exit 1 on any failure.
"""
import importlib, importlib.util, json, os, subprocess, sys, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
mode = sys.argv[1] if len(sys.argv) > 1 else 'packages'

if mode == 'script':
    path = Path(sys.argv[2])
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('s5_script_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.exit(0)

sys.path.insert(0, str(ROOT))
modules = []
for base in ('memvane', 'hermes_adapter'):
    for path in sorted((ROOT / base).rglob('*.py')):
        rel = path.relative_to(ROOT)
        if '__pycache__' in rel.parts or 'tests' in rel.parts:
            continue
        parts = list(rel.with_suffix('').parts)
        if parts[-1] == '__init__':
            parts = parts[:-1]
        modules.append('.'.join(parts))
failures = {}
for name in modules:
    try:
        importlib.import_module(name)
    except BaseException as exc:  # noqa: BLE001 - report every kind
        failures[name] = ''.join(traceback.format_exception_only(type(exc), exc)).strip()
# Every process entry named in entry/run.py resolves to a callable.
import ast
for node in ast.parse((ROOT / 'entry/run.py').read_text(encoding='utf8')).body:
    if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'TARGETS' for t in node.targets):
        for command, (target, _prefix, _argv) in ast.literal_eval(node.value).items():
            module_name, function_name = target.split(':', 1)
            try:
                if not callable(getattr(importlib.import_module(module_name), function_name)):
                    failures['entry:' + command] = target + ' is not callable'
            except BaseException as exc:  # noqa: BLE001
                failures['entry:' + command] = target + ': ' + type(exc).__name__ + ': ' + str(exc)
        break
else:
    failures['entry/run.py'] = 'TARGETS table not found'
scripts = [ROOT / 'entry/run.py', ROOT / 'entry/setup.py'] + sorted(ROOT.glob('skills/*/scripts/*.py'))
for script in scripts:
    # entry/setup.py is an alias that always runs; its import check is a --help run.
    command = ([str(script), '--help'] if script.name == 'setup.py' and script.parent.name == 'entry'
               else [__file__, 'script', str(script)])
    proc = subprocess.run([sys.executable, '-B', *command], capture_output=True, text=True, timeout=60)
    if proc.returncode:
        failures[str(script.relative_to(ROOT))] = (proc.stderr.strip().splitlines() or ['exit %d' % proc.returncode])[-1]
print(json.dumps({'python': sys.version.split()[0], 'modules': len(modules), 'scripts': len(scripts),
                  'failures': failures}, indent=1, sort_keys=True))
sys.exit(1 if failures else 0)
