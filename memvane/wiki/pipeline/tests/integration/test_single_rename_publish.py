"""Integration tests for single-rename publish, CAS conflict, fence, and 4 crash recoveries."""
from __future__ import annotations

import hashlib
import os

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
            audit_kind='manifest', audit_payload={'action': 'created'},
        )
        self.assertEqual(res1.state, 'published')
        self.assertEqual((self.wiki / 'concepts/alpha.md').read_text(encoding='utf-8'), '# Alpha v1\n')
        self.assertEqual(self._publish_temps(), [])

        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            self.assertEqual(len(store.query_audit('manifest', target_path='concepts/alpha.md')), 1)

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
