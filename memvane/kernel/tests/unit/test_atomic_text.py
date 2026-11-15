from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.kernel import atomic_rename


class AtomicTextTests(FixtureCase):
    def test_failed_replace_preserves_original_and_removes_temp(self):
        path = self.tmp / 'card.md'
        original = '---\nstatus: generated\n---\nbody\n'
        path.write_text(original, encoding='utf-8')
        with patch.object(atomic_rename.os, 'replace', side_effect=OSError('synthetic failure')):
            self.assertFalse(atomic_rename.write_text_atomic(path, original.replace('generated', 'verified')))
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        self.assertEqual(list(self.tmp.glob('.*.tmp-*')), [])

    def test_successful_replace_preserves_private_mode(self):
        path = self.tmp / 'card.md'
        path.write_text('old', encoding='utf-8')
        path.chmod(0o600)
        self.assertTrue(atomic_rename.write_text_atomic(path, 'new'))
        self.assertEqual(path.read_text(encoding='utf-8'), 'new')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
