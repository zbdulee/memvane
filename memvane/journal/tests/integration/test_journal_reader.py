"""Reader endpoints of a selected install: issue, expiry, the epoch floor and the one re-sealer.

The reader record is sealed by the install (initial build) and re-sealed only
by the sync owner's ``resettle_selection``. Each recall resolves the view head
and refuses one behind the record's ``head_floor`` on the same view.
"""
import dataclasses
import time
import unittest
from unittest import mock

from memvane.journal import journal_reader
from memvane.journal.tests.support import InstallCase, file_state, thaw
from memvane.journal.types import VerifiedEndpoint


def journal_api():
    from memvane.journal import api
    return api


class SelectedReader(InstallCase):
    def test_selected_endpoint_pins_the_published_head_and_vector_spec(self):
        journal = journal_api()
        _path, selected_id, epoch, sealed = journal.selected_journal_reader(self.install.journal)
        with self.endpoint() as endpoint:
            record = thaw(endpoint.record)
            self.assertEqual((endpoint.endpoint_id, record), (selected_id, sealed))
            self.assertTrue(journal.persisted_schema3(endpoint))
            selection = journal.reader_selection(endpoint)
            self.assertEqual((selection.epoch, selection.preferred_endpoint, selection.previous_endpoint,
                              selection.transition_id),
                             (epoch, endpoint.endpoint_id, None, self.install.install_id))
            saved, _raw, proof = journal.recheck_reader(endpoint)
            self.assertEqual((saved, proof['selectionEpoch']), (sealed, epoch))
            view_id, head_epoch, spec = self.pin(record)
        floor = record['head_floor']
        self.assertEqual(view_id, floor['view_id'])
        self.assertGreaterEqual(head_epoch, floor['epoch'])
        self.assertEqual(spec, (record['vector_profile'], record['vector_recipe']))
        self.assertEqual((record['store_format'], record['capability'], record['db_path']),
                         ('schema3', journal_reader.SCHEMA3_CAPABILITY, str(self.install.store_path)))

    def test_reading_writes_no_journal_store_or_lock_bytes(self):
        journal = journal_api()
        files = [self.install.journal, self.install.store_path, self.lock_path()]
        before = [file_state(path) for path in files]
        listing = sorted(p.name for p in self.state.iterdir())
        with self.endpoint() as endpoint:
            journal.recheck_reader(endpoint)
            journal.bind_schema3(endpoint)
            self.pin(thaw(endpoint.record))
            self.head(thaw(endpoint.record))
        self.assertEqual([file_state(path) for path in files], before)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), listing)

    def test_endpoint_and_its_record_are_immutable(self):
        with self.endpoint() as endpoint:
            with self.assertRaises(TypeError):
                endpoint.record['head_floor'] = None
            with self.assertRaises(dataclasses.FrozenInstanceError):
                endpoint.endpoint_id = '0' * 64
            self.assertEqual(journal_api().recheck_reader(endpoint)[0], self.record())

    def test_caller_created_endpoint_or_installer_seal_is_refused(self):
        journal = journal_api()
        self.refused('reader_endpoint_not_issued', VerifiedEndpoint)
        self.refused('reader_installer_not_issued', journal_reader.InstallerSeal)
        with self.endpoint() as issued:
            forged = object.__new__(VerifiedEndpoint)
            object.__setattr__(forged, 'endpoint_id', issued.endpoint_id)
            object.__setattr__(forged, 'record', issued.record)
            self.refused('reader_endpoint_not_issued', journal.recheck_reader, forged)
            self.refused('reader_endpoint_not_issued', journal.bind_schema3, forged)
            self.assertFalse(journal.persisted_schema3(forged))
            self.assertIsNone(journal.reader_selection(forged))

    def test_endpoint_expires_with_its_reader_lifetime(self):
        journal = journal_api()
        with self.endpoint() as endpoint:
            journal.bind_schema3(endpoint)
        self.refused('reader_endpoint_not_issued', journal.recheck_reader, endpoint)
        self.refused('reader_endpoint_not_issued', journal.bind_schema3, endpoint)
        self.assertFalse(journal.persisted_schema3(endpoint))
        self.assertIsNone(journal.reader_selection(endpoint))

    def test_new_write_mark_keeps_the_reader_record(self):
        before, epoch, endpoint = self.record(), self.row()['epoch'], self.endpoint_id()
        with self.writer() as writer:
            proof = writer.mark_new_write()
        row = self.row()
        self.assertEqual((row['epoch'], row['newWriteSeen'], proof['selectionEpoch']),
                         (epoch + 1, True, epoch + 1))
        self.assertEqual((row['reader'], self.endpoint_id()), (before, endpoint))


class EpochFloor(InstallCase):
    """The head a reader pins never goes below ``head_floor`` on the floor's view."""

    def floor(self, record, epoch):
        return dict(record, head_floor=dict(record['head_floor'], epoch=epoch))

    def test_head_below_the_floor_is_refused(self):
        record = self.record()
        view_id, epoch = self.head(record)
        self.assertEqual(view_id, record['head_floor']['view_id'])
        ahead = self.floor(record, epoch + 1)
        self.refused('reader_head_regressed', self.head, ahead)
        self.refused('reader_head_regressed', self.pin, ahead)
        equal = self.floor(record, epoch)
        self.assertEqual(self.head(equal), (view_id, epoch))
        self.assertEqual(self.pin(equal)[:2], (view_id, epoch))

    def test_floor_of_another_view_does_not_bound_this_view(self):
        record = self.record()
        view_id, epoch = self.head(record)
        other = dict(record, head_floor=dict(record['head_floor'], view_id='f' * 64, epoch=epoch + 9))
        self.assertEqual(self.head(other), (view_id, epoch))
        self.assertEqual(self.pin(other)[:2], (view_id, epoch))

    def test_forged_floor_issues_an_endpoint_whose_pin_and_recall_refuse(self):
        record = self.record()
        _view, epoch = self.head(record)
        self.forge_record(self.floor(record, epoch + 1))
        with self.endpoint() as endpoint:
            self.refused('reader_head_regressed', self.pin, thaw(endpoint.record))
            self.refused('reader_head_regressed', self.head, thaw(endpoint.record))
        self.refused('reader_head_regressed', self.install.recall, 'emerald heron')

    def test_published_heads_move_above_a_fixed_floor(self):
        record = self.record()
        _view, epoch = self.head(record)
        self.fixture.write_card('egret.md', title='Snowy egret', body='The snowy egret wades at noon.\n')
        self.fixture.commit('egret')
        self.install.settle()
        after = self.record()
        self.assertEqual(after['head_floor'], record['head_floor'])
        view_id, later = self.head(after)
        self.assertEqual(view_id, record['head_floor']['view_id'])
        self.assertGreater(later, epoch)


class OneResealer(InstallCase):
    """Only the sync owner's ``resettle_selection`` re-seals the selected reader."""

    def change_settings(self):
        self.fixture.write_settings(skip_paths=['scratch/'])
        decision = journal_api().select_route(str(self.project), action='recall')
        self.assertEqual(decision['state'], 'candidate', decision)
        return decision

    def test_settings_change_refuses_readers_until_the_owner_reseals(self):
        journal = journal_api()
        before, row, endpoint = self.record(), self.row(), self.endpoint_id()
        decision = self.change_settings()
        self.assertTrue(journal.settings_resettle_pending(self.state, self.project, None))
        self.refused('reader_endpoint_changed', self.endpoint_id)
        with self.owner() as owner:
            status, proof = journal.resettle_selection(decision, owner)
            self.assertEqual(status, 'resealed')
            self.assertEqual(journal.resettle_selection(decision, owner)[0], 'unchanged')
        after = self.record()
        self.assertEqual({key for key in after if after[key] != before[key]}, {'settings'})
        self.assertEqual((self.row()['epoch'], proof['selectionEpoch']), (row['epoch'] + 1, row['epoch'] + 1))
        self.assertFalse(journal.settings_resettle_pending(self.state, self.project, None))
        self.assertNotEqual(self.endpoint_id(), endpoint)

    def test_resettle_without_the_live_owner_changes_nothing(self):
        journal = journal_api()
        decision = self.change_settings()
        before = self.row()
        with self.owner() as expired:
            pass
        for owner in (object(), expired):
            with self.subTest(owner=type(owner).__name__):
                with self.assertRaises(Exception):
                    journal.resettle_selection(decision, owner)
                self.assertEqual(self.row(), before)

    def test_resettle_stopped_before_commit_keeps_the_old_record(self):
        journal = journal_api()
        decision = self.change_settings()
        before = self.row()
        from memvane.runtime import api as runtime
        real, calls = runtime.validate_sync_owner, []

        def third_check_fails(claim, queue_path):
            # resettle_selection checks the owner on entry, before the journal
            # transaction and inside it after both updates: stop at the last.
            calls.append(queue_path)
            if len(calls) == 3:
                raise RuntimeError('owner_lost_inside_transaction')
            return real(claim, queue_path)
        with self.owner() as owner, mock.patch('memvane.runtime.api.validate_sync_owner', third_check_fails):
            self.refused('owner_lost_inside_transaction', journal.resettle_selection, decision, owner)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.row(), before)
        self.refused('reader_endpoint_changed', self.endpoint_id)
        with self.owner() as owner:
            self.assertEqual(journal.resettle_selection(decision, owner)[0], 'resealed')


class UnselectedInstall(InstallCase):
    """A finalized install holds a sealed reader record but issues no endpoint."""

    UPTO = 'finalized'

    def test_unselected_install_issues_no_endpoint(self):
        journal = journal_api()
        row = self.row()
        self.assertEqual(row['state'], 'prepared')
        self.assertTrue(journal_reader._schema3_shape(row['reader']))
        self.assertNotEqual(journal.select_route(str(self.project), action='recall').get('state'), 'candidate')
        self.refused('reader_selection_unproved', self.endpoint_id)
        self.refused('reader_selection_invalid', journal.selected_journal_reader, self.install.journal)
        self.assertEqual(self.row(), row)


if __name__ == '__main__':
    unittest.main()
