"""Persisted identities survive an APFS device renumbering (design 8.4).

A reboot may give every file another ``st_dev``. Records sealed in another
boot compare their ``*_node`` fields by inode; observations within one process
(open fd against path, before against after) keep the device.
"""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.journal import journal_reader
from memvane.journal.tests.support import InstallCase, thaw
from memvane.kernel.persistent_identity import (persistent_identity_equal, persistent_node_equal,
                                                persistent_provider_equal)


class RebootStat:
    """A stat result whose device is one higher, as after a reboot."""

    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    @property
    def st_dev(self):
        return self.original.st_dev + 1

    def __getitem__(self, key):
        values = list(self.original)
        values[2] += 1
        return values[key]

    def __iter__(self):
        return iter(self[:])


class RebootEntry:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __fspath__(self):
        return self.original.path

    def stat(self, *args, **kwargs):
        return RebootStat(self.original.stat(*args, **kwargs))


class RebootScandir:
    def __init__(self, original):
        self.original = original

    def __iter__(self):
        return self

    def __next__(self):
        return RebootEntry(next(self.original))

    def __enter__(self):
        self.original.__enter__()
        return self

    def __exit__(self, *args):
        return self.original.__exit__(*args)

    def close(self):
        self.original.close()


@contextmanager
def simulated_reboot():
    """Every stat of this process reports another device; inodes and times stay."""
    def wrap(original):
        def stat(*args, **kwargs):
            result = original(*args, **kwargs)
            return result if isinstance(result, RebootStat) else RebootStat(result)
        return stat
    with ExitStack() as stack:
        for target, names in ((os, ('stat', 'lstat', 'fstat')), (Path, ('stat', 'lstat'))):
            for name in names:
                stack.enter_context(mock.patch.object(target, name, wrap(getattr(target, name))))
        for name in ('supports_dir_fd', 'supports_follow_symlinks', 'supports_fd'):
            stack.enter_context(mock.patch.object(os, name, getattr(os, name) | {os.stat}))
        scandir = os.scandir
        stack.enter_context(mock.patch.object(
            os, 'scandir', side_effect=lambda *a, **kw: RebootScandir(scandir(*a, **kw))))
        yield


def journal_api():
    from memvane.journal import api
    return api


class RecordNodes(unittest.TestCase):
    def test_only_the_device_is_ignored_and_inputs_are_unchanged(self):
        old = {'path': '/fixture', 'dev': 4, 'ino': 8, 'size': 3,
               'mtime': 5, 'ctime': 6, 'mode': 7, 'nlink': 1, 'sha': 'abc'}
        live = {**old, 'dev': 9}
        self.assertTrue(persistent_identity_equal(old, live))
        for key in old.keys() - {'dev'}:
            with self.subTest(field=key):
                self.assertFalse(persistent_identity_equal(old, {**live, key: None}))
        self.assertFalse(persistent_identity_equal(old, {k: v for k, v in live.items() if k != 'dev'}))
        self.assertEqual((old['dev'], live['dev']), (4, 9))
        self.assertTrue(persistent_node_equal([4, 8], [9, 8]))
        self.assertFalse(persistent_node_equal([4, 8], [9, 7]))
        self.assertFalse(persistent_node_equal([4, 8], [9, 8, 3]))
        rows = [['/fixture', 4, 8, 3, 5, 6]]
        self.assertTrue(persistent_provider_equal(rows, [['/fixture', 9, 8, 3, 5, 6]]))
        for index in (0, 2, 3, 4, 5):
            changed = list(rows[0])
            changed[index] = None
            with self.subTest(column=index):
                self.assertFalse(persistent_provider_equal(rows, [changed]))
        self.assertFalse(persistent_provider_equal(rows, []))

    def test_reader_records_compare_nodes_by_inode_only(self):
        base = {'project_node': [4, 8], 'state_node': [4, 9], 'db_node': [4, 10], 'scope': 's'}
        keys = tuple(base)
        renumbered = {**base, 'project_node': [5, 8], 'state_node': [5, 9], 'db_node': [5, 10]}
        self.assertTrue(journal_reader.records_agree(base, renumbered, keys))
        for key in ('project_node', 'state_node', 'db_node'):
            with self.subTest(field=key):
                self.assertFalse(journal_reader.records_agree(base, {**base, key: [4, 7]}, keys))
        self.assertFalse(journal_reader.records_agree(base, {**base, 'scope': 't'}, keys))

    def test_a_reseal_keeps_the_committed_devices_of_unchanged_nodes(self):
        stored = {'project_node': [4, 8], 'state_node': [4, 9], 'db_node': [4, 10], 'settings': 'a'}
        current = {'project_node': [5, 8], 'state_node': [5, 9], 'db_node': [5, 11], 'settings': 'b'}
        kept = journal_reader._preserve_node_devices(stored, dict(current))
        self.assertEqual(kept, {'project_node': [4, 8], 'state_node': [4, 9], 'db_node': [5, 11],
                                'settings': 'b'})


class AfterReboot(InstallCase):
    def renumbered(self, record, delta=7):
        return dict(record, **{key: [record[key][0] + delta, record[key][1]]
                               for key in ('project_node', 'state_node', 'db_node')})

    def test_a_record_sealed_in_another_boot_still_serves(self):
        record, endpoint = self.record(), self.endpoint_id()
        self.forge_record(self.renumbered(record))
        with self.endpoint() as issued:
            self.assertNotEqual(issued.endpoint_id, endpoint)
            journal_api().recheck_reader(issued)
            self.assertEqual(self.pin(thaw(issued.record))[0], record['head_floor']['view_id'])
        moved = dict(record, db_node=[record['db_node'][0], record['db_node'][1] + 1])
        self.forge_record(moved)
        self.refused('journal_reader_invalid', self.endpoint_id)

    def test_selected_proofs_and_reads_survive_a_reboot(self):
        journal = journal_api()
        record, endpoint = self.record(), self.endpoint_id()
        with simulated_reboot():
            self.assertTrue(journal.verify_selected_route(self.decision)['productRouteAllowed'])
            self.assertTrue(journal.verify_selected_route_for_write(self.decision)['providerAvailable'])
            notification = journal.select_notification_route(self.project)
            self.assertTrue(journal.verify_notification_target(notification)['notificationOnly'])
            with self.endpoint() as issued:
                self.assertEqual(issued.endpoint_id, endpoint)
                journal.recheck_reader(issued)
                self.assertEqual(self.pin(thaw(issued.record))[2],
                                 (record['vector_profile'], record['vector_recipe']))
            self.assertIn('emerald heron', self.install.recall('emerald heron').lower())
        self.assertEqual(self.record(), record)

    def test_writer_admission_and_publication_survive_a_reboot(self):
        record, endpoint = self.record(), self.endpoint_id()
        self.fixture.write_card('egret.md', title='Snowy egret', body='The snowy egret wades at noon.\n')
        self.fixture.commit('egret')
        with simulated_reboot():
            with self.writer() as writer:
                self.assertEqual(writer.store()['path'], record['db_path'])
                writer.validate()
            self.install.settle()
            self.assertEqual(self.endpoint_id(), endpoint)
        self.assertEqual(self.record(), record)
        self.assertGreater(self.head()[1], record['head_floor']['epoch'])

    def test_live_observations_inside_one_process_keep_the_device(self):
        journal = journal_api()
        with self.writer() as writer:
            with simulated_reboot():
                # The sync owner's lock is checked first, against its own earlier observation.
                self.refused('sync_owner_lock_unavailable', writer.validate)
            writer.validate()
        with self.endpoint() as issued:
            with simulated_reboot():
                self.refused('reader_lifetime_changed', journal.bind_schema3, issued)
            journal.bind_schema3(issued)


if __name__ == '__main__':
    unittest.main()
