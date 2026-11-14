"""S4: settings a user cannot write, given to a use case as a config dict.

The only settings file a user writes is ``{"name", "skipPaths"}``. A test of
a knob outside it (``compile.verify.onFail``, ``semanticDedup`` caps, ...)
builds the effective config with ``expanded(project, compile={...})`` and
passes it to a use case that takes a config argument; a use case that reads
the project settings itself gets it through ``use_config``, which answers
``memvane.settings.api.find_project_config``/``load_project_config`` for that
project root only (every domain calls them as ``settings.api`` attributes).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest import mock


def _merge(base, overrides):
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def expanded(project, **overrides):
    """The project's normalized minimal config with ``overrides`` deep-merged in."""
    from memvane.settings import api as settings
    raw = json.loads(Path(project.settings_path).read_text(encoding='utf-8'))
    folder = Path(project.settings_path).parent.name
    config = settings.normalize_config(settings.expand_minimal_settings(raw, folder), folder)
    return _merge(config, overrides)


def use_config(case, project, config):
    """Answer the settings api lookups for ``project.root`` (and below) with ``config``."""
    from memvane.settings import api as settings
    real_find, real_load = settings.find_project_config, settings.load_project_config
    root = Path(project.root).resolve()

    def owned(cwd):
        path = Path(cwd).resolve()
        return path == root or root in path.parents

    def find(cwd):
        found = real_find(cwd)
        return {**found, 'config': copy.deepcopy(config)} if owned(cwd) else found

    def load(cwd):
        return copy.deepcopy(config) if owned(cwd) else real_load(cwd)

    for name, value in (('find_project_config', find), ('load_project_config', load)):
        patcher = mock.patch.object(settings, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)
