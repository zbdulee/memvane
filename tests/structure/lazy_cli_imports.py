"""Check fresh-process hook imports and the same modules' CLI entry points."""
import json
import subprocess
import sys

MODULES = ('memvane.settings.config', 'memvane.settings.collection_match', 'memvane.recall.keywords')
READ_MODULES = ('memvane.kernel.atomic_rename', 'memvane.kernel.memvane_paths', 'memvane.journal.ledger_io')


def main():
    loaded = []
    for module in MODULES:
        code = ('import importlib, sys; importlib.import_module(' + repr(module)
                + '); print(int("argparse" in sys.modules))')
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True,
                                timeout=30, check=True)
        if result.stdout.strip() == '1':
            loaded.append(module)
    loaded_tempfile = []
    for module in READ_MODULES:
        code = ('import importlib, sys; importlib.import_module(' + repr(module)
                + '); print(int("tempfile" in sys.modules))')
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True,
                                timeout=30, check=True)
        if result.stdout.strip() == '1':
            loaded_tempfile.append(module)
    help_ok = 0
    for module in MODULES:
        result = subprocess.run([sys.executable, '-B', '-m', module, '--help'], capture_output=True,
                                text=True, timeout=30)
        help_ok += result.returncode == 0 and 'usage:' in result.stdout.lower()
    print(json.dumps({'loaded_argparse': loaded, 'loaded_tempfile': loaded_tempfile, 'help_ok': help_ok}))


if __name__ == '__main__':
    main()
