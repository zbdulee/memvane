"""Loaded only by the hook children of ``wiki_pipeline_e2e.py`` (first on their PYTHONPATH).

Every ``sqlite3.connect`` and every ``runtime.api.request`` call of the child is
appended as one JSON line to ``MEMVANE_E2E_HOOK_LOG``. A connect to a path inside
``MEMVANE_E2E_LEDGER_DIR`` (the project's ``.memvane/compile``) fails after it is
logged, so a hook that opened the wiki pipeline ledger is seen even where the
hook swallows the error. A wake answers ``queued`` as the durable wake queue
would; under ``QMD_SETUP_GUARD_FIXTURE=1`` the real call records nothing.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys

_LOG = os.environ['MEMVANE_E2E_HOOK_LOG']
_LEDGER_DIR = os.environ['MEMVANE_E2E_LEDGER_DIR']
_ROOT = str(Path(__file__).resolve().parents[3])


def _record(row):
    with open(_LOG, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({**row, 'argv': sys.argv[1:3]}) + '\n')


_connect = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    name = os.fsdecode(database) if isinstance(database, (str, bytes, os.PathLike)) else str(database)
    _record({'sqlite': name})
    if _LEDGER_DIR in name:
        raise sqlite3.OperationalError('hook_opened_the_wiki_pipeline_ledger')
    return _connect(database, *args, **kwargs)


sqlite3.connect = _guarded_connect

if _ROOT not in sys.path:
    sys.path.append(_ROOT)
from memvane.runtime import api as _runtime_api  # noqa: E402


def _request(kind, target='', **kwargs):
    _record({'wake': kind, 'target': target})
    return {'status': 'queued'}


_runtime_api.request = _request
