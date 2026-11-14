"""Resolve process entry paths from the executing plugin package."""
from __future__ import annotations

from pathlib import Path
import sys

# Bundle-relative provider files. Attestation ``providerRelativePath`` and the
# onboarding artifact sha map record these exact strings.
PROVIDER_ADAPTERS_PREFIX = 'memvane/provider/adapters/'
PROVIDER_RELATIVE_PATH = PROVIDER_ADAPTERS_PREFIX + 'local_embed_provider.py'
WORKER_RELATIVE_PATH = PROVIDER_ADAPTERS_PREFIX + 'laya_local_embed_worker.mjs'
CHUNKER_RELATIVE_PATH = PROVIDER_ADAPTERS_PREFIX + 'laya_qmd_chunking.mjs'


def plugin_root():
    return Path(__file__).resolve().parents[2]


def entry_path():
    return plugin_root() / 'entry' / 'run.py'


def entry_argv(name, *args, python=None, isolated=False):
    return [python or sys.executable, *(['-I'] if isolated else []), '-B',
            str(entry_path()), name, *args]


def is_provider_relative_path(value):
    return type(value) is str and value.startswith(PROVIDER_ADAPTERS_PREFIX)


def bundle_file(relative):
    return plugin_root() / relative
