"""Integration tests for single-rename publish, CAS conflict, fence, and 4 crash recoveries."""
from __future__ import annotations

import errno
import hashlib
import os
import signal

from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import FENCE_GLOBAL_PUBLISH, TEMP_PREFIX, TEMP_SUFFIX
from tests.support.case import FixtureCase
from tests.support.project import make_project


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class SingleRenamePublishTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.wiki = self.project.root / '.memvane/wiki'
        self.wiki.mkdir(parents=True, exist_ok=True)

    def _publish_temps(self):
        return [p for p in self.wiki.rglob(f'{TEMP_PREFIX}*{TEMP_SUFFIX}')]

    def test_new_create_replace_cas_conflict_and_fence_abort(self):
        res1 = pipeline.publish_card(
            self.project.root, self.wiki, 'concepts/alpha.md', '# Alpha v1\n',
            audit_kind='verify_log', audit_payload={'action': 'created'},
            generation={'targetPath': 'concepts/alpha.md', 'action': 'created'},
        )
        self.assertEqual(res1.state, 'published')
        self.assertEqual((self.wiki / 'concepts/alpha.md').read_text(encoding='utf-8'), '# Alpha v1\n')
        self.assertEqual(self._publish_temps(), [])

        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            self.assertEqual(len(store.query_audit('verify_log', target_path='concepts/alpha.md')), 1)
            self.assertEqual(store.generations(target_path='concepts/alpha.md'),
                             [{'targetPath': 'concepts/alpha.md', 'action': 'created'}])

        # User edits card; stale expected_target_sha triggers CAS conflict and preserves user edit.
        (self.wiki / 'concepts/alpha.md').write_text('# User Edited\n', encoding='utf-8')
        conflict = pipeline.publish_card(
            self.project.root, self.wiki, 'concepts/alpha.md', '# Alpha v2\n',
            expected_target_sha=res1.live_sha,
        )
        self.assertEqual(conflict.state, 'conflict_user_edited')
        self.assertEqual((self.wiki / 'concepts/alpha.md').read_text(encoding='utf-8'), '# User Edited\n')
        self.assertEqual(self._publish_temps(), [])

        # Matching expected_target_sha succeeds.
        res2 = pipeline.publish_card(
            self.project.root, self.wiki, 'concepts/alpha.md', '# Alpha v2\n',
            expected_target_sha=_sha('# User Edited\n'),
        )
        self.assertEqual(res2.state, 'published')
        self.assertEqual((self.wiki / 'concepts/alpha.md').read_text(encoding='utf-8'), '# Alpha v2\n')

        # Global publish fence aborts safely.
        with pipeline.open_pipeline(self.project.root) as store:
            store.set_fence(FENCE_GLOBAL_PUBLISH, 'opaque_batch')
        aborted = pipeline.publish_card(self.project.root, self.wiki, 'concepts/beta.md', '# Beta\n')
        self.assertEqual(aborted.state, 'aborted')
        self.assertFalse((self.wiki / 'concepts/beta.md').exists())
        self.assertEqual(self._publish_temps(), [])

    def test_four_crash_recovery_cases_without_st_dev(self):
        # Verify publish_txns schema does not persist st_dev.
        with pipeline.open_pipeline(self.project.root) as store:
            cols = {r['name'] for r in store._conn.execute('PRAGMA table_info(publish_txns)').fetchall()}
            self.assertNotIn('st_dev', cols)
            self.assertNotIn('temp_dev', cols)

        # Case 1: crash before rename, live unchanged -> roll-forward published.
        (self.wiki / 'concepts').mkdir(parents=True, exist_ok=True)
        t1 = self.wiki / 'concepts' / f'{TEMP_PREFIX}tx1{TEMP_SUFFIX}'
        t1.write_text('# Roll Forward\n', encoding='utf-8')
        with pipeline.open_pipeline(self.project.root) as store:
            store.begin_publish_txn(
                'concepts/c1.md', t1.name, os.stat(t1).st_ino, _sha('# Roll Forward\n'), tx_id='tx1',
            )
        r1 = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([(o.tx_id, o.state) for o in r1], [('tx1', 'published')])
        self.assertEqual((self.wiki / 'concepts/c1.md').read_text(encoding='utf-8'), '# Roll Forward\n')
        self.assertFalse(t1.exists())

        # Case 2: crash before rename, user edited live meanwhile -> conflict_user_edited roll-back.
        live2 = self.wiki / 'concepts/c2.md'
        live2.write_text('# Base\n', encoding='utf-8')
        t2 = self.wiki / 'concepts' / f'{TEMP_PREFIX}tx2{TEMP_SUFFIX}'
        t2.write_text('# New Candidate\n', encoding='utf-8')
        with pipeline.open_pipeline(self.project.root) as store:
            store.begin_publish_txn(
                'concepts/c2.md', t2.name, os.stat(t2).st_ino, _sha('# New Candidate\n'),
                expected_target_sha=_sha('# Base\n'), tx_id='tx2',
            )
        live2.write_text('# User Won\n', encoding='utf-8')
        r2 = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([(o.tx_id, o.state) for o in r2], [('tx2', 'conflict_user_edited')])
        self.assertEqual(live2.read_text(encoding='utf-8'), '# User Won\n')
        self.assertFalse(t2.exists())

        # Case 3: crash after rename before commit, live == temp_sha -> published.
        live3 = self.wiki / 'concepts/c3.md'
        live3.write_text('# Renamed Already\n', encoding='utf-8')
        with pipeline.open_pipeline(self.project.root) as store:
            store.begin_publish_txn(
                'concepts/c3.md', f'{TEMP_PREFIX}tx3{TEMP_SUFFIX}', 999999, _sha('# Renamed Already\n'), tx_id='tx3',
            )
        r3 = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([(o.tx_id, o.state) for o in r3], [('tx3', 'published')])

        # Case 4: crash after rename before commit, user re-edited live -> published_user_edited (never overwrite).
        live4 = self.wiki / 'concepts/c4.md'
        live4.write_text('# User Re-Edited\n', encoding='utf-8')
        with pipeline.open_pipeline(self.project.root) as store:
            store.begin_publish_txn(
                'concepts/c4.md', f'{TEMP_PREFIX}tx4{TEMP_SUFFIX}', 999998, _sha('# Earlier Publish\n'), tx_id='tx4',
            )
        r4 = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([(o.tx_id, o.state) for o in r4], [('tx4', 'published_user_edited')])
        self.assertEqual(live4.read_text(encoding='utf-8'), '# User Re-Edited\n')

    def _stop_after(self, name):
        """Raise inside publish right after the named step, as a crash would."""
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish
        real = getattr(publish, name)

        class Crash(Exception):
            pass

        def wrapped(*args, **kwargs):
            real(*args, **kwargs)
            raise Crash
        return patch.object(publish, name, wrapped), Crash

    def _card(self, rel, text):
        path = self.wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_exchange_crash_recovers_clean_and_restores_an_editor_save(self):
        # P7: stopped after the exchange; the displaced file equals the expected base.
        live = self._card('concepts/p7.md', '# Base\n')
        stop, crash = self._stop_after('_exchange')
        with stop, self.assertRaises(crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/p7.md', '# New\n',
                                  expected_target_sha=_sha('# Base\n'))
        self.assertEqual(live.read_text(encoding='utf-8'), '# New\n')
        self.assertEqual(len(self._publish_temps()), 1)
        out = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([o.state for o in out], ['published'])
        self.assertEqual(live.read_text(encoding='utf-8'), '# New\n')
        self.assertEqual(self._publish_temps(), [])

        # P8: an editor saves between the CAS read and the exchange.
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish
        live8 = self._card('concepts/p8.md', '# Base\n')
        real = publish._exchange
        def editor_then_exchange(temp_path, target):
            if target == live8 and live8.read_text(encoding='utf-8') == '# Base\n':
                live8.write_text('# Editor Save\n', encoding='utf-8')
            real(temp_path, target)
        with patch.object(publish, '_exchange', editor_then_exchange):
            res = pipeline.publish_card(self.project.root, self.wiki, 'concepts/p8.md', '# New\n',
                                        expected_target_sha=_sha('# Base\n'))
        self.assertEqual(res.state, 'conflict_user_edited')
        self.assertEqual(live8.read_text(encoding='utf-8'), '# Editor Save\n')
        self.assertEqual(self._publish_temps(), [])

    def test_occupied_new_target_same_bytes_and_human_delete_are_never_overwritten(self):
        # P9: a person created the new card's path first.
        mine = self._card('concepts/p9.md', '# Person\n')
        res = pipeline.publish_card(self.project.root, self.wiki, 'concepts/p9.md', '# Machine\n')
        self.assertEqual(res.state, 'conflict_user_edited')
        self.assertEqual(mine.read_text(encoding='utf-8'), '# Person\n')
        # P10: the same bytes published twice in the same second.
        first = pipeline.publish_card(self.project.root, self.wiki, 'concepts/p10.md', '# Same\n')
        again = pipeline.publish_card(self.project.root, self.wiki, 'concepts/p10.md', '# Same\n')
        self.assertEqual((first.state, again.state), ('published', 'published'))
        self.assertEqual(self._publish_temps(), [])
        # P11: a replacement for a card the person deleted is not resurrected.
        gone = self._card('concepts/p11.md', '# Base\n')
        gone.unlink()
        res = pipeline.publish_card(self.project.root, self.wiki, 'concepts/p11.md', '# New\n',
                                    expected_target_sha=_sha('# Base\n'))
        self.assertEqual(res.state, 'conflict_user_edited')
        self.assertFalse(gone.exists())
        # P12: renamed, then deleted by the person before the txn commit.
        stop, crash = self._stop_after('_create_into_place')
        with stop, self.assertRaises(crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/p12.md', '# Made\n')
        (self.wiki / 'concepts/p12.md').unlink()
        out = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([o.state for o in out], ['published_user_edited'])
        self.assertFalse((self.wiki / 'concepts/p12.md').exists())

    def test_orphan_temp_without_txn_is_swept_and_recovery_ignores_st_dev(self):
        # P2: a temp written before its txn commit.
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish
        from memvane.wiki.pipeline.store import PipelineStore
        class Crash(Exception):
            pass
        with patch.object(PipelineStore, 'begin_publish_txn', side_effect=Crash), self.assertRaises(Crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/p2.md', '# Orphan\n')
        orphan, = self._publish_temps()
        self.assertEqual(pipeline.recover_publishes(self.project.root, self.wiki), [])
        self.assertTrue(orphan.exists())  # Too young: a concurrent writer may still own it.
        old = orphan.stat().st_mtime - 120
        os.utime(orphan, (old, old))
        pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual(self._publish_temps(), [])
        self.assertFalse((self.wiki / 'concepts/p2.md').exists())
        # P13: stopped between the txn commit and the rename, recovered after a reboot that
        # changed every st_dev: the decision uses (st_ino, sha256) only.
        with patch.object(publish, '_apply', side_effect=Crash), self.assertRaises(Crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/p13.md', '# Boot\n')
        real_fstat = os.fstat
        def rebooted(fd):
            info = real_fstat(fd)
            values = list(info)
            values[2] = info.st_dev + 1  # st_dev
            return os.stat_result(values)
        with patch.object(os, 'fstat', rebooted):
            out = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([o.state for o in out], ['published'])
        self.assertEqual((self.wiki / 'concepts/p13.md').read_text(encoding='utf-8'), '# Boot\n')

    def test_person_creating_the_path_after_the_cas_read_keeps_their_file(self):
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish
        real = publish._create_into_place
        def person_first(temp_path, target):
            target.write_text('# Person\n', encoding='utf-8')
            real(temp_path, target)
        with patch.object(publish, '_create_into_place', person_first):
            res = pipeline.publish_card(self.project.root, self.wiki, 'concepts/race.md', '# Machine\n')
        self.assertEqual(res.state, 'conflict_user_edited')
        self.assertEqual((self.wiki / 'concepts/race.md').read_text(encoding='utf-8'), '# Person\n')
        self.assertEqual(self._publish_temps(), [])

    def test_generation_row_lands_only_with_the_card_including_recovery(self):
        row = {'targetPath': 'concepts/g.md', 'action': 'created', 'sourceHash': 'h'}
        stop, crash = self._stop_after('_create_into_place')
        with stop, self.assertRaises(crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/g.md', '# G\n', generation=row)
        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            self.assertEqual(store.generations(target_path='concepts/g.md'), [])
        pipeline.recover_publishes(self.project.root, self.wiki)
        pipeline.recover_publishes(self.project.root, self.wiki)
        self._card('concepts/taken.md', '# Person\n')
        lost = pipeline.publish_card(self.project.root, self.wiki, 'concepts/taken.md', '# M\n',
                                     generation={'targetPath': 'concepts/taken.md', 'action': 'created'})
        self.assertEqual(lost.state, 'conflict_user_edited')
        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            self.assertEqual(store.generations(target_path='concepts/g.md'), [row])
            self.assertEqual(store.generations(target_path='concepts/taken.md'), [])

    # I1: one txn recovery cannot settle must not stall the others or the caller's pass.
    def _interrupt(self, rel, text, expected=None):
        """Real writer up to the txn commit, then a stop before the card lock."""
        class Crash(Exception):
            pass

        def crash():
            raise Crash
        (self.wiki / rel).parent.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(Crash):
            pipeline.publish_card(self.project.root, self.wiki, rel, text,
                                  expected_target_sha=expected, lock_context=crash)
        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            return [r for r in store.open_publish_txns() if r['target_path'] == rel][0]

    def _folder_became_symlink(self):
        row = self._interrupt('concepts/a.md', '# A\n')
        elsewhere = self.project.root / 'elsewhere'
        os.rename(self.wiki / 'concepts', elsewhere)
        os.symlink(elsewhere, self.wiki / 'concepts')
        return row, elsewhere / row['temp_name'], 'unrecoverable:unsafe_target_path'

    def _target_became_directory(self):
        row = self._interrupt('concepts/d.md', '# D\n')
        (self.wiki / 'concepts/d.md').mkdir()
        return row, self.wiki / 'concepts' / row['temp_name'], 'unrecoverable:unsafe_publish_file'

    def _target_became_fifo(self):
        row = self._interrupt('concepts/f.md', '# F\n')
        os.mkfifo(self.wiki / 'concepts/f.md')
        return row, self.wiki / 'concepts' / row['temp_name'], 'unrecoverable:unsafe_publish_file'

    def _parent_became_file(self):
        row = self._interrupt('gone/p.md', '# P\n')
        for child in (self.wiki / 'gone').iterdir():
            child.unlink()
        (self.wiki / 'gone').rmdir()
        (self.wiki / 'gone').write_text('a file now\n', encoding='utf-8')
        return row, None, 'unrecoverable:ENOTDIR'

    def _exchange_unsupported(self):
        live = self._card('concepts/x.md', '# Base\n')
        row = self._interrupt('concepts/x.md', '# New\n', expected=_sha('# Base\n'))
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish

        def refuse(*_args, **_kwargs):
            raise OSError(errno.ENOTSUP, 'exchange not supported')
        self._exchange_patch = patch.object(publish, 'rename_exchange', refuse)
        self._exchange_live = live
        return row, self.wiki / 'concepts' / row['temp_name'], 'unrecoverable:ENOTSUP'

    def test_one_unrecoverable_txn_is_aborted_and_the_others_still_recover(self):
        from contextlib import nullcontext

        def hang(_signum, _frame):
            raise AssertionError('recovery blocked on a file it cannot read')
        previous = signal.signal(signal.SIGALRM, hang)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for case in ('_folder_became_symlink', '_target_became_directory', '_target_became_fifo',
                     '_parent_became_file', '_exchange_unsupported'):
            with self.subTest(case=case):
                # A project per case: a failing case cannot leave state the next one trips on.
                self.project = make_project(self.home, git=False, dirname='project' + case)
                self.wiki = self.project.root / '.memvane/wiki'
                self._exchange_patch = None
                bad, temp, reason = getattr(self, case)()
                good = self._interrupt(f'other/{case}.md', '# Good\n')
                signal.alarm(10)
                try:
                    with self._exchange_patch or nullcontext():
                        first = pipeline.recover_publishes(self.project.root, self.wiki)
                        again = pipeline.recover_publishes(self.project.root, self.wiki)
                finally:
                    signal.alarm(0)
                by_tx = {o.tx_id: o for o in first}
                self.assertEqual((by_tx[bad['tx_id']].state, by_tx[bad['tx_id']].reason), ('aborted', reason))
                self.assertEqual(by_tx[good['tx_id']].state, 'published')
                self.assertEqual((self.wiki / f'other/{case}.md').read_text(encoding='utf-8'), '# Good\n')
                self.assertEqual(again, [], 'nothing stays open to fail the next pass')
                with pipeline.open_pipeline(self.project.root, readonly=True) as store:
                    self.assertEqual(store.open_publish_txns(), [])
                    self.assertEqual(store.generations(target_path=bad['target_path']), [])
                if temp is not None:
                    self.assertTrue(temp.exists(), 'recovery leaves the files of a txn it cannot settle')
                if case == '_exchange_unsupported':
                    self.assertEqual(self._exchange_live.read_text(encoding='utf-8'), '# Base\n')

    def test_a_stop_between_closing_a_conflict_and_dropping_its_temp_is_not_a_user_edit(self):
        """N3: the txn closes before the temp goes, so recovery never reads a dropped temp as a publish."""
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish

        class Crash(Exception):
            pass
        real = publish._unlink_if_present

        def drop_then_stop(path):
            real(path)
            if path.name.startswith(TEMP_PREFIX):
                raise Crash
        for rel, expected in (('concepts/n3-new.md', None), ('concepts/n3-replace.md', _sha('# Base\n'))):
            with self.subTest(rel=rel):
                self._card(rel, '# Person\n')
                with patch.object(publish, '_unlink_if_present', drop_then_stop), self.assertRaises(Crash):
                    pipeline.publish_card(self.project.root, self.wiki, rel, '# Machine\n',
                                          expected_target_sha=expected,
                                          generation={'targetPath': rel, 'action': 'created'})
                self.assertEqual(pipeline.recover_publishes(self.project.root, self.wiki), [])
                with pipeline.open_pipeline(self.project.root, readonly=True) as store:
                    self.assertEqual(store.generations(target_path=rel), [])
                    states = [r['state'] for r in store._conn.execute(
                        'SELECT state FROM publish_txns WHERE target_path = ?', (rel,))]
                self.assertEqual(states, ['conflict_user_edited'])
                self.assertEqual((self.wiki / rel).read_text(encoding='utf-8'), '# Person\n')

    def test_a_closed_txn_leftover_goes_only_when_a_later_publish_can_rewrite_it(self):
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish

        def refuse(*_args, **_kwargs):
            raise OSError(errno.ENOTSUP, 'exchange not supported')
        rows = []
        for rel in ('concepts/own.md', 'concepts/foreign.md'):
            self._card(rel, '# Base\n')
            rows.append(self._interrupt(rel, '# New\n', expected=_sha('# Base\n')))
        own, foreign = (self.wiki / 'concepts' / row['temp_name'] for row in rows)
        foreign.unlink()
        foreign.write_text('# Someone else\n', encoding='utf-8')
        with patch.object(publish, 'rename_exchange', refuse):
            out = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual(sorted(o.state for o in out), ['aborted', 'aborted'])
        for temp in (own, foreign):
            old = temp.stat().st_mtime - 120
            os.utime(temp, (old, old))
        pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertFalse(own.exists(), 'its own candidate bytes come back with the next publish')
        self.assertEqual(foreign.read_text(encoding='utf-8'), '# Someone else\n')

    def test_a_temp_the_sweep_cannot_remove_does_not_stop_recovery(self):
        from unittest.mock import patch
        from memvane.wiki.pipeline import publish
        from memvane.wiki.pipeline.store import PipelineStore

        class Crash(Exception):
            pass
        with patch.object(PipelineStore, 'begin_publish_txn', side_effect=Crash), self.assertRaises(Crash):
            pipeline.publish_card(self.project.root, self.wiki, 'concepts/stuck.md', '# Orphan\n')
        orphan, = self._publish_temps()
        old = orphan.stat().st_mtime - 120
        os.utime(orphan, (old, old))
        good = self._interrupt('other/fine.md', '# Fine\n')

        def refuse(_path):
            raise PermissionError(13, 'denied')
        with patch.object(publish, '_unlink_if_present', refuse):
            out = pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertEqual([(o.tx_id, o.state) for o in out], [(good['tx_id'], 'published')])
        self.assertTrue(orphan.exists())
        pipeline.recover_publishes(self.project.root, self.wiki)
        self.assertFalse(orphan.exists(), 'the next pass removes it')
