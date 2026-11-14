from contextlib import closing
import sqlite3

from tests.support.case import FixtureCase
from memvane.runtime import sync_owner_lock as sync


class SyncLeaseTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / 'queue.sqlite'
        self.other = self.tmp / 'other.sqlite'
        for path in (self.queue, self.other):
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE fixture(value TEXT)')
                db.execute("INSERT INTO fixture VALUES('preserved')")
                db.commit()
            path.chmod(0o600)

    def test_issued_claim_excludes_nested_owner_and_allows_explicit_borrow(self):
        with sync.sync_owner(self.queue) as claim:
            self.assertIsNotNone(claim)
            sync.validate_sync_owner(claim, self.queue)
            with sync.sync_owner(self.queue) as nested:
                self.assertIsNone(nested)
            with sync.borrow_sync_owner(self.queue, claim=claim) as borrowed:
                self.assertIs(borrowed, True)
            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                sync.validate_sync_owner(claim, self.other)
            for forged in (True, False, {}, object()):
                with self.subTest(forged=type(forged)):
                    with self.assertRaises((TypeError, ValueError, RuntimeError)):
                        sync.validate_sync_owner(forged, self.queue)
        with self.assertRaises((TypeError, ValueError, RuntimeError)):
            sync.validate_sync_owner(claim, self.queue)
        with sync.sync_owner(self.queue) as claim:
            self.assertIsNotNone(claim)

    def test_replaced_lock_inode_invalidates_existing_claim(self):
        with sync.sync_owner(self.queue) as claim:
            lock = self.tmp / 'sync.lock'
            old = self.tmp / 'old.lock'
            lock.rename(old)
            try:
                lock.touch(mode=0o600)
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    sync.validate_sync_owner(claim, self.queue)
            finally:
                lock.unlink()
                old.rename(lock)
