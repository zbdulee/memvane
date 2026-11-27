"""Compile intents written by the verify worker on a selected install (store format 4).

A pass on a row with observed source fingerprints writes one compile intent
naming the exact stamped card bytes before they are published, unless the
checkout changed or cannot be read (the stamp still happens); a publish that
does not land supersedes its intent. Consuming
intents is the sync domain's (``tests/e2e/wiki_db_provenance.py`` runs both).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock
import uuid

from memvane.wiki.verify import api as verify_api
from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import SOURCE_FINGERPRINTS_SCHEMA, jsonl
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.engines_fake import FakeEngines
from tests.support.install_builder import build_install
from tests.support.project import make_project

SOURCE = 'Synthetic source for verification.\n'
CLAIMS = [{'claim': 'Synthetic claim'}]


class _Crash(BaseException):
    """Stands in for a process killed at the patched point."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class IntentAck(FixtureCase):
    """Acknowledging (superseding) an intent never trusts a path that changed under it."""

    def private(self, path, text):
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
        return path

    def test_an_existing_acknowledgement_is_never_overwritten(self):
        path = self.private(self.tmp / 'compile-a.json', 'new')
        ack = self.private(self.tmp / 'compile-a.superseded.json', 'old')
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            verify_api.supersede_compile_intent(path)
        self.assertEqual((path.read_text(encoding='utf-8'), ack.read_text(encoding='utf-8')), ('new', 'old'))

    def test_a_replaced_intent_path_is_never_acknowledged(self):
        path = self.private(self.tmp / 'compile-a.json', 'original')
        old = self.tmp / 'original.json'
        replacement = self.private(self.tmp / 'replacement.json', 'replacement')
        real_lstat = Path.lstat

        def swap(candidate, *args, **kwargs):
            if candidate == path and replacement.exists():
                os.replace(path, old)
                os.replace(replacement, path)
            return real_lstat(candidate, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', new=swap), self.assertRaises((OSError, ValueError, RuntimeError)):
            verify_api.supersede_compile_intent(path)
        self.assertEqual(old.read_text(encoding='utf-8'), 'original')
        self.assertEqual(path.read_text(encoding='utf-8'), 'replacement')
        self.assertFalse((self.tmp / 'compile-a.superseded.json').exists())



class SelectedCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, name='provenance')
        anchor = self.project.write_note('notes/anchor.md', 'The emerald heron nests beside the pond.\n')
        self.project.write_card('concepts/anchor.md', title='Anchor', sources=[anchor],
                                body='The emerald heron nests beside the pond.\n')
        self.project.commit('fixture')
        self.root = self.project.root
        self.install = build_install(self, self.project)
        self.intents = self.install.journal.parent / 'provenance-intents'
        self.log = []  # any sink with append(); the worker logs verify rows into it
        for name in ('reindex_wiki', 'wake_pending_retirement'):
            patcher = mock.patch.object(worker, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args):
        self.project.git(*args)

    def checkout(self):
        checkout = workspace.schema3_checkout(workspace.selected(self.root))
        self.assertIsNotNone(checkout)
        return checkout

    def fingerprints(self, revisions):
        """The observed fingerprint spec the compiler records for ``revisions``."""
        from memvane.wiki.lifecycle import api as lifecycle
        checkout = self.checkout()
        capture = checkout.capture()
        tracked = lifecycle.tracked_states(self.root, [r['path'] for r in revisions], capture['headOid'])
        self.assertIs(checkout.matches(capture), True)
        scan, observed = uuid.uuid4().hex, time.time_ns()
        rows = [{'worktree_id': capture['worktreeId'], 'branch_key': capture['branchKey'], 'path': r['path'],
                 'source_sha': r['sha256'], 'byte_length': r['size'], 'head_oid': capture['headOid'],
                 'tracked_state': tracked[r['path']], 'binding_state': 'observed', 'scan_id': scan,
                 'observed_ns': observed} for r in revisions]
        return {'schema': SOURCE_FINGERPRINTS_SCHEMA, 'viewId': capture['viewId'],
                'checkoutDigest': capture['checkoutDigest'], 'gitObjectFormat': capture['gitObjectFormat'],
                'fingerprints': rows, 'scanId': scan, 'observedNs': observed}

    def intent_files(self):
        return sorted(self.intents.glob('compile-*.json')) if self.intents.exists() else []

    def config(self):
        from memvane.settings import api as settings
        return settings.load_project_config(str(self.root))


class Checkout(SelectedCase):
    def blockable_route(self):
        """Patch the journal route lookup; ``blocked`` (a list) switches it to a fence."""
        from memvane.journal import api as journal
        real = journal.select_route
        blocked = []

        def select_route(cwd, **kwargs):
            return {'state': 'blocked'} if blocked else real(cwd, **kwargs)
        patcher = mock.patch('memvane.journal.api.select_route', side_effect=select_route)
        patcher.start()
        self.addCleanup(patcher.stop)
        return blocked

    def test_the_checkout_tracks_the_registered_capture_and_a_branch_change(self):
        checkout = self.checkout()
        capture = checkout.capture()
        self.assertEqual(capture['branchKey'], 'refs/heads/main')
        self.assertTrue(checkout.matches(capture))
        self.git('checkout', '-q', '-b', 'provenance-next')
        self.assertFalse(checkout.matches(capture))
        after = self.checkout().capture()
        self.assertEqual(after['branchKey'], 'refs/heads/provenance-next')
        self.assertEqual(after['worktreeId'], capture['worktreeId'])

    def test_forged_authority_is_refused_and_a_route_fence_ends_the_checkout(self):
        bound = workspace.selected(self.root)
        with self.assertRaises((ValueError, RuntimeError)):
            workspace.schema3_checkout((bound[0], {}))
        blocked = self.blockable_route()
        checkout = workspace.schema3_checkout(bound)
        capture = checkout.capture()
        blocked.append(True)
        self.assertFalse(checkout.matches(capture))
        with self.assertRaises((ValueError, RuntimeError)):
            checkout.capture()

    def test_a_selection_change_during_issuance_or_capture_is_refused(self):
        from memvane.store import api as store
        bound = workspace.selected(self.root)
        blocked = self.blockable_route()
        real_binding, real_capture = store.observe_binding, store.capture_view

        def binding_then_fence(*args, **kwargs):
            value = real_binding(*args, **kwargs)
            blocked.append(True)
            return value
        with mock.patch('memvane.store.api.observe_binding', side_effect=binding_then_fence):
            with self.assertRaises((ValueError, RuntimeError)):
                workspace.schema3_checkout(bound)
        blocked.clear()
        checkout = workspace.schema3_checkout(bound)

        def capture_then_fence(*args, **kwargs):
            value = real_capture(*args, **kwargs)
            blocked.append(True)
            return value
        with mock.patch('memvane.store.api.capture_view', side_effect=capture_then_fence):
            with self.assertRaises((ValueError, RuntimeError)):
                checkout.capture()


class VerifyIntent(SelectedCase):
    def card(self):
        from memvane.wiki.card import api as card
        note = self.project.write_note('notes/provenance-source.md', SOURCE)
        revision = self.project.revision(note)
        target = self.project.wiki / 'concepts' / 'intent.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = ('---\ntitle: Intent\nstatus: generated\ncreatedBy: memvane\nsourceRevisions:\n  - '
               + card.dump_source_revisions([revision])[0] + '\n---\n\nSynthetic claim.\n')
        target.write_text(raw, encoding='utf-8')
        job = {'targetPath': target.relative_to(self.root).as_posix(), 'engine': 'claude',
               'sources': [{'kind': 'file', 'path': revision['path'], 'collection': revision['collection']}],
               'sourceRevisions': [revision], 'sourceFingerprints': self.fingerprints([revision])}
        return target, raw, job

    def apply_locked(self, target, raw, job, *, claims=CLAIMS, checkout=None):
        """Publish a recorded pass with the checkout issued before the verifier call."""
        checkout = checkout or self.checkout()
        recorded = {'verdict': 'pass', 'claims': claims, 'reasons': [], 'spans': [], 'engine': 'claude',
                    'mode': 'self', 'provenance': {}}
        wiki_root = worker._wiki_root(self.root, self.config())
        state = worker._publish_pass(self.root, self.config(), job, target, wiki_root, raw, sha(raw.encode()),
                                     recorded, worker.base_record(job), self.log, checkout)
        return state[2], state[0] == 'queued'

    def process(self, job, config=None):
        """Claim ``job`` as a verify row and settle it through the worker's own call."""
        from memvane.wiki.pipeline import api as pipeline
        config = config or self.config()
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('verify', [job])
            row = store.claim_verify_batch(os.getpid(), 'tok', budget=1)['claimed'][0]
            state = worker.process_row(self.root, config, config['compile'], {}, store, 'tok', row, self.log,
                                       workspace.selected(self.root))
        return state[2], state[0] == 'queued'

    def assert_stamped_without_intent(self, target, reason=None):
        self.assertEqual(self.intent_files(), [])
        self.assertIn('status: verified', target.read_text(encoding='utf-8'))
        if reason:
            self.assertIn(reason, [row.get('reason') for row in self.log])

    def test_a_verified_card_gets_one_intent_for_its_stamped_bytes(self):
        target, raw, job = self.card()
        self.assertEqual(self.apply_locked(target, raw, job), (True, False))
        files = self.intent_files()
        self.assertEqual(len(files), 1, json.dumps(self.log))
        intent = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual(intent['kind'], 'compile')
        self.assertEqual(intent['cardSha'], sha(target.read_bytes()))
        self.assertEqual(intent['targetPath'], 'concepts/intent.md')
        self.assertEqual(intent['sourceFingerprints'], job['sourceFingerprints'])
        self.assertEqual(files[0].name, 'compile-' + verify_api.compile_intent_id(intent) + '.json')

    def test_the_worker_writes_the_intent_through_its_own_verify_call(self):
        target, _, job = self.card()
        fake = FakeEngines({'verify': {'verdict': 'pass', 'claims': CLAIMS, 'reasons': []}}).install(self)
        config = self.config()
        self.assertEqual(self.process(job, config), (True, False))
        self.assertEqual(len(fake.calls), 1)
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding='utf-8'))['cardSha'], sha(target.read_bytes()))

    def test_a_head_advance_on_the_same_branch_keeps_provenance(self):
        target, raw, job = self.card()
        checkout = self.checkout()
        self.git('commit', '-q', '--allow-empty', '-m', 'advance')
        self.assertEqual(self.apply_locked(target, raw, job, checkout=checkout), (True, False))
        self.assertEqual(len(self.intent_files()), 1)

    def test_a_branch_change_skips_the_intent_but_keeps_the_stamp(self):
        target, raw, job = self.card()
        checkout = self.checkout()
        self.git('checkout', '-q', '-b', 'verify-next')
        self.assertEqual(self.apply_locked(target, raw, job, checkout=checkout), (True, False))
        self.assert_stamped_without_intent(target, 'provenance_checkout_changed')

    def test_an_unreadable_checkout_skips_the_intent_but_keeps_the_stamp(self):
        for error in (OSError('git unavailable'), sqlite3.OperationalError('fixture reader busy')):
            with self.subTest(error=type(error).__name__):
                target, raw, job = self.card()
                checkout = self.checkout()
                with mock.patch('memvane.store.api.capture_view', side_effect=error):
                    self.assertEqual(self.apply_locked(target, raw, job, checkout=checkout), (True, False))
                self.assert_stamped_without_intent(target, 'provenance_checkout_unavailable')

    def test_a_checkout_error_before_the_lock_keeps_the_stamp(self):
        target, _, job = self.card()
        FakeEngines({'verify': {'verdict': 'pass', 'claims': CLAIMS, 'reasons': []}}).install(self)
        config = self.config()
        with mock.patch('memvane.wiki.workspace.api.schema3_checkout',
                        side_effect=sqlite3.OperationalError('fixture reader busy')):
            self.assertEqual(self.process(job, config), (True, False))
        self.assert_stamped_without_intent(target, 'provenance_checkout_unavailable')

    def test_invalid_digest_input_keeps_the_stamp_and_writes_no_intent(self):
        target, raw, job = self.card()
        self.assertEqual(self.apply_locked(target, raw, job, claims=[{'claim': float('nan')}]), (True, False))
        self.assert_stamped_without_intent(target, 'provenance_value_invalid')

    def test_a_failed_publish_supersedes_its_intent(self):
        from memvane.wiki.pipeline import api as pipeline
        target, raw, job = self.card()
        with mock.patch.object(pipeline, 'publish_card', side_effect=OSError('disk full')):
            self.assertEqual(self.apply_locked(target, raw, job), (False, True))
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith('.superseded.json'))
        self.assertIn('status: generated', target.read_text(encoding='utf-8'))
        self.assertEqual(self.log[-1]['result'], 'publish_pending')

    def test_a_crash_before_the_publish_leaves_only_a_waiting_intent(self):
        from memvane.wiki.pipeline import api as pipeline
        target, raw, job = self.card()
        with mock.patch.object(pipeline, 'publish_card', side_effect=_Crash):
            with self.assertRaises(_Crash):
                self.apply_locked(target, raw, job)
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r'^compile-[0-9a-f]{64}\.json$')
        self.assertNotEqual(json.loads(files[0].read_text(encoding='utf-8'))['cardSha'], sha(target.read_bytes()))
        self.assertIn('status: generated', target.read_text(encoding='utf-8'))


class ReplaceIntent(SelectedCase):
    SOURCE_HASH = 'a' * 16

    def stage(self):
        from memvane.wiki.card import api as card
        note = self.project.write_note('notes/replay-source.md', 'Synthetic replay source.\n')
        revision = self.project.revision(note)
        self.live = self.project.wiki / 'concepts' / 'replay.md'
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_text('previous live card\n', encoding='utf-8')
        image = ('---\ntitle: Replay\nstatus: generated\ncreatedBy: memvane\nsourceRevisions:\n  - '
                 + card.dump_source_revisions([revision])[0] + '\n---\n'
                 '<!-- qmd:auto:start id="main" sourceHash="' + self.SOURCE_HASH + '" -->\n'
                 'Synthetic claim.\n<!-- qmd:auto:end -->\n')
        job = {'kind': 'replace', 'targetPath': self.live.relative_to(self.root).as_posix(),
               'expectedTargetHash': sha(self.live.read_bytes()), 'sourceHash': self.SOURCE_HASH,
               'engine': 'claude', 'sourceRevisions': [revision],
               'sources': [{'kind': 'file', 'path': revision['path'], 'collection': revision['collection']}],
               'sourceFingerprints': self.fingerprints([revision])}
        return image, job

    def settle(self, image, job, verdict):
        from memvane.wiki.pipeline import api as pipeline
        config = self.config()
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('verify', [job], pending_blobs=[image.encode()])
            row = store.claim_verify_batch(os.getpid(), 'tok', budget=1)['claimed'][0]
            return worker.process_row(self.root, config, config['compile'], {}, store, 'tok', row, self.log,
                                      workspace.selected(self.root))

    def test_a_replace_row_publishes_its_stamped_image_with_one_matching_intent(self):
        image, job = self.stage()
        FakeEngines({'verify': {'verdict': 'pass', 'claims': CLAIMS, 'reasons': []}}).install(self)
        self.assertEqual(self.settle(image, job, 'pass'), ('done', 'verified', True))
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        intent = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual((intent['cardSha'], intent['expectedTargetHash']),
                         (sha(self.live.read_bytes()), job['expectedTargetHash']))
        self.assertIn('status: verified', self.live.read_text(encoding='utf-8'))

    def test_a_human_edit_during_the_call_keeps_the_edit_and_supersedes_the_intent(self):
        image, job = self.stage()

        def edit_then_pass(payload, argv):
            self.live.write_text('human edit\n', encoding='utf-8')
            return {'verdict': 'pass', 'claims': CLAIMS, 'reasons': []}
        FakeEngines({'verify': edit_then_pass}).install(self)
        self.assertEqual(self.settle(image, job, 'pass'), ('done', 'changed_during_verify', True))
        self.assertEqual(self.live.read_text(encoding='utf-8'), 'human edit\n')
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith('.superseded.json'))


if __name__ == '__main__':
    unittest.main()
