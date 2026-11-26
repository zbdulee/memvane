"""The verify worker run in process over a fake host CLI.

Verdicts (a negative verdict keeps the card), the recorded verdict reused after
an interrupted publish, transient failures that are never a verdict, the
producing-engine policy and per-engine cooldowns, the run budget, and the
source snapshot a verifier is shown.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.settings import types as settings_types
from memvane.wiki.engines import api as engines
from memvane.wiki.verify import api as verify
from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import (
    CARD_REL, CLAIM, FAIL, HASH, INCONCLUSIVE, PASS, SOURCE, SOURCE_REL, VerifyCase, card_text)


class Pass(VerifyCase):
    def test_pass_stamps_verified_with_its_proof_fields_and_empties_the_queue(self):
        self.setup()
        fake = self.fake()
        result = self.run_verify()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        text = self.card_now()
        self.assert_status('verified')
        frontmatter = self.frontmatter()
        for pattern in (r'(?m)^verifiedBy: "?claude"?$', r'(?m)^verifiedAt: .+$',
                        r'(?m)^verifiedMode: "?self"?$', r'(?m)^verifiedBodyHash: .+$'):
            self.assertRegex(frontmatter, pattern)
        self.assertIn('Durable claim', text, 'the managed block body is kept')
        row = self.log()[0]
        self.assertEqual((row['result'], row['verifiedMode'], row['attemptedMode'], row['producedBy'],
                          row['engine'], row['claims']), ('verified', 'self', 'self', 'claude', 'claude', 1))
        self.assertEqual(self.queued(), '')
        self.assertFalse((self.root / '.memvane/compile/verify-deleted.jsonl').exists())

    def test_default_verification_uses_only_the_producing_host(self):
        for host in ('codex', 'claude', 'hermes'):
            with self.subTest(host=host):
                self.setup(engine=host)
                seen = []
                fake = self.fake(lambda payload, argv: seen.append(payload) or PASS)
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual([call['argv'] for call in fake.calls], [engines.builtin_adapter_argv(host)])
                row = self.log()[-1]
                self.assertEqual((row['verifiedMode'], row['crossEngine'], row['engine'], row['enginesAttempted']),
                                 ('self', 'off', host, [host]))
                self.assertEqual(row['_qmd']['reasoningEffort']['requested'], 'low')
                payload = seen[0]
                self.assertEqual((payload['task'], payload['engine'], payload['card']['path']),
                                 ('verify', host, CARD_REL))
                self.assertEqual([s['path'] for s in payload['sources']], [SOURCE_REL])

    def test_a_legacy_reviewed_scalar_neither_blocks_nor_protects(self):
        self.setup(extra_fm='reviewed: true')
        self.fake()
        self.run_verify()
        self.assert_status('verified')
        self.setup(extra_fm='reviewed: true')
        self.fake(INCONCLUSIVE)
        self.run_verify()
        self.assert_status('generated')

    def test_a_verified_card_is_never_reverified(self):
        self.setup(card_status='verified')
        fake = self.fake(FAIL)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(fake.calls, [], 'no paid call for an already verified card')
        self.assertTrue(self.card.exists())
        row = self.log()[0]
        self.assertEqual((row['reason'], row['producedBy']), ('not_generated', 'claude'))
        self.assertNotIn('engine', row, 'a row before any attempt names no verifying engine')

    def test_skips_that_change_nothing(self):
        table = [
            ({'sourceHash': 'deadbeef'}, 'stale_job'),
            ({'targetPath': '.memvane/wiki/concepts/absent.md'}, 'card_missing'),
        ]
        for overrides, reason in table:
            with self.subTest(reason=reason):
                before = self.setup(**overrides)
                fake = self.fake()
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.log()[-1]['reason'], reason)
                self.assertEqual(self.card_now(), before)
                self.assertEqual(self.queued(), '')

    def test_rows_with_missing_or_unsafe_targets_close_without_a_call(self):
        fake = self.fake()
        for target in ('', SOURCE_REL, '/abs.md'):
            with self.subTest(target=target):
                before = self.setup(targetPath=target)
                source = self.source.read_bytes()
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(self.queued(), '')
                self.assertEqual(self.rows()[-1]['reason'], 'unsafe_target_path')
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.card_now(), before)
                self.assertEqual(self.source.read_bytes(), source)

    def test_a_card_without_any_readable_source_stays_generated(self):
        before = self.setup(with_source=False)
        fake = self.fake()
        self.run_verify()
        self.assertEqual(self.log()[0]['reason'], 'source_missing')
        self.assertEqual(self.card_now(), before)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.compile_dir / 'source-missing.jsonl').exists())
        # A non-file source is not a missing source either.
        self.setup(with_source=False, sources=[{'kind': 'url', 'path': 'https://example.com/x'}])
        self.run_verify()
        self.assertEqual(self.log()[-1]['reason'], 'source_missing')
        self.assertFalse((self.compile_dir / 'source-missing.jsonl').exists())

    def test_verify_disabled_consumes_nothing(self):
        self.setup()
        fake = self.fake()
        self.assertEqual(self.run_verify(verify={'enabled': False})['processed'], 0)
        self.assertIn('test-card.md', self.queued())
        self.assertEqual(fake.calls, [])


class NegativeVerdicts(VerifyCase):
    def test_fail_and_inconclusive_keep_the_card_generated_without_any_deletion_ledger(self):
        for answer, result in ((FAIL, 'kept'), (INCONCLUSIVE, 'inconclusive')):
            with self.subTest(verdict=answer['verdict']):
                before = self.setup()
                fake = self.fake(answer)
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(self.card_now(), before)
                row = self.log()[-1]
                self.assertEqual((row['result'], row['verdict']), (result, answer['verdict']))
                self.assertEqual((self.rows()[-1]['state'], self.rows()[-1]['reason']), ('done', 'kept'))
                # The same row is never paid for again.
                self.assertEqual(self.run_verify()['processed'], 0)
                self.assertEqual(len(fake.calls), 1)
        for name in ('verify-deleted.jsonl', 'verify-skipped.jsonl', 'tombstones.jsonl',
                     'generated-manifest.jsonl'):
            self.assertEqual(self.ledger(name), [], name)

    def test_suppressions_ignore_malformed_rows_and_a_missing_ledger(self):
        path = self.compile_dir / 'verify-skipped.jsonl'
        path.write_text('\n'.join(['not json', '{"sourcePath":"a.md"}', '{"sourcePath":"","sourceBodyHash":"x"}',
                                   '{"sourcePath":"a.md","sourceBodyHash":"h1"}',
                                   '{"sourcePath":"a.md","sourceBodyHash":"h2"}']) + '\n', encoding='utf-8')
        self.assertEqual(verify.load_verify_suppressions(path), {'a.md': 'h2'})
        self.assertEqual(verify.load_verify_suppressions(None), {})
        self.assertEqual(verify.load_verify_suppressions(self.compile_dir / 'absent.jsonl'), {})

    def test_a_selected_route_keeps_every_card_and_wakes_sync_after_a_pass(self):
        from memvane.wiki.card import api as cards
        decision = {'recallSettings': {'skipPaths': []}, 'projectRoot': str(self.root),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32}
        self.project.write_note(SOURCE_REL, SOURCE)
        job = self.job(sourceHash='a' * 16)
        revisions = cards.dump_source_revisions(job['sourceRevisions'])
        frontmatter = 'sourceRevisions:\n' + '\n'.join('  - ' + entry for entry in revisions)
        self.card.parent.mkdir(parents=True, exist_ok=True)
        for answer, expected, card_left in ((FAIL, 'kept', True), (INCONCLUSIVE, 'inconclusive', True),
                                            (PASS, 'verified', True)):
            with self.subTest(verdict=answer['verdict']):
                self.card.write_text(card_text(source_hash=job['sourceHash'], extra_fm=frontmatter), encoding='utf-8')
                self.write_queue(job)
                self.fake(answer)
                with mock.patch('memvane.wiki.workspace.api.selected', return_value=(decision, {})), \
                        mock.patch('memvane.wiki.workspace.api.recheck') as recheck, \
                        mock.patch('memvane.wiki.workspace.api.request_sync') as wake:
                    self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(self.card.exists(), card_left)
                self.assertEqual(self.log()[-1]['result'], expected)
                self.assertGreater(recheck.call_count, 0)
                self.assertEqual(wake.call_count, 1 if expected == 'verified' else 0)
        self.assertEqual(self.ledger('verify-deleted.jsonl'), [])
        self.assertEqual(self.ledger('verify-skipped.jsonl'), [])


class RecordedVerdict(VerifyCase):
    """A paid verdict is recorded on its row before publishing (V12)."""

    def test_a_failed_publish_keeps_the_row_and_reuses_the_recorded_verdict(self):
        from memvane.wiki.pipeline import api as pipeline
        self.setup()
        fake = self.fake()
        with mock.patch.object(pipeline, 'publish_card', side_effect=OSError('disk full')):
            result = self.run_verify()
        self.assertEqual(result['processed'], 0)
        self.assert_status('generated')
        self.assertIn('test-card.md', self.queued())
        self.assertEqual((self.log()[-1]['result'], self.log()[-1]['reason']), ('publish_pending', 'write_failed'))
        self.assertEqual(self.rows()[-1]['payload']['_verdict']['verdict'], 'pass')
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1, 'the recorded verdict is reused, not paid again')
        self.assert_status('verified')

    def test_a_dead_claim_after_the_verdict_is_reused_and_a_fourth_claim_drops_the_row(self):
        from memvane.wiki.pipeline import api as pipeline
        self.setup()
        fake = self.fake()
        crash = RuntimeError('worker killed between the verdict and the publish')
        with mock.patch.object(worker, 'source_freshness', side_effect=crash), \
                mock.patch.object(worker.os, 'getpid', return_value=999999):
            with self.assertRaises(RuntimeError):
                self.run_verify()
        with pipeline.open_pipeline(self.root) as store:  # the crashed run never released its claim
            store._conn.execute("UPDATE verify_jobs SET state='claimed', claim_pid=999999")
            store._conn.commit()
        with mock.patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '999999'}):
            self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assert_status('verified')
        self.setup()
        with pipeline.open_pipeline(self.root) as store:
            store._conn.execute("UPDATE verify_jobs SET state='claimed', claim_pid=999999, attempt_count=4")
            store._conn.commit()
        with mock.patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '999999'}):
            self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((self.rows()[-1]['state'], self.rows()[-1]['reason']), ('dropped', 'requeue_exhausted'))
        self.assertEqual(self.log()[-1]['reason'], 'requeue_exhausted')


class Transient(VerifyCase):
    def test_an_absent_cli_is_logged_and_neither_cools_nor_deletes(self):
        self.setup()
        fake = self.fake(returncode=127)
        self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(self.card.exists())
        self.assert_status('generated')
        self.assertIn('test-card.md', self.queued())
        row = self.log()[0]
        self.assertEqual((row['result'], row['reason'], row['enginesAttempted']),
                         ('deferred', 'extractor_unavailable', ['claude']))
        self.assertEqual(self.engine_cooldown(), {})
        self.assertEqual(self.ledger('verify-skipped.jsonl'), [])

    def test_a_paid_failure_cools_only_its_engine_and_is_never_a_verdict(self):
        for kwargs, reason in (({'timeout': True}, 'extractor_timeout'), ({'returncode': 1}, 'extractor_failed')):
            with self.subTest(reason=reason):
                (self.compile_dir / 'verify-engine-cooldown.json').unlink(missing_ok=True)
                self.setup()
                fake = self.fake(**kwargs)
                self.assertEqual(self.run_verify()['processed'], 0)
                self.assertEqual(len(fake.calls), 1)
                self.assertTrue(self.card.exists())
                self.assertIn('test-card.md', self.queued())
                self.assertEqual(list(self.engine_cooldown()), ['claude'])
                self.assertFalse((self.compile_dir / 'cooldown').exists(), 'the compile cooldown is separate')
                row = self.log()[-1]
                self.assertEqual((row['result'], row['reason'], row['cooledKey'], row['attemptedMode']),
                                 ('deferred', reason, 'claude', 'self'))
                self.assertNotIn('verifiedMode', row, 'a failed attempt achieved no mode')
                self.assertEqual(self.ledger('verify-skipped.jsonl'), [])
                self.assertEqual(self.ledger('verify-deleted.jsonl'), [])

    def test_a_cooled_producer_spends_nothing_until_its_cooldown_ends(self):
        self.setup()
        fake = self.fake(returncode=1)
        self.run_verify()
        self.run_verify()
        self.assertEqual(len(fake.calls), 1, 'one paid call while the producer is cooled')
        row = self.log()[-1]
        self.assertEqual((row['result'], row['reason'], row['enginesCooling']),
                         ('deferred', 'engines_cooling', ['claude']))
        self.assertTrue(self.card.exists())
        self.assertIn('test-card.md', self.queued())

    def test_an_unbounded_cooldown_value_does_not_exclude_the_engine(self):
        self.setup()
        (self.compile_dir / 'verify-engine-cooldown.json').write_text(json.dumps({'claude': 9e18}), encoding='utf-8')
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assert_status('verified')

    def test_a_failed_cooldown_write_is_surfaced(self):
        self.setup()
        (self.compile_dir / 'verify-engine-cooldown.json').mkdir()
        self.fake(timeout=True)
        self.run_verify()
        row = self.log()[0]
        self.assertEqual((row['result'], row.get('cooldownWriteFailed')), ('deferred', True))
        self.assertTrue(self.card.exists())

    def test_an_unknown_producer_defers_without_any_call(self):
        for engine in (settings_types.UNKNOWN_ENGINE, 'gemini', ''):
            with self.subTest(engine=engine):
                self.setup(engine=engine)
                fake = self.fake()
                self.assertEqual(self.run_verify()['processed'], 0)
                self.assertEqual(fake.calls, [])
                self.assert_status('generated')
                self.assertIn('test-card.md', self.queued())
                self.assertEqual(self.log()[-1]['reason'], 'producing_engine_unavailable')

    def test_unusable_output_is_terminal_after_one_paid_call(self):
        self.setup()
        fake = self.fake(garbage=True)
        self.assertEqual(self.run_verify()['processed'], 1)
        row = self.log()[0]
        self.assertEqual((row['result'], row['reason']), ('skipped', 'invalid_extractor_json'))
        self.assertEqual(self.queued(), '')
        self.assertTrue(self.card.exists(), 'dropping the job never deletes the card')
        self.assertEqual(len(fake.calls), 1)

    def test_a_resource_deferral_keeps_the_job_and_cools_nothing(self):
        self.setup()
        with mock.patch('memvane.wiki.engines.api.run_extractor', return_value=(None, 'resource_deferred', 75)):
            self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual((self.log()[0]['result'], self.log()[0]['reason']), ('deferred', 'resource_deferred'))
        self.assertEqual(self.engine_cooldown(), {})
        self.assertIn('test-card.md', self.queued())

    def test_the_retired_global_cooldown_file_is_removed(self):
        self.setup()
        stale = self.compile_dir / 'verify-cooldown'
        stale.write_text('99999999999\n', encoding='utf-8')
        self.fake(returncode=127)
        self.run_verify()
        self.assertFalse(stale.exists())


class Budget(VerifyCase):
    def cards(self, names):
        jobs = []
        for name in names:
            rel = '.memvane/wiki/concepts/%s.md' % name
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(card_text(), encoding='utf-8')
            jobs.append(self.job(targetPath=rel, ts='2026-07-04T00:00:%02dZ' % len(jobs)))
        return jobs

    def test_this_runs_cards_go_first_and_one_backlog_slot_is_reserved(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        jobs = self.cards(['old-1', 'old-2', 'old-3', 'old-4', 'new-1', 'new-2'])
        self.write_queue(*jobs)
        fake = self.fake()
        produced = [jobs[4]['targetPath'], jobs[5]['targetPath']]
        result = self.run_verify(produced, budget={'verifyPerRun': 2})
        self.assertEqual({key: result[key] for key in ('budget', 'freshProcessed', 'backlogProcessed',
                                                      'processed', 'remaining')},
                         {'budget': 2, 'freshProcessed': 2, 'backlogProcessed': 1, 'processed': 3, 'remaining': 3})
        self.assertEqual(len(fake.calls), 3, 'budget plus the one reserved backlog slot')
        verified = sorted(r['targetPath'] for r in self.log() if r['result'] == 'verified')
        self.assertEqual(verified, sorted(produced + [jobs[0]['targetPath']]))

    def test_the_budget_follows_production_up_to_the_hard_cap(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        names = ['card-%02d' % i for i in range(settings_types.VERIFY_PRODUCED_HARD_CAP + 2)]
        jobs = self.cards(names)
        self.write_queue(*jobs)
        fake = self.fake()
        produced = [job['targetPath'] for job in jobs] + ['.memvane/wiki/concepts/ghost-%d.md' % i for i in range(8)]
        result = self.run_verify(produced, budget={'verifyPerRun': 1})
        self.assertEqual(result['budget'], settings_types.VERIFY_PRODUCED_HARD_CAP)
        self.assertEqual(result['producedCapped'], len(produced))
        self.assertEqual(len(fake.calls), settings_types.VERIFY_PRODUCED_HARD_CAP,
                         'model-derived production cannot raise paid calls past the cap')
        self.assertEqual(result['remaining'], 2)

    def test_a_human_budget_is_honoured_and_clamped(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        jobs = self.cards(['card-%02d' % i for i in range(settings_types.MAX_VERIFY_PER_RUN + 3)])
        self.write_queue(*jobs)
        fake = self.fake()
        result = self.run_verify(None, budget={'verifyPerRun': 10_000})
        self.assertEqual(result['budget'], settings_types.MAX_VERIFY_PER_RUN)
        self.assertEqual(len(fake.calls), settings_types.MAX_VERIFY_PER_RUN)
        self.assertNotIn('producedCapped', result)

    def test_the_latest_row_per_card_wins_even_after_a_dead_claim_returns_an_older_one(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        self.card.parent.mkdir(parents=True, exist_ok=True)
        self.card.write_text(card_text(source_hash='def456'), encoding='utf-8')
        older = self.job(sourceHash='abc123', ts='2026-07-04T00:00:00Z')
        newer = self.job(sourceHash='def456', ts='2026-07-05T00:00:00Z')
        self.write_queue(older, newer)
        fake = self.fake()
        result = self.run_verify()
        self.assertEqual((result['processed'], len(fake.calls), result['superseded']), (1, 1, 1))
        self.assert_status('verified')
        self.assertEqual(self.queued(), '')
        self.assertEqual([(r['payload']['sourceHash'], r['reason']) for r in self.rows()],
                         [('abc123', 'superseded'), ('def456', 'verified')])
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline(self.root) as store:  # a dead claim returns the older row
            store._conn.execute("UPDATE verify_jobs SET state='queued', reason=NULL WHERE reason='superseded'")
            store._conn.commit()
        self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(fake.calls), 1)


class SourceSnapshot(VerifyCase):
    def test_a_source_changed_before_the_call_closes_the_row_without_paying(self):
        self.setup()
        self.project.write_note(SOURCE_REL, '# Changed before verify\n')
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(fake.calls, [])
        self.assert_status('generated')
        self.assertEqual(self.queued(), '')
        self.assertEqual(self.rows()[-1]['reason'], 'stale_revision')
        self.assertEqual(self.log()[-1]['reason'], 'source_changed_before_verify')

    def test_empty_source_revisions_fail_closed(self):
        self.setup(sourceRevisions=[])
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(fake.calls, [])
        self.assert_status('generated')
        row = self.log()[-1]
        self.assertEqual((row['reason'], row['freshnessReason']),
                         ('source_changed_before_verify', 'missing_source_revisions'))

    def test_a_source_changed_during_the_call_is_never_stamped(self):
        self.setup()

        def edit_then_pass(payload, argv):
            Path(payload['cwd'], SOURCE_REL).write_text('# Changed during verify\n', encoding='utf-8')
            return PASS
        fake = self.fake(edit_then_pass)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertNotRegex(self.card_now(), '(?m)^status: verified$')
        self.assertEqual(self.queued(), '')
        self.assertEqual(self.rows()[-1]['reason'], 'stale_revision')
        self.assertEqual(self.log()[-1]['reason'], 'source_changed_during_verify')
        self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(fake.calls), 1)

    def test_a_card_changed_during_the_call_is_not_stamped(self):
        self.setup()

        def edit_card_then_pass(payload, argv):
            self.card.write_text(card_text().replace('cite markdown', 'cite HTML'), encoding='utf-8')
            return PASS
        self.fake(edit_card_then_pass)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assert_status('generated')
        self.assertIn('cite HTML', self.card_now())
        self.assertEqual(self.log()[-1]['reason'], 'changed_during_verify')

    def test_the_verifier_sees_the_revision_checked_snapshot_bytes(self):
        self.setup()
        transient = '# Transient B\n\nThis must never reach the verifier.\n'
        from memvane.wiki.card import api as card
        real = card.snapshot_bytes
        switched = []

        def aba(path):
            snapshot = real(path)
            if Path(path) == self.source and not switched:
                self.source.write_text(transient, encoding='utf-8')
                switched.append(True)
            return snapshot
        seen = []

        def restore_then_pass(payload, argv):
            seen.append(payload['sources'][0]['content'])
            self.source.write_text(SOURCE, encoding='utf-8')
            return PASS
        self.fake(restore_then_pass)
        with mock.patch('memvane.wiki.card.api.snapshot_bytes', side_effect=aba):
            self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(seen, [SOURCE], 'never the reopened transient bytes')
        self.assert_status('verified')

    def test_queue_sources_come_before_model_listed_decoys(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        for name in ('a', 'b', 'c'):
            self.project.write_note('docs/decoy-%s.md' % name, 'decoy %s\n' % name)
        self.card.parent.mkdir(parents=True, exist_ok=True)
        self.card.write_text(card_text(), encoding='utf-8')
        decoys = [{'kind': 'file', 'path': 'docs/decoy-%s.md' % n} for n in ('a', 'b', 'c')]
        job = self.job(sources=decoys, authoritativeSources=[{'kind': 'file', 'path': SOURCE_REL}])
        job.pop('sourceRevisions')
        self.write_queue(job)
        seen = []
        self.fake(lambda payload, argv: seen.append([s['path'] for s in payload['sources']]) or PASS)
        self.run_verify()
        self.assertEqual(seen, [[SOURCE_REL, 'docs/decoy-a.md', 'docs/decoy-b.md']])


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class SourceSpans(VerifyCase):
    def bind(self, text=None, *, card=True, extra_fm=''):
        """Queue a job for ``text`` (or the default source) and give the card its own revisions."""
        from memvane.wiki.card import api as card_api
        self.setup(extra_fm=extra_fm)
        if text is not None:
            self.project.write_note(SOURCE_REL, text)
        revision = self.project.revision(self.source)
        self.write_queue(self.job())
        if card:
            line = card_api.dump_source_revisions([revision])[0]
            self.card.write_text(self.card_now().replace(
                'createdBy: memvane\n', 'createdBy: memvane\nsourceRevisions:\n  - ' + line + '\n', 1),
                encoding='utf-8')
        return revision

    def passing(self, **claim):
        return self.fake({'verdict': 'pass', 'claims': [{**CLAIM, **claim}], 'reasons': []})

    def test_an_exact_single_line_quote_is_stamped_without_its_text(self):
        quote = 'the source documents cite'
        self.passing(quote=quote)
        revision = self.bind()
        self.assertEqual(self.run_verify()['processed'], 1)
        frontmatter = self.frontmatter()
        self.assertRegex(frontmatter, '(?m)^status: verified$')
        line_sha = sha256('Durable claim: the source documents cite markdown.\n')
        self.assertIn('sourceSpans:\n  - {path: "docs/source.md", sourceSha256: "%s", startLine: 3, endLine: 3, '
                      'spanSha256: "%s"}' % (revision['sha256'], line_sha), frontmatter)
        self.assertNotIn(quote, frontmatter)
        row = self.log()[-1]
        self.assertEqual((row['result'], row['sourceSpans']), ('verified', 1))
        self.assertNotIn(quote, json.dumps(self.log()))

    def test_a_multiline_quote_covers_the_whole_inclusive_line_slice(self):
        self.passing(quote='line one\nbeta')
        self.bind('# Source\n\nalpha line one\nbeta line two\ngamma\n')
        self.assertEqual(self.run_verify()['processed'], 1)
        frontmatter = self.frontmatter()
        self.assertIn('startLine: 3, endLine: 4, ', frontmatter)
        self.assertIn('spanSha256: "%s"' % sha256('alpha line one\nbeta line two\n'), frontmatter)

    def test_unlocatable_quotes_keep_file_level_verified_with_no_span_key(self):
        table = [
            ('duplicate quote', {'quote': 'repeat me'}, '# S\n\nrepeat me\nrepeat me\n', None, True),
            ('paraphrase', {'quote': 'the source docs cite markdown'}, None, None, True),
            ('truncated tail', {'quote': 'tail fact'}, '# S\n\n%s\ntail fact\n' % ('x' * 40), 30, True),
            ('decoy sourcePath', {'quote': 'the source documents cite', 'sourcePath': 'docs/decoy.md'},
             None, None, True),
            ('supported false', {'quote': 'the source documents cite', 'supported': False}, None, None, True),
            ('supported null', {'quote': 'the source documents cite', 'supported': None}, None, None, True),
            ('card without its own revisions', {'quote': 'the source documents cite'}, None, None, False),
        ]
        for label, claim, text, max_chars, own in table:
            with self.subTest(label=label):
                self.passing(**claim)
                self.bind(text, card=own)
                overrides = {'maxSourceChars': max_chars} if max_chars else {}
                self.assertEqual(self.run_verify(**overrides)['processed'], 1)
                frontmatter = self.frontmatter()
                self.assertRegex(frontmatter, '(?m)^status: verified$')
                self.assertNotIn('sourceSpans', frontmatter)
                self.assertEqual(self.log()[-1]['sourceSpans'], 0)

    def test_tampered_span_headers_are_replaced_by_one_fresh_list(self):
        self.passing(quote='the source documents cite')
        self.bind(extra_fm='sourceSpans:\n  - {junk: "1"}\n"sourceSpans":\n  - x')
        self.assertEqual(self.run_verify()['processed'], 1)
        frontmatter = self.frontmatter()
        self.assertRegex(frontmatter, '(?m)^status: verified$')
        self.assertEqual(frontmatter.count('sourceSpans'), 1)
        self.assertNotIn('junk', frontmatter)
        self.assertIn('startLine: 3, endLine: 3', frontmatter)

    def test_a_source_changed_during_the_call_stamps_neither_status_nor_spans(self):
        def edit(payload, argv):
            Path(payload['cwd'], SOURCE_REL).write_text(
                '# S\n\ninserted\nDurable claim: the source documents cite markdown.\n', encoding='utf-8')
            return {'verdict': 'pass', 'claims': [{**CLAIM, 'quote': 'the source documents cite'}], 'reasons': []}
        self.fake(edit)
        self.bind()
        self.assertEqual(self.run_verify()['processed'], 1)
        frontmatter = self.frontmatter()
        self.assertRegex(frontmatter, '(?m)^status: generated$')
        self.assertNotIn('sourceSpans', frontmatter)
        self.assertEqual(self.log()[-1]['reason'], 'source_changed_during_verify')

if __name__ == '__main__':
    unittest.main()
