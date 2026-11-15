"""Verify rules as input tables: who verifies, what a verdict needs, when a card changed."""
from __future__ import annotations

import unittest

from memvane.settings import types as settings_types
from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import CARD_REL, CLAIM, VerifyCase

ALL_HOSTS = {'extractor': {'builtins': ['claude', 'codex', 'hermes']}}


class VerifyAttempts(unittest.TestCase):
    def test_the_producing_engine_verifies_in_one_attempt(self):
        for producing in ('claude', 'codex', 'hermes'):
            with self.subTest(producing=producing):
                attempts, policy, reason = worker.plan_verify_attempts(ALL_HOSTS, {}, producing)
                self.assertEqual(len(attempts), 1)
                self.assertEqual({key: attempts[0][key] for key in ('engine', 'mode', 'key')},
                                 {'engine': producing, 'mode': settings_types.VERIFIED_MODE_SELF,
                                  'key': producing})
                self.assertEqual((policy, reason), ('off', ''))

    def test_an_unknown_or_unresolvable_producer_defers_without_paying_another_cli(self):
        table = [
            ('', ALL_HOSTS, {}),
            (settings_types.UNKNOWN_ENGINE, ALL_HOSTS, {}),
            ('gemini', ALL_HOSTS, {}),
            ('claude', {'extractor': {'builtins': ['codex']}}, {}),
            ('claude', {}, {'builtins': ['codexx']}),  # a verify.builtins typo adds nothing
        ]
        for producing, compile_cfg, vcfg in table:
            with self.subTest(producing=producing, compile_cfg=compile_cfg, vcfg=vcfg):
                self.assertEqual(worker.plan_verify_attempts(compile_cfg, vcfg, producing),
                                 ([], 'off', 'producing_engine_unavailable'))

    def test_verify_builtins_extend_the_declared_engines(self):
        attempts, _, reason = worker.plan_verify_attempts({}, {'builtins': ['codex']}, 'codex')
        self.assertEqual(([a['engine'] for a in attempts], reason), (['codex'], ''))
        # A typo there drops nothing: the producer still resolves from the extractor engines.
        attempts, _, reason = worker.plan_verify_attempts(ALL_HOSTS, {'builtins': ['codexx']}, 'claude')
        self.assertEqual(([a['engine'] for a in attempts], reason), (['claude'], ''))

    def test_a_cooled_producer_waits_for_its_cooldown(self):
        self.assertEqual(worker.plan_verify_attempts(ALL_HOSTS, {}, 'claude', {'claude'}),
                         ([], 'off', 'engines_cooling'))
        attempts, _, reason = worker.plan_verify_attempts(ALL_HOSTS, {}, 'claude', {'codex', 'hermes'})
        self.assertEqual(([a['engine'] for a in attempts], reason), (['claude'], ''))
        self.assertEqual(worker.plan_verify_attempts(ALL_HOSTS, {}, 'gemini', {'gemini'})[2],
                         'producing_engine_unavailable')


class ChangedDuringVerify(unittest.TestCase):
    def changed(self, job_hash, fresh_hash, status='generated', created_by='memvane',
                payload='card-text', fresh='card-text'):
        return worker.card_changed_during_verify(job_hash, {'createdBy': created_by}, status, fresh_hash,
                                                 payload, fresh)

    def test_the_marker_status_and_owner_axes(self):
        self.assertFalse(self.changed('aaaa1111', 'aaaa1111'))
        self.assertTrue(self.changed('aaaa1111', 'bbbb2222'))
        self.assertTrue(self.changed('aaaa1111', ''), 'a removed auto block is a change')
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111', status='verified'))
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111', created_by='human'))
        self.assertFalse(self.changed('aaaa1111', 'aaaa1111', created_by='qmd-auto-context'),
                         'the 0.x plugin owner name still owns its cards')

    def test_full_text_equality_catches_body_and_provenance_edits(self):
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111', payload='---\nstatus: generated\n---\nport 8080\n',
                                     fresh='---\nstatus: generated\n---\nport 9999\n'))
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111',
                                     payload='---\nsourceRevisions:\n  - {path: "a.md"}\n---\nbody\n',
                                     fresh='---\nsourceRevisions:\n  - {path: "b.md"}\n---\nbody\n'))
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111', payload=None))
        self.assertTrue(self.changed('aaaa1111', 'aaaa1111', fresh=None))
        self.assertTrue(self.changed('', '', payload='a', fresh='b'))

    def test_a_legacy_job_without_a_hash_skips_only_the_marker_axis(self):
        self.assertFalse(self.changed('', ''))
        self.assertFalse(self.changed('', 'anything'))
        self.assertTrue(self.changed('', '', status='verified'))


class VerdictTable(VerifyCase):
    """What a verifier answer does to the card (one paid call each)."""

    def verdict(self, answer):
        self.setup()
        # An unusable answer cools the producer; each row starts uncooled.
        (self.compile_dir / 'verify-engine-cooldown.json').unlink(missing_ok=True)
        fake = self.fake(answer)
        result = self.run_verify()
        self.assertEqual(len(fake.calls), 1)
        return result, self.log()[-1]

    def test_a_pass_without_evidence_is_an_invalid_verdict(self):
        for claims in ([], [None], ['c'], [{}], ['', 0]):
            with self.subTest(claims=claims):
                result, row = self.verdict({'verdict': 'pass', 'claims': claims, 'reasons': []})
                self.assertEqual((row['result'], row['reason']), ('skipped', 'invalid_verdict'))
                self.assertEqual(result['processed'], 1, 'terminal: no paid retry loop')
                self.assertTrue(self.card.exists(), 'never deleted')
                self.assert_status('generated')
                self.assertNotRegex(self.card_now(), '(?m)^verifiedBy:')
                self.assertEqual(self.ledger('verify-deleted.jsonl'), [])
                self.assertEqual(self.queued(), '')

    def test_one_claim_from_the_prompt_contract_verifies(self):
        result, row = self.verdict({'verdict': 'pass', 'claims': [CLAIM], 'reasons': []})
        self.assertEqual((row['result'], row['claims'], result['processed']), ('verified', 1, 1))
        self.assert_status('verified')

    def test_values_outside_the_verdict_vocabulary_are_invalid(self):
        for answer in ({'verdict': 'duplicate', 'claims': [CLAIM]}, {'claims': [CLAIM]},
                       {'verdict': 'PASS', 'claims': [CLAIM]}):
            with self.subTest(answer=answer):
                _, row = self.verdict(answer)
                self.assertEqual((row['result'], row['reason']), ('skipped', 'invalid_verdict'))
                self.assert_status('generated')

    def test_fail_and_inconclusive_delete_without_a_route_and_keep_with_none(self):
        for verdict, action, card_left in (('fail', 'delete', False), ('inconclusive', 'delete', False),
                                           ('fail', 'none', True), ('inconclusive', 'none', True)):
            with self.subTest(verdict=verdict, action=action):
                self.setup()
                self.fake({'verdict': verdict, 'claims': [], 'reasons': ['r']})
                self.run_verify(verify={'onFail': action, 'onInconclusive': action})
                self.assertEqual(self.card.exists(), card_left)
                expected = 'deleted' if action == 'delete' else ('inconclusive' if verdict == 'inconclusive'
                                                                 else 'kept')
                self.assertEqual(self.log()[-1]['result'], expected)
                if card_left:
                    self.assert_status('generated')


if __name__ == '__main__':
    unittest.main()
