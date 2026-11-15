"""Reader lifetime leases: one private lock per install, opened read-only, never created by readers.

A reader holds the lock shared for as long as its endpoint lives; restore
takes it exclusive. Each check re-proves the lock's path and inode.
"""
from contextlib import ExitStack
import os
import threading
import unittest
from unittest import mock

from memvane.journal import journal_reader
from memvane.journal.reader_windows import ReaderWindowBusy, _Lifetime
from memvane.journal.tests.support import InstallCase, file_state


def journal_api():
    from memvane.journal import api
    return api


class ReaderLease(InstallCase):
    def lifetime(self, *, exclusive, install_id=None):
        return journal_api().reader_lifetime(install_id or self.install.install_id, exclusive=exclusive)

    def busy(self, *, exclusive):
        with self.assertRaises(ValueError) as caught:
            with self.lifetime(exclusive=exclusive):
                pass
        self.assertEqual(str(caught.exception), 'reader_lifetime_busy')

    def test_install_prepares_a_private_lock_that_reads_open_without_writing(self):
        lock = self.lock_path()
        info, parent = lock.lstat(), lock.parent.lstat()
        self.assertEqual((oct(info.st_mode & 0o777), info.st_nlink, oct(parent.st_mode & 0o777)),
                         ('0o600', 1, '0o700'))
        before, listing = file_state(lock), sorted(os.listdir(lock.parent))
        with self.lifetime(exclusive=False), self.endpoint() as endpoint:
            journal_api().bind_schema3(endpoint)
        self.assertEqual((file_state(lock), sorted(os.listdir(lock.parent))), (before, listing))

    def test_a_shared_reader_defers_exclusive_until_it_ends(self):
        with self.lifetime(exclusive=False):
            self.busy(exclusive=True)
            with self.lifetime(exclusive=False):
                pass
        with self.lifetime(exclusive=True):
            self.busy(exclusive=False)
            self.refused('reader_lifetime_busy', self.endpoint_id)
            with self.assertRaises(ReaderWindowBusy):
                _Lifetime(self.install.install_id)
        with self.lifetime(exclusive=True):
            pass

    def test_a_resolved_endpoint_holds_the_lifetime_until_it_closes(self):
        with self.endpoint():
            self.busy(exclusive=True)
        with self.lifetime(exclusive=True):
            pass

    def test_missing_lock_or_unknown_install_never_creates_a_lock(self):
        journal = journal_api()
        locks = self.lock_path().parent.parent
        before = sorted(os.listdir(locks))
        with self.assertRaises(Exception):
            with self.lifetime(exclusive=False, install_id='a' * 32):
                pass
        self.refused('reader_install_invalid', self.lifetime(exclusive=False, install_id='A1').__enter__)
        self.assertEqual(sorted(os.listdir(locks)), before)
        lock = self.lock_path()
        lock.unlink()
        with self.assertRaises(FileNotFoundError):
            self.endpoint_id()
        self.assertFalse(lock.exists())
        journal.prepare_reader_lifetime(self.install.install_id)
        self.assertTrue(self.endpoint_id())

    def test_unsafe_lock_files_are_refused(self):
        lock = self.lock_path()
        alias = lock.with_name('alias.lock')
        os.link(lock, alias)
        self.refused('reader_lifetime_unsafe', self.endpoint_id)
        alias.unlink()
        lock.chmod(0o644)
        self.refused('reader_lifetime_unsafe', self.endpoint_id)
        lock.chmod(0o600)
        target = lock.with_name('target.lock')
        os.replace(lock, target)
        os.symlink(target, lock)
        with self.assertRaises(OSError):
            self.endpoint_id()
        lock.unlink()
        os.mkfifo(lock, 0o600)
        self.refused('reader_lifetime_unsafe', self.endpoint_id)
        lock.unlink()
        os.replace(target, lock)
        self.assertTrue(self.endpoint_id())

    def test_a_lock_replaced_after_flock_is_refused(self):
        journal = journal_api()
        lock = self.lock_path()

        def fresh():
            path = lock.with_name('fresh.lock')
            path.write_bytes(b'')
            path.chmod(0o600)
            return path
        # Moved aside: the held inode is still private, the name is another file.
        with self.endpoint() as endpoint:
            os.rename(lock, lock.with_name('aside.lock'))
            os.rename(fresh(), lock)
            self.refused('reader_lifetime_changed', journal.bind_schema3, endpoint)
            self.refused('reader_lifetime_changed', journal.recheck_reader, endpoint)
        lock.with_name('aside.lock').unlink()
        self.assertTrue(self.endpoint_id())
        # Replaced: the held inode is unlinked.
        with self.endpoint() as endpoint:
            os.replace(fresh(), lock)
            self.refused('reader_lifetime_unsafe', journal.bind_schema3, endpoint)
        self.assertTrue(self.endpoint_id())

    def test_an_endpoint_binds_only_inside_its_own_lifetime_context(self):
        journal = journal_api()
        errors = []
        with self.endpoint() as endpoint:
            def other_context():
                try:
                    journal.bind_schema3(endpoint)
                except ValueError as exc:
                    errors.append(str(exc))
            thread = threading.Thread(target=other_context)
            thread.start()
            thread.join()
            journal.bind_schema3(endpoint)
        self.assertEqual(errors, ['reader_lifetime_invalid'])

    def test_a_forward_epoch_keeps_the_endpoint_and_its_creation_epoch(self):
        from memvane.kernel.memvane_version import plugin_version, version_key
        journal = journal_api()
        record, endpoint = self.record(), self.endpoint_id()
        self.forge("UPDATE selection SET max_plugin_version='0.9.0' WHERE id=1")
        epoch = self.row()['epoch']
        with self.owner() as owner:
            journal.publish_plugin_version(self.decision, owner, version_key(plugin_version()))
        self.assertEqual(self.row()['epoch'], epoch + 1)
        self.assertEqual((self.record()['creation_epoch'], self.endpoint_id()),
                         (record['creation_epoch'], endpoint))

    def test_a_held_endpoint_is_refused_after_a_reseal_and_a_new_read_gets_the_new_one(self):
        journal = journal_api()
        results = []

        def owner_reseals():
            # The sync owner's own thread: no reader context of this thread.
            try:
                decision = journal.select_route(str(self.project), action='recall')
                with self.owner() as owner:
                    results.append(journal.resettle_selection(decision, owner)[0])
            except Exception as exc:  # reported through results
                results.append(repr(exc))
        with self.endpoint() as held:
            self.fixture.write_settings(skip_paths=['scratch/'])
            decision = journal.select_route(str(self.project), action='recall')
            before = self.row()
            with self.owner() as owner:
                # A reader's resolve context never writes the journal.
                self.refused('reader_journal_read_only', journal.resettle_selection, decision, owner)
            self.assertEqual(self.row(), before)
            thread = threading.Thread(target=owner_reseals)
            thread.start()
            thread.join()
            self.assertEqual(results, ['resealed'])
            self.refused('reader_endpoint_changed', journal.recheck_reader, held)
        self.assertNotEqual(self.endpoint_id(), held.endpoint_id)

    def test_a_selection_change_during_resolution_issues_nothing(self):
        journal = journal_api()
        issued = []
        real = journal_reader._issue_v1_reader

        def issue_then_concurrent_cas(*args):
            endpoint = real(*args)
            issued.append(endpoint)
            self.forge('UPDATE selection SET epoch=epoch+1 WHERE id=1')
            return endpoint
        with mock.patch('memvane.journal.journal_reader._issue_v1_reader', issue_then_concurrent_cas):
            self.refused('reader_selection_changed', self.endpoint_id)
        self.assertEqual(len(issued), 1)
        self.assertFalse(journal.persisted_schema3(issued[0]))
        self.refused('reader_endpoint_not_issued', journal.recheck_reader, issued[0])
        with ExitStack() as stack:
            endpoint = stack.enter_context(self.endpoint())
            self.assertEqual(journal.reader_selection(endpoint).epoch, self.row()['epoch'])


if __name__ == '__main__':
    unittest.main()
