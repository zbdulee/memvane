"""Pin receipts only reuse a verified, unchanged private store generation."""
import os
import json
import subprocess
import sys
from unittest import mock

from memvane.store.tests.support import VectorCase
from memvane.store.tests.support import drop_triggers_and


class PinReceipt(VectorCase):
    def test_independent_process_reuses_unchanged_generation(self):
        store, repository, handle, _manifest, _recipe, _profile, _gen, pin = self.ready()
        store.release(pin)
        store.close()
        reader = self.Store.open_reader(self.db, repository=repository, scope_id=self.scope)
        original = reader.capture_view(handle.worktree_id)
        pin = reader.pin_view(original)
        reader.release(pin)
        reader.close()
        script = r'''
import json, sys
from memvane.store.view.view_store import ViewStore
path, repository, scope, wid = sys.argv[1:]
reader = ViewStore.open_reader(path, repository=json.loads(repository), scope_id=scope)
def full(*args, **kwargs): raise AssertionError('full revalidation unexpectedly ran')
reader._rows_verify = full
handle = reader.capture_view(wid)
pin = reader.pin_view(handle)
reader.release(pin)
reader.close()
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.db),
                                 json.dumps(repository), self.scope, handle.worktree_id],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutated_card_forces_full_verification_and_refusal(self):
        store, repository, handle, _manifest, _recipe, _profile, _gen, pin = self.ready()
        store.release(pin)
        store.close()
        reader = self.Store.open_reader(self.db, repository=repository, scope_id=self.scope)
        initial = reader.capture_view(handle.worktree_id)
        pin = reader.pin_view(initial)
        reader.release(pin)
        reader.close()
        drop_triggers_and(self.db, 's_cards', ('UPDATE s_cards SET title=?', ('corrupt',)))
        reader = self.Store.open_reader(self.db, repository=repository, scope_id=self.scope)
        self.addCleanup(reader.close)
        current = reader.capture_view(handle.worktree_id)
        with self.assertRaises(self.Error):
            reader.pin_view(current)

    def test_reopen_reuses_only_matching_private_receipt(self):
        store, repository, handle, _manifest, _recipe, _profile, _gen, pin = self.ready()
        store.release(pin)
        store.close()
        def opened():
            reader = self.Store.open_reader(self.db, repository=repository, scope_id=self.scope)
            self.addCleanup(reader.close)
            return reader, reader.capture_view(handle.worktree_id)
        reader, first = opened()
        with mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(first)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 1)
        reader.close()
        receipt = self.db.parent / '.view-pin-receipt.json'
        self.assertTrue(receipt.is_file())
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        reader, second = opened()
        with mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(second)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 0)
        reader.close()
        receipt.write_text('invalid', encoding='utf8')
        reader, third = opened()
        with mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(third)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 1)
        reader.close()
        info = self.db.stat()
        os.utime(self.db, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        reader, fourth = opened()
        with mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(fourth)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 1)
        reader.close()
        receipt.unlink()
        receipt.symlink_to(self.db)
        reader, fifth = opened()
        with mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(fifth)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 1)
        reader.close()
        self.assertTrue(receipt.is_symlink())
        receipt.unlink()
        reader, sixth = opened()
        pinned = reader.pin_view(sixth)
        reader.release(pinned)
        reader.close()
        reader, seventh = opened()
        with mock.patch('memvane.store.view.pin_receipt.plugin_version', return_value='9.9.9'), \
             mock.patch.object(reader, '_rows_verify', wraps=reader._rows_verify) as verify:
            pinned = reader.pin_view(seventh)
            reader.release(pinned)
            self.assertEqual(verify.call_count, 1)
