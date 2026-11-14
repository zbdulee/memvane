import unittest
from unittest.mock import patch
from memvane.kernel import errors


class SQLiteReadUriTests(unittest.TestCase):
    def test_non_apple_sqlite_uses_read_only_uri(self):
        with patch.object(errors.sys, 'platform', 'linux'):
            uri = errors._query_only_uri('/tmp/fixture store.sqlite')
        self.assertTrue(uri.startswith('file:///'))
        self.assertTrue(uri.endswith('?mode=ro'))
        self.assertIn('%20', uri)

    def test_apple_python_39_uses_rw_uri_for_query_only_reads(self):
        with patch.object(errors.sys, 'platform', 'darwin'), \
                patch.object(errors.sys, 'version_info', (3, 9, 0)):
            uri = errors._query_only_uri('/tmp/fixture.sqlite')
        self.assertTrue(uri.endswith('?mode=rw'))

    def test_apple_linked_sqlite_uses_rw_on_newer_python(self):
        with patch.object(errors.sys, 'platform', 'darwin'), \
                patch.object(errors.sys, 'version_info', (3, 13, 0)), \
                patch.object(errors, '_apple_sqlite', return_value=True):
            uri = errors._query_only_uri('/tmp/fixture.sqlite')
        self.assertTrue(uri.endswith('?mode=rw'))
