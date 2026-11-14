"""S5: the host-CLI extractor engines without a host CLI.

``FakeEngines(responses).install(case)`` replaces
``memvane.wiki.engines.api.run_extractor``; each call is recorded in
``calls`` as ``{'task', 'engine', 'argv'}`` so a paid-call cap is asserted
with ``len(fake.calls)``. ``responses`` maps a payload ``task`` (``'verify'``,
``'dedup'``, ``'extract'``) to a result dict or a callable
``(payload, argv) -> dict``. ``returncode=127`` stands in for an absent CLI,
``timeout=True`` for a timed-out call and ``garbage=True`` for stdout that is
not a JSON object, each with the extractor's own ``(result, reason, code)``.

``script(tmp, payload)`` writes a real extractor executable that prints
``payload`` for tests that cross the process boundary.

Reach: the compile worker, the dedup judge and the verify worker all look
``run_extractor`` up on ``engines.api`` at call time.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import sys
from unittest import mock


def _engine(argv):
    # Builtin adapters end with `extractor-<engine>`; a bare host name is also accepted.
    # Whole elements only, so a repository path that contains a host name never decides it.
    for part in reversed([str(part) for part in argv or ()]):
        name = os.path.basename(part)
        if name.startswith('extractor-'):
            return name[len('extractor-'):] or None
        if name in ('claude', 'codex', 'hermes', 'gemini'):
            return name
    return None


class FakeEngines:
    def __init__(self, responses=None, *, returncode=None, timeout=False, garbage=False):
        self.responses = dict(responses or {})
        self.returncode = returncode
        self.timeout = timeout
        self.garbage = garbage
        self.calls = []

    def run_extractor(self, argv, payload, timeout, root):
        task = payload.get('task', 'extract') if isinstance(payload, dict) else None
        self.calls.append({'task': task, 'engine': _engine(argv), 'argv': list(argv or ())})
        if self.returncode == 127:
            return None, 'extractor_failed', 127
        if self.returncode not in (None, 0):
            return None, 'extractor_failed', self.returncode
        if self.timeout:
            return None, 'extractor_timeout', None
        if self.garbage:
            return None, 'invalid_extractor_json', 0
        response = self.responses.get(task)
        if callable(response):
            response = response(payload, list(argv or ()))
        if not isinstance(response, dict):
            return None, 'invalid_extractor_json', 0
        return copy.deepcopy(response), None, 0

    def install(self, case):
        patcher = mock.patch('memvane.wiki.engines.api.run_extractor', self.run_extractor)
        patcher.start()
        case.addCleanup(patcher.stop)
        return self


def script(tmp, payload_json):
    """argv of an executable that ignores stdin and prints ``payload_json``."""
    path = Path(tmp) / ('fake-extractor-%d.py' % len(list(Path(tmp).glob('fake-extractor-*.py'))))
    text = payload_json if isinstance(payload_json, str) else json.dumps(payload_json)
    path.write_text('import sys\nsys.stdin.read()\nsys.stdout.write(%r)\n' % text, encoding='utf-8')
    os.chmod(path, stat.S_IRWXU)
    return [sys.executable, '-B', str(path)]
