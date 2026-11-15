"""The persisted schema3 reader record: exact shape, identity fields and independent views.

Recall trusts a reader record only when every install and store identity
field still matches; a linked worktree pins its own view in the same store.
"""
import unittest

from memvane.journal import journal_reader
from memvane.journal.tests.support import InstallCase, thaw


def journal_api():
    from memvane.journal import api
    return api


def valid_record():
    return dict(
        scope='5' * 32, install_id='4' * 32, project='/fixture/project', project_node=[1, 2],
        state='/fixture/state', state_node=[1, 3], attestation_sha256='a' * 64, profile='b' * 64,
        settings='c' * 64, roots=[], store_format='schema3',
        capability=journal_reader.SCHEMA3_CAPABILITY, checkout_policy=journal_reader.SCHEMA3_POLICY,
        incarnation='d' * 32, db_path='/fixture/state/store.sqlite', db_node=[1, 4],
        repository_digest='e' * 64, worktree_id='f' * 32, worktree_binding='1' * 64,
        settings_policy_digest='2' * 64, vector_profile='3' * 64, vector_recipe='6' * 64,
        head_floor={'view_id': '7' * 64, 'epoch': 2, 'ordinal': 2}, creation_epoch=2)


class RecordShape(unittest.TestCase):
    def test_only_the_exact_persisted_shape_is_a_reader_record(self):
        base = valid_record()
        self.assertTrue(journal_reader._schema3_shape(base))
        self.assertTrue(journal_reader._schema3_shape(dict(base, vector_profile=None, vector_recipe=None,
                                                           head_floor=None)))
        missing = dict(base)
        del missing['head_floor']
        cases = {
            'extra key': dict(base, bridge='x'),
            'missing key': missing,
            'capability': dict(base, capability='v6-rw'),
            'checkout policy': dict(base, checkout_policy='any'),
            'digest case': dict(base, repository_digest='E' * 64),
            'short incarnation': dict(base, incarnation='d' * 31),
            'relative store': dict(base, db_path='store.sqlite'),
            'store outside state': dict(base, db_path='/elsewhere/store.sqlite'),
            'node arity': dict(base, db_node=[1, 4, 5]),
            'node type': dict(base, db_node=['1', 4]),
            'half vector spec': dict(base, vector_recipe=None),
            'floor keys': dict(base, head_floor={'view_id': '7' * 64, 'epoch': 2}),
            'floor epoch': dict(base, head_floor={'view_id': '7' * 64, 'epoch': 0, 'ordinal': 2}),
            'floor ordinal type': dict(base, head_floor={'view_id': '7' * 64, 'epoch': 2, 'ordinal': '2'}),
            'creation epoch': dict(base, creation_epoch=0),
            'creation epoch type': dict(base, creation_epoch=True),
        }
        for label, record in cases.items():
            with self.subTest(case=label):
                self.assertFalse(journal_reader._schema3_shape(record))

    def test_a_resettled_record_changes_only_settings_or_the_vector_spec(self):
        old = valid_record()
        accepted = {
            'settings': dict(old, settings='9' * 64),
            'vector spec': dict(old, vector_profile='8' * 64, vector_recipe='0' * 64),
        }
        refused = {
            'unchanged': dict(old),
            'vector spec dropped': dict(old, vector_profile=None, vector_recipe=None),
            'store': dict(old, db_path='/fixture/state/other.sqlite'),
            'floor': dict(old, head_floor={'view_id': '7' * 64, 'epoch': 3, 'ordinal': 3}),
            'attestation': dict(old, settings='9' * 64, attestation_sha256='0' * 64),
            'format': dict(old, store_format='v6', settings='9' * 64),
        }
        for label, new in accepted.items():
            with self.subTest(accepted=label):
                self.assertTrue(journal_reader.resettled_pair(old, new))
        for label, new in refused.items():
            with self.subTest(refused=label):
                self.assertFalse(journal_reader.resettled_pair(old, new))


def forged(value):
    """One changed value of the same JSON type."""
    if isinstance(value, str):
        return value[:-1] + ('0' if value[-1] != '0' else '1')
    if isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value):
        return [value[0], value[1] + 1]
    if isinstance(value, list):
        return [dict(item, name=item['name'] + '-x') for item in value]
    raise TypeError(value)


class PersistedEndpoint(InstallCase):
    def test_persisted_endpoint_serves_recall_from_the_store(self):
        self.assertIn('emerald heron', self.install.recall('emerald heron').lower())

    def test_one_identity_field_changed_refuses_the_endpoint(self):
        record, original = self.record(), self.endpoint_id()
        expected = {
            'journal_reader_invalid': ('install_id', 'scope', 'project', 'project_node', 'state',
                                       'db_path', 'db_node', 'incarnation', 'capability',
                                       'checkout_policy'),
            'reader_endpoint_changed': ('attestation_sha256', 'profile', 'settings', 'roots',
                                        'state_node'),
            'reader_store_changed': ('repository_digest', 'worktree_id', 'worktree_binding',
                                     'settings_policy_digest'),
        }
        for reason, keys in expected.items():
            for key in keys:
                with self.subTest(field=key):
                    self.forge_record(dict(record, **{key: forged(record[key])}))
                    self.refused(reason, self.endpoint_id)
        self.forge_record(dict(record, creation_epoch=self.row()['epoch'] + 1))
        self.refused('reader_endpoint_changed', self.endpoint_id)
        self.forge_record(record)
        self.assertEqual(self.endpoint_id(), original)

    def test_pin_time_fields_issue_an_endpoint_that_refuses_its_reads(self):
        journal = journal_api()
        record = self.record()
        self.forge_record(dict(record, vector_profile=forged(record['vector_profile'])))
        with self.endpoint() as endpoint:
            self.refused('reader_endpoint_changed', self.pin, thaw(endpoint.record))
        self.forge_record(dict(record, store_format='v6'))
        with self.endpoint() as endpoint:
            self.assertFalse(journal.persisted_schema3(endpoint))

    def test_a_record_without_a_vector_spec_reads_the_head_lexically(self):
        record = self.record()
        unbound = dict(record, vector_profile=None, vector_recipe=None)
        view_id, epoch, spec = self.pin(unbound)
        self.assertEqual((view_id, spec), (record['head_floor']['view_id'],
                                           (record['vector_profile'], record['vector_recipe'])))
        self.assertEqual(self.head(unbound), (view_id, epoch))


class LinkedView(InstallCase):
    """A linked worktree reads its own view of the main checkout's store."""

    def setUp(self):
        super().setUp()
        self.linked = self.fixture.linked_worktree('wt', 'feature')
        self.install.settle()
        self.linked_decision = journal_api().select_route(str(self.linked.root), action='recall')
        self.assertEqual(self.linked_decision['state'], 'worktree', self.linked_decision)

    def test_linked_worktree_pins_its_own_view(self):
        record = self.record()
        main = self.head(record)
        linked = self.head(record, linked=self.linked_decision)
        self.assertNotEqual(linked[0], main[0])
        self.assertNotEqual(linked[0], record['head_floor']['view_id'])
        self.assertEqual(self.pin(record, linked=self.linked_decision)[0], linked[0])
        self.assertEqual(self.head(record), main)

    def test_linked_decision_naming_the_main_checkout_is_refused(self):
        record = self.record()
        main_root = dict(self.linked_decision, worktreeProjectRoot=record['project'])
        self.refused('reader_checkout_changed', self.head, record, linked=main_root)
        self.refused('reader_checkout_changed', self.head, record, linked={})

    def test_unpublished_linked_worktree_is_preparing(self):
        from memvane.store.types import BlobStoreError
        other = self.fixture.linked_worktree('wt2', 'second')
        decision = journal_api().select_route(str(other.root), action='recall')
        with self.assertRaises(BlobStoreError) as caught:
            self.head(self.record(), linked=decision)
        self.assertEqual(str(caught.exception), 'view_preparing')


if __name__ == '__main__':
    unittest.main()
