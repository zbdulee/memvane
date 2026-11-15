"""GB6 real validation failures versus elapsed-deadline deferral; no models."""
import json
import sqlite3
import struct
import unittest
from unittest import mock

from memvane.store.tests import support as vectors


class ReaderReviewChecks(unittest.TestCase):
    def compose(self):
        case = vectors.VectorCase('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    @staticmethod
    def emit(**row):
        print(json.dumps(row, sort_keys=True), flush=True)

    def corruption(self, hybrid):
        case = self.compose()
        store, _, _, _, _, profile, _, pin = case.ready()
        baseline = store.search(pin, 'hello', 10)
        original_vectors, original_search = store._vectors, store.search
        clock, observed, lexical = [100.0], [], []

        def damage():
            case.damage('vectors', 'UPDATE vectors SET embedding=?',
                        (struct.pack('<ff', 0.5, 1.0),))

        def actual_vectors(*args, **kwargs):
            try:
                return original_vectors(*args, **kwargs)
            except case.Error as exc:
                # Observe the genuine byte/provenance validator first. No
                # progress-handler expiry can manufacture this failure.
                observed.append(exc.reason)
                clock[0] = 200.0
                raise

        def lexical_then_damage(*args, **kwargs):
            rows = original_search(*args, **kwargs)
            lexical.extend(rows)
            damage()
            return rows

        if not hybrid:
            damage()
        outcome, returned = None, None
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(store, '_vectors', side_effect=actual_vectors), \
                mock.patch.object(store, 'search', side_effect=lexical_then_damage if hybrid else original_search):
            try:
                returned = (store.search_hybrid(pin, 'hello', [1.0, 0.0], 10,
                                               profile_id=profile, deadline_seconds=2)
                            if hybrid else store.search_exact(pin, [1.0, 0.0], 10,
                                                              profile_id=profile, deadline_seconds=2))
            except case.Error as exc:
                outcome = exc.reason
        self.emit(case='hybrid-corruption' if hybrid else 'exact-corruption',
                  actual_validation_errors=observed, returned_error=outcome,
                  semantic_status=None if returned is None else returned.get('semantic_status'),
                  lexical_completed=len(lexical), fake_clock=clock[0])
        self.assertEqual(len(observed), 1, 'setup must reach the real vector validator before expiry')
        self.assertNotIn(observed[0], ('fts_deadline_exceeded', 'vector_deadline_exceeded'))
        if hybrid:
            self.assertEqual(lexical, baseline)
        self.assertEqual(outcome, observed[0], 'deadline must preserve the actual corruption refusal')

    def test_exact_preserves_actual_vector_corruption_after_deadline(self):
        self.corruption(False)

    def test_hybrid_does_not_hide_post_lexical_corruption_as_deferred(self):
        self.corruption(True)

    def test_reader_context_preserves_actual_checkout_binding_refusal(self):
        case = self.compose()
        store, _, handle, _, _, _, _, _ = case.ready()
        saved = dict(store._db.execute('SELECT * FROM worktrees WHERE worktree_id=?',
                                      (handle.worktree_id,)).fetchone())
        capture = store._check(handle)['capture']
        settings = case.root / '.memvane' / 'settings.json'
        settings.write_text(json.dumps(dict(json.loads(settings.read_bytes()), name='renamed')))
        observed, clock, outcome = [], [100.0], None
        baseline_error = None
        try:
            case.views._current_capture(saved, store._repository, store._policy, capture)
        except case.Error as exc:
            baseline_error = exc.reason
        self.assertIsNotNone(baseline_error, 'actual changed settings must refuse before the timed scenario')
        # _current_capture uses the same real checkout/settings observation as
        # writer commit validation, without nesting a SQLite transaction. This
        # isolates _reading's exception contract; it is not a claim that exact
        # search normally calls the writer's commit validator.
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]):
            try:
                with store._reading(102.0):
                    try:
                        case.views._current_capture(saved, store._repository, store._policy, capture)
                    except case.Error as exc:
                        observed.append(exc.reason)
                        clock[0] = 200.0
                        raise
            except case.Error as exc:
                outcome = exc.reason
        self.emit(case='reader-checkout-context', actual_validation_errors=observed,
                  baseline_error=baseline_error, returned_error=outcome, fake_clock=clock[0])
        self.assertEqual(observed, [baseline_error])
        self.assertEqual(outcome, observed[0])

    def test_exact_checkout_observation_outside_reader_stays_fail_closed(self):
        case = self.compose()
        store, _, _, _, _, profile, _, pin = case.ready()
        settings = case.root / '.memvane' / 'settings.json'
        settings.write_text(json.dumps(dict(json.loads(settings.read_bytes()), name='renamed')))
        actual, clock, observed = store._observe, [100.0], []

        def observe(*args,**kwargs):
            try:
                return actual(*args,**kwargs)
            except case.Error as exc:
                observed.append(exc.reason)
                clock[0] = 200.0
                raise

        outcome = None
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(store, '_observe', side_effect=observe):
            try:
                store.search_exact(pin, [1.0, 0.0], 10, profile_id=profile)
            except case.Error as exc:
                outcome = exc.reason
        self.emit(case='exact-checkout-outer-control', actual_validation_errors=observed,
                  returned_error=outcome)
        self.assertEqual(len(observed), 1)
        self.assertEqual(outcome, observed[0])
        self.assertNotIn(outcome, ('fts_deadline_exceeded', 'vector_deadline_exceeded'))

    def test_actual_sqlite_interrupt_in_semantic_lane_preserves_lexical(self):
        case = self.compose()
        store, _, _, _, _, profile, _, pin = case.ready()
        baseline = store.search(pin, 'hello', 10)
        # The semantic lane's first store read is the pinned generation check
        # (_rows_verified): it reuses the pin's last full verification while no
        # commit reached the store, and runs inside the lane's read transaction.
        original_search, original_read = store.search, store._rows_verified
        clock, lexical_done, sqlite_errors = [100.0], [], []

        def lexical(*args, **kwargs):
            rows = original_search(*args, **kwargs)
            lexical_done.append(True)
            return rows

        def interrupt_semantic(db, *args, **kwargs):
            if not lexical_done:
                return original_read(db, *args, **kwargs)

            def progress():
                clock[0] = 200.0
                return 1

            db.set_progress_handler(progress, 1)
            try:
                # SQLite itself raises SQLITE_INTERRUPT; no fabricated error.
                db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL '
                           'SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n').fetchone()
            except sqlite3.Error as exc:
                sqlite_errors.append(dict(code=getattr(exc, 'sqlite_errorcode', None),
                                          args=list(exc.args), message=str(exc)))
                raise
            finally:
                db.set_progress_handler(None, 0)

        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(store, 'search', side_effect=lexical), \
                mock.patch.object(store, '_rows_verified', side_effect=interrupt_semantic):
            result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10,
                                         profile_id=profile, deadline_seconds=2)
        self.emit(case='actual-sqlite-interrupt-control', sqlite_errors=sqlite_errors,
                  semantic_status=result['semantic_status'], lexical_count=len(result['lexical']))
        self.assertEqual(len(sqlite_errors), 1)
        # sqlite_errorcode is unavailable on older supported Python versions.
        self.assertIn(sqlite_errors[0]['code'], (None, getattr(sqlite3, 'SQLITE_INTERRUPT', 9)))
        self.assertEqual(sqlite_errors[0]['args'], ['interrupted'])
        self.assertEqual(sqlite_errors[0]['message'], 'interrupted')
        self.assertEqual(result['lexical'], baseline)
        self.assertEqual(result['semantic'], [])
        self.assertEqual(result['semantic_status'], 'deferred_deadline')

    def test_deadline_after_actual_lexical_search_does_not_dispatch_semantic(self):
        case = self.compose()
        store, _, _, _, _, profile, _, pin = case.ready()
        original, clock = store.search, [100.0]
        baseline = original(pin, 'hello', 10)

        def lexical(*args, **kwargs):
            rows = original(*args, **kwargs)
            clock[0] = 200.0
            return rows

        with mock.patch('time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(store, 'search', side_effect=lexical), \
                mock.patch.object(store, 'search_exact', wraps=store.search_exact) as exact:
            result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10,
                                         profile_id=profile, deadline_seconds=2)
        self.emit(case='elapsed-deadline-control', exact_calls=exact.call_count,
                  semantic_status=result['semantic_status'], lexical_count=len(result['lexical']))
        self.assertEqual(exact.call_count, 0)
        self.assertEqual(result['lexical'], baseline)
        self.assertEqual(result['semantic'], [])
        self.assertEqual(result['semantic_status'], 'deferred_deadline')

    def test_unrelated_actual_sqlite_error_after_deadline_is_not_interruption(self):
        case = self.compose()
        store, _, _, _, _, _, _, _ = case.ready()
        clock, observed, outcome = [100.0], [], None
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]):
            try:
                with store._reading(102.0) as db:
                    try:
                        db.execute('SELECT * FROM nonexistent_gb6_fixture_table')
                    except sqlite3.Error as exc:
                        observed.append(str(exc))
                        clock[0] = 200.0
                        raise
            except case.Error as exc:
                outcome = exc.reason
        self.emit(case='actual-sqlite-non-interrupt', sqlite_errors=observed,
                  returned_error=outcome)
        self.assertEqual(observed, ['no such table: nonexistent_gb6_fixture_table'])
        self.assertEqual(outcome, 'candidate_io_failed')

    def test_actual_interrupt_before_deadline_is_not_expired_deadline(self):
        case = self.compose()
        store, _, _, _, _, _, _, _ = case.ready()
        clock, observed, outcome = [100.0], [], None
        with mock.patch('time.monotonic', side_effect=lambda: clock[0]):
            try:
                with store._reading(102.0) as db:
                    db.set_progress_handler(lambda: 1, 1)
                    try:
                        db.execute('SELECT count(*) FROM s_cards').fetchone()
                    except sqlite3.Error as exc:
                        observed.append(str(exc))
                        raise
                    finally:
                        db.set_progress_handler(None, 0)
            except case.Error as exc:
                outcome = exc.reason
        self.emit(case='actual-sqlite-interrupt-before-deadline', sqlite_errors=observed,
                  returned_error=outcome)
        self.assertEqual(observed, ['interrupted'])
        self.assertEqual(outcome, 'candidate_io_failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
