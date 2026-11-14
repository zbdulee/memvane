from contextlib import closing
import os
import sqlite3
import threading
from unittest.mock import patch

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

    def test_symlink_hardlink_and_queue_alias_are_rejected(self):
        marker = self.tmp / 'marker'
        marker.write_text('preserved', encoding='utf-8')
        marker.chmod(0o600)
        lock = self.tmp / 'sync.lock'
        saved = self.tmp / 'saved.lock'
        alias = self.tmp / 'alias.lock'
        with sync.sync_owner(self.queue) as claim:
            for mode in ('symlink', 'hardlink'):
                with self.subTest(mode=mode):
                    if mode == 'symlink':
                        lock.rename(saved)
                        lock.symlink_to(marker)
                    else:
                        os.link(lock, alias)
                    try:
                        with self.assertRaises((TypeError, ValueError, RuntimeError)):
                            sync.validate_sync_owner(claim, self.queue)
                        self.assertEqual(marker.read_text(), 'preserved')
                    finally:
                        if mode == 'symlink':
                            lock.unlink()
                            saved.rename(lock)
                        else:
                            alias.unlink()
                    sync.validate_sync_owner(claim, self.queue)
            queue_alias = self.tmp / 'queue-alias'
            queue_alias.symlink_to(self.queue)
            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                sync.validate_sync_owner(claim, queue_alias)

    def test_expired_pin_keeps_new_owner_out_when_lock_path_changes(self):
        with sync.sync_owner(self.queue) as claim:
            pin = claim.pin()
        lock = self.tmp / 'sync.lock'
        old = self.tmp / 'old.lock'
        try:
            for mode in ('missing', 'replaced', 'queue-and-lock-replaced'):
                with self.subTest(mode=mode):
                    saved_queue = self.tmp / 'old.sqlite'
                    if mode == 'queue-and-lock-replaced':
                        self.queue.rename(saved_queue)
                        with closing(sqlite3.connect(self.queue)) as db:
                            db.execute('CREATE TABLE replacement(value TEXT)')
                            db.commit()
                        self.queue.chmod(0o600)
                    lock.rename(old)
                    try:
                        if mode != 'missing':
                            lock.write_bytes(b'foreign lock')
                            lock.chmod(0o600)
                        with sync.sync_owner(self.queue) as blocked:
                            self.assertIsNone(blocked)
                        if mode == 'missing':
                            self.assertFalse(lock.exists())
                        else:
                            self.assertEqual(lock.read_bytes(), b'foreign lock')
                    finally:
                        if lock.exists():
                            lock.unlink()
                        old.rename(lock)
                        if saved_queue.exists():
                            self.queue.unlink()
                            saved_queue.rename(self.queue)
        finally:
            pin.close()

    def test_close_only_pin_holds_exclusion_without_extending_authority(self):
        with sync.sync_owner(self.queue) as claim:
            pin = claim.pin()
        try:
            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                sync.validate_sync_owner(claim, self.queue)
            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                sync.validate_sync_owner(pin, self.queue)
            with sync.sync_owner(self.queue) as busy:
                self.assertIsNone(busy)
        finally:
            pin.close()
            pin.close()
        with sync.sync_owner(self.queue) as fresh:
            self.assertIsNotNone(fresh)

    def test_other_thread_cannot_borrow_issued_claim(self):
        observed = []
        with sync.sync_owner(self.queue) as claim:
            def run():
                try:
                    sync.validate_sync_owner(claim, self.queue)
                except (TypeError, ValueError, RuntimeError):
                    observed.append('rejected')
                try:
                    with sync.borrow_sync_owner(self.queue, claim=claim) as borrowed:
                        observed.append(borrowed)
                except (TypeError, ValueError, RuntimeError):
                    observed.append('borrow-rejected')
                with sync.borrow_sync_owner(self.queue) as borrowed:
                    observed.append(borrowed)
            thread = threading.Thread(target=run)
            thread.start()
            thread.join()
            self.assertEqual(observed, ['rejected', 'borrow-rejected', False])
        with sync.borrow_sync_owner(self.queue) as borrowed:
            self.assertIs(borrowed, True)

    def test_caller_error_survives_and_owner_lock_is_released(self):
        marker = OSError('synthetic')
        try:
            with sync.sync_owner(self.queue):
                raise marker
        except OSError as observed:
            self.assertIs(observed, marker)
        else:
            self.fail('caller error lost')
        with sync.sync_owner(self.queue) as claim:
            self.assertIsNotNone(claim)

    def test_anchor_rejects_invalid_or_nonprivate_fixture_roots(self):
        public = self.tmp / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        for root in ('relative/root', '/usr/share', str(public)):
            with self.subTest(root=root), patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': root}):
                with self.assertRaises((ValueError, RuntimeError)):
                    with sync.sync_owner(self.queue):
                        self.fail('unsafe root admitted')
