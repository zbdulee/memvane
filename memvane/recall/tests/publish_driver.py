"""Child process of the recall tests: settle one project's index as the sync worker does.

A sync pass and a recall are different processes in the product (worker and
hook); the test that recalls during a publish runs the publish here for the
same reason. Writes one JSON object to ``out``: the published generation ids,
and on a failure its traceback with what identifies the SQLite in use.

Usage: ``python3 -B -m memvane.recall.tests.publish_driver <project root> <journal path> <out>``
"""
from __future__ import annotations

import json
import platform
import sqlite3
import sys
import traceback
import types

PASSES = 8


def _diagnosis(exc):
    return {'type': type(exc).__name__, 'args': [str(item) for item in exc.args],
            'errno': getattr(exc, 'errno', None),
            'sqliteErrorCode': getattr(exc, 'sqlite_errorcode', None),
            'sqliteErrorName': getattr(exc, 'sqlite_errorname', None),
            'sqliteVersion': sqlite3.sqlite_version, 'python': platform.python_version(),
            'platform': platform.platform()}


def settle(project, journal, published):
    from memvane.journal.api import inspect_journal
    from memvane.provider.api import fixture_port
    from memvane.store import api as store_api
    from tests.support import sync_harness
    real_open = store_api.open_store

    class RecordedStore:
        def __init__(self, base):
            self.base = base

        def __getattr__(self, name):
            return getattr(self.base, name)

        def publish_generation(self, handle, gen_id, **kwargs):
            result = self.base.publish_generation(handle, gen_id, **kwargs)
            published.append(gen_id)
            return result

    store_api.open_store = lambda *args, **kwargs: RecordedStore(real_open(*args, **kwargs))
    install = types.SimpleNamespace(project=types.SimpleNamespace(root=project), provider=fixture_port())
    for _ in range(PASSES):
        before = inspect_journal(journal)['epoch']
        result = sync_harness.run_pass(install)
        if result.get('state') == 'unchanged' and inspect_journal(journal)['epoch'] == before:
            return
    raise AssertionError('sync passes did not settle')


def main(argv):
    project, journal, out = argv
    published, report = [], {'published': None, 'error': None}
    try:
        settle(project, journal, published)
    except BaseException as exc:  # noqa: BLE001 - the parent reports it with the diagnosis
        report['error'] = {'traceback': traceback.format_exc(), **_diagnosis(exc)}
    report['published'] = published
    with open(out, 'w', encoding='utf-8') as stream:
        json.dump(report, stream)
    return 1 if report['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
