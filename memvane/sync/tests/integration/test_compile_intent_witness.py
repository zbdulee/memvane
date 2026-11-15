"""Compile intent witnesses and swaps: a consumed name is not evidence, a swapped file is not acknowledged.

Real intent files and renames in a private state root with the fake store of
``test_compile_intents``. A ``.consumed.json`` file whose evidence the store
does not hold supersedes nothing; an intent replaced while its attestation is
written is left for a later pass, never renamed.
"""
import os
import unittest
from unittest import mock

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.sync import compile_intents as ci
from memvane.sync.tests.integration import test_compile_intents as rules


class Witness(rules.CompileIntentRules):
    def test_a_consumed_name_without_stored_evidence_supersedes_nothing(self):
        witness = self.intent('8' * 64, observed=50, published=False)
        self.write(witness, '.consumed.json')
        orphan = self.intent('9' * 64, observed=50, published=False)
        orphan['targetPath'] = witness['targetPath']
        waiting = self.write(orphan)
        with mock.patch.object(ci, '_stored', lambda store, handle, intent: False):
            result = self.consume(rules.FakeStore())
        self.assertEqual((result['superseded'], result['waiting']), (0, 1))
        self.assertTrue(waiting.exists())

    def test_an_intent_swapped_during_its_write_is_never_acknowledged(self):
        intent = self.intent('a' * 64)
        path = self.write(intent)
        swapped = canonical_json_bytes(dict(intent, verifyEngine='other'))

        def swap():
            temporary = path.with_name('.swap')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, swapped)
            os.close(fd)
            os.replace(temporary, path)
        self.consume(rules.FakeStore(during=swap))
        self.assertEqual(path.read_bytes(), swapped)
        self.assertEqual(self.outcomes(), ['json'])


# Only the two checks above run here; the inherited rules run in their own module.
for _name in dir(rules.CompileIntentRules):
    if _name.startswith('test_'):
        setattr(Witness, _name, None)


if __name__ == '__main__':
    unittest.main()
