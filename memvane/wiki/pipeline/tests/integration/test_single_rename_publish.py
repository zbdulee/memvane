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
