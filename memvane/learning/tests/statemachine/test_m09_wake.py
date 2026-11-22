"""M9 (the ``learning`` wake row): every state under every event.

Real writers: ``WakeQueue.offer`` and the dispatcher's ``drain`` with the pure
``settle`` decision. The handler is a stand-in that answers done or not done.
"""
from __future__ import annotations

import json

from tests.support.case import FixtureCase
from memvane.learning.context_learning.state_model import ABSENT, M9
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import runtime_work as runtime

TARGET = '/synthetic/project'


class Killed(BaseException):
    """Ends a dispatcher inside its handler, past every ``except Exception``."""


class WakeTable(FixtureCase):
    def fresh(self):
        self.root = self.tmp / ('state-%d' % len(list(self.tmp.iterdir())))
        self.root.mkdir(mode=0o700)
        self.queue = runtime.WakeQueue(self.root)
        self.dropped = []

    def drain(self, execute, wall=1000):
        return runtime.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                             wall=lambda: wall, sleep=lambda _: None, max_runs=1,
                             on_drop=lambda kind, target, **row: self.dropped.append((kind, row['reason'], row['attempts'])))

    def row(self):
        with self.queue.connect() as db:
            found = db.execute('SELECT status,attempts,revision,due,reason FROM wakes').fetchall()
        return dict(zip(('status', 'attempts', 'revision', 'due', 'reason'), found[0])) if found else None

    def observed(self):
        row = self.row()
        return ABSENT if row is None else row['status']

    def set(self, **values):
        with self.queue.connect() as db:
            for name, value in values.items():
                db.execute('UPDATE wakes SET %s=?' % name, (value,))

    def build(self, state):
        self.fresh()
        if state != ABSENT:
            self.queue.put('learning', TARGET, now=0)
        if state == 'running':
            self.set(status='running')

    def finish_running(self, done):
        """The dispatcher's own settlement of the row it holds as running."""
        self.set(status='pending')
        return self.drain(lambda _row: done)

    def drive(self, state, event):
        if event == 'request':
            self.queue.put('learning', TARGET, now=5)
        elif event == 'claim':
            seen = []
            def held(_row):
                seen.append(self.observed())
                raise Killed  # The dispatcher dies holding the row.
            if state == 'pending':
                with self.assertRaises(Killed):
                    self.drain(held)
                self.assertEqual(seen, ['running'])
            elif state == ABSENT:
                self.assertEqual(self.drain(held)['runs'], 0)
            # A running row is claimed by no one: the claim updates a pending row only,
            # and a dispatcher that starts re-pends it first (``worker_restart``).
        elif state != 'running':
            # Nothing runs: these events exist only for a row a dispatcher holds.
            return
        elif event == 'done':
            self.finish_running(True)
        elif event == 'not_done':
            self.finish_running(False)
        elif event == 'retry_cap':
            self.set(attempts=policy.LEARNING_RETRY_CAP_ATTEMPTS - 1)
            self.finish_running(False)
        elif event == 'worker_restart':
            # A new dispatcher re-pends what the last one left running; nothing is due yet.
            self.set(due=10**12)
            self.assertEqual(self.drain(lambda _row: True)['status'], 'pending')

    def test_every_wake_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M9.states:
            for event in M9.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(state, event)
                    self.assertEqual(self.observed(), M9.next(state, event))
                cells += 1
                if (state, event) in M9.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M9.states), len(M9.events), cells), (3, 6, 18))
        self.assertEqual(driven, set(M9.transitions))

    def test_a_pass_that_is_not_done_counts_and_backs_off_to_the_ceiling(self):
        self.build('pending')
        for attempts in range(1, 9):
            self.set(due=0)
            self.drain(lambda _row: False)
            row = self.row()
            self.assertEqual((row['attempts'], row['reason']), (attempts, policy.NOT_DONE_REASON))
            self.assertEqual(row['due'], 1000 + min(300, 5 * 2 ** (attempts - 1)))
        self.assertEqual(self.dropped, [])

    def test_the_cap_drops_the_wake_with_its_reason_and_a_later_request_starts_again(self):
        self.build('pending')
        self.set(attempts=policy.LEARNING_RETRY_CAP_ATTEMPTS - 2)
        self.drain(lambda _row: False)
        self.assertEqual((self.observed(), self.dropped), ('pending', []))
        self.set(due=0)
        self.drain(lambda _row: False)
        self.assertEqual(self.observed(), ABSENT)
        self.assertEqual(self.dropped, [('learning', 'retry_cap', policy.LEARNING_RETRY_CAP_ATTEMPTS)])
        entry, = json.loads((self.root / runtime.DROP_DETAIL_FILE).read_text()).values()
        self.assertEqual((entry['kind'], entry['reason']), ('learning', 'retry_cap'))
        self.queue.put('learning', TARGET, now=2000)
        self.assertEqual((self.observed(), self.row()['attempts']), ('pending', 0))

    def test_the_other_background_kind_keeps_no_cap(self):
        step = policy.settle(10 ** 6, done=False, now=0, kind='auto')
        self.assertEqual((step['ack'], step.get('drop')), (False, None))
        self.assertEqual(policy.RETRY_CAPS, {'laya-sync': 144, 'laya-compile': 144, 'learning': 288})

    def test_a_request_while_the_row_runs_survives_its_acknowledgement(self):
        self.build('pending')
        def requested_again(_row):
            self.queue.put('learning', TARGET, now=7)
            return True
        first = self.row()['revision']
        self.drain(requested_again)
        row = self.row()
        self.assertEqual((row['status'], row['attempts']), ('pending', 0))
        self.assertGreater(row['revision'], first)
