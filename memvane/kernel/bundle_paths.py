"""Resolve process entry paths from the executing plugin package."""
from __future__ import annotations

from pathlib import Path
import sys


def plugin_root():
    return Path(__file__).resolve().parents[2]


def entry_path():
    return plugin_root() / 'entry' / 'run.py'


def entry_argv(name, *args, python=None, isolated=False):
    return [python or sys.executable, *(['-I'] if isolated else []), '-B',
            str(entry_path()), name, *args]
