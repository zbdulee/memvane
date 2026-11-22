"""Stop a teacher pass right after its k-th durable write.

A durable write is a learning-database transaction that changed a row and
committed, or an atomic replace of a state file (``teacher-host-state.json``,
the files of a training cycle, the application journal, pointer, receipt and
binding). External effects are points of their own: the host CLI returned its
answer, and a stand-in that answers for another process calls ``passed()``
(the resident's prepare and activate answers). ``count`` runs an action in
this process and returns how many points it passed.
``crash_after`` runs one default-teacher pass of a ``TeacherFixture`` in a
child interpreter that leaves with ``os._exit(137)`` right after write ``k``:
no cleanup, no further write, the way a killed worker leaves. The child is a
new process (a fork of a test process is not safe on every supported
Python), so it applies the fixture's stand-ins again from the values the
fixture holds. A fixture whose scope check defers at a ledger stage
(``defer_at``, ``ParkMixin``) is rebuilt with the same deferral for the case
named by ``DEFER_RID``.

``interrupt_after`` is the in-process stand-in for fixtures a new interpreter
cannot rebuild (a whole enrolled install): point ``k`` is the last that
lasts. The action is left by an exception no handler catches, and every
durable write its cleanup still attempts is discarded, as if the process were
gone.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

from memvane.learning.context_learning import local_cycle, shared, teacher_hosts, teacher_state, training_cursor

KILLED = 137
# The case whose ledger stage a deferring scope check reads (``ParkMixin``).
DEFER_RID = 'a1'
FINISHED = 0
# The point function of the counting in effect, for ``passed``.
_ACTIVE = []


def passed():
    """An external effect happened (another process answered): a point when a counting is in effect."""
    if _ACTIVE:
        _ACTIVE[-1]()


class Interrupted(BaseException):
    """The process is gone: nothing that catches ``Exception`` may handle this."""


class _Discard(Exception):
    pass


@contextmanager
def _counting(exit_at=None, *, leave=None):
    """Count the points; at ``exit_at`` call ``leave`` (default: end the process)."""
    seen, gone = [0], []
    # ``candidate_apply`` binds ``_atomic_json`` by name when it is imported: its own
    # reference is replaced too, or the application's file writes would not be points.
    from memvane.learning.context_learning import candidate_apply
    opened, replace, invoke = shared._db_at, teacher_state.atomic_json, teacher_hosts.invoke
    files = local_cycle._atomic_json

    def wrote():
        seen[0] += 1
        if seen[0] == exit_at:
            if leave is None:
                os._exit(KILLED)
            gone.append(True)
            leave()

    @contextmanager
    def database(path):
        if gone:
            # After the end nothing commits: the transaction is rolled back.
            try:
                with opened(path) as db:
                    yield db
                    raise _Discard()
            except _Discard:
                pass
            return
        with opened(path) as db:
            before = db.total_changes
            yield db
            changed = db.total_changes > before
        # Here the transaction is committed and the connection closed.
        if changed:
            wrote()

    def written(real):
        def write(*args, **kwargs):
            if gone:
                return None
            result = real(*args, **kwargs)
            wrote()
            return result
        return write

    def answered(*args, **kwargs):
        result = invoke(*args, **kwargs)
        wrote()  # The host CLI wrote its answer: nothing of it is recorded yet.
        return result

    with mock.patch.object(shared, '_db_at', database), \
            mock.patch.object(teacher_state, 'atomic_json', written(replace)), \
            mock.patch.object(local_cycle, '_atomic_json', written(files)), \
            mock.patch.object(training_cursor, '_atomic_json', written(files)), \
            mock.patch.object(candidate_apply, '_atomic_json', written(files)), \
            mock.patch.object(teacher_hosts, 'invoke', answered):
        _ACTIVE.append(wrote)
        try:
            yield seen
        finally:
            _ACTIVE.pop()


def count(action):
    """The number of durable writes ``action()`` makes."""
    with _counting() as seen:
        action()
    return seen[0]


def interrupt_after(k, action):
    """Run ``action()`` so that point ``k`` is its last; True when it was reached."""
    def leave():
        raise Interrupted()
    with _counting(k, leave=leave):
        try:
            action()
        except Interrupted:
            return True
    return False


def crash_after(k, fixture):
    """One pass of ``fixture`` in a child killed after write ``k``; the child's exit code."""
    spec = {'k': k, 'root': str(fixture.root), 'project': str(fixture.project),
            'policy': fixture.policy, 'proposal': fixture.proposal, 'grant': fixture.grant,
            'environ': fixture.environ, 'deferAt': list(getattr(fixture, 'defer_at', ()))}
    repo = Path(__file__).resolve().parents[4]
    done = subprocess.run([sys.executable, '-B', '-m', __name__], input=json.dumps(spec),
                          cwd=str(repo), env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                          capture_output=True, text=True, timeout=120)
    if done.returncode not in (KILLED, FINISHED):
        raise AssertionError('crash child ended %s: %s' % (done.returncode, done.stderr[-2000:]))
    return done.returncode


def _child(spec):
    import sqlite3
    from memvane.kernel.resource_deferred import ResourceDeferred
    from memvane.learning.context_learning import general_teacher

    def scope(*_args, **_kwargs):
        if spec.get('deferAt'):
            try:
                with shared._db(spec['root']) as db:
                    row = db.execute('SELECT stage FROM project_gold_attempts WHERE request_id=?',
                                     (DEFER_RID,)).fetchone()
            except sqlite3.OperationalError:
                row = None  # No attempt was ever reserved in this namespace.
            if row and row[0] in spec['deferAt']:
                raise ResourceDeferred('fixture_pressure')
        return spec['grant']
    stand_ins = {'_policy': lambda root: spec['policy'],
                 '_scope_default': scope,
                 '_consent_file_v2': lambda root, project: dict(spec['proposal']),
                 '_case_active': lambda *a, **k: True,
                 'resource_report': lambda *a, **k: {'ready': True},
                 '_emit': lambda *a, **k: None}
    for name, value in stand_ins.items():
        setattr(general_teacher, name, value)
    with _counting(spec['k']):
        general_teacher.process_pending_default(spec['root'], spec['project'], spec['policy'],
                                                environ=spec['environ'])


if __name__ == '__main__':
    _child(json.load(sys.stdin))
    os._exit(FINISHED)
