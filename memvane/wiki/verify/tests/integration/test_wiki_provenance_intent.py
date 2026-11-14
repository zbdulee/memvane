"""Compile intents written by the verify worker on a selected install (store format 4).

A pass on a job with observed source fingerprints writes one compile intent
naming the exact stamped card bytes, unless the checkout changed or cannot be
read (the stamp still happens); a failed stamp supersedes its intent. Consuming
intents is the sync domain's (``tests/e2e/wiki_db_provenance.py`` runs both).
"""
from __future__ import annotations

import contextlib
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

    def test_a_symlinked_card_is_refused_before_its_intent_is_acknowledged(self):
        from memvane.journal import api as journal
        root = self.tmp / 'root'
        wiki = root / 'wiki'
        wiki.mkdir(parents=True)
        state = self.tmp / 'state'
        state.mkdir()
        intents = state / 'provenance-intents'
        intents.mkdir(mode=0o700)
        foreign = self.tmp / 'foreign'
        foreign.write_text('foreign card', encoding='utf-8')
        pending = wiki / '.pending'
        pending.symlink_to(foreign)
        spec = {'viewId': 'a' * 64, 'scanId': 'b' * 32}
        result = {'viewId': spec['viewId'], 'targetPath': 'live.md', 'cardSha': sha(foreign.read_bytes()),
                  'sourceFingerprints': spec}
        path = intents / ('compile-' + verify_api.compile_intent_id(result) + '.json')
        journal.ledger_write(path, {'kind': 'compile', **result}, replace=False)
        with mock.patch('memvane.journal.api.reader_state', return_value=state), \
                self.assertRaises((OSError, ValueError, RuntimeError)):
            worker._supersede_job_intent(root, {'wikiPath': 'wiki'},
                                         {'targetPath': 'wiki/live.md', 'sourceFingerprints': spec},
                                         pending, {'journalPath': str(state / 'journal.sqlite')})
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name(path.stem + '.superseded.json').exists())


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
        self.log = self.home / 'verify-log.jsonl'
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
        """The locked apply step with the checkout issued before the verifier call."""
        checkout = checkout or self.checkout()
        with workspace.card_write_lock(self.root), workspace.mutation_lock(self.root):
            return worker.apply_verify_verdict_locked(
                self.root, self.config(), {}, {}, job, target, '', raw, 'claude', 'self', {},
                'pass', claims, [], [], self.log, spans=[], checkout=checkout)

    def assert_stamped_without_intent(self, target, reason=None):
        self.assertEqual(self.intent_files(), [])
        self.assertIn('status: verified', target.read_text(encoding='utf-8'))
        if reason:
            self.assertIn(reason, [row.get('reason') for row in jsonl(self.log)])

    def test_a_verified_card_gets_one_intent_for_its_stamped_bytes(self):
        target, raw, job = self.card()
        self.assertEqual(self.apply_locked(target, raw, job), (True, False))
        files = self.intent_files()
        self.assertEqual(len(files), 1, self.log.read_text(encoding='utf-8') if self.log.exists() else '')
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
        self.assertEqual(worker.process_verify_job(self.root, config, config['compile'], {}, job, self.log),
                         (True, False))
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
            self.assertEqual(worker.process_verify_job(self.root, config, config['compile'], {}, job, self.log),
                             (True, False))
        self.assert_stamped_without_intent(target, 'provenance_checkout_unavailable')

    def test_invalid_digest_input_keeps_the_stamp_and_writes_no_intent(self):
        target, raw, job = self.card()
        self.assertEqual(self.apply_locked(target, raw, job, claims=[{'claim': float('nan')}]), (True, False))
        self.assert_stamped_without_intent(target, 'provenance_value_invalid')

    def test_a_failed_stamp_supersedes_its_intent(self):
        target, raw, job = self.card()
        with mock.patch.object(worker, 'write_rendered_verification', return_value=False):
            self.assertEqual(self.apply_locked(target, raw, job), (False, True))
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith('.superseded.json'))
        self.assertIn('status: generated', target.read_text(encoding='utf-8'))
        self.assertEqual(jsonl(self.log)[-1]['result'], 'stamp_failed')

    def test_a_crash_before_the_stamp_leaves_only_a_waiting_intent(self):
        target, raw, job = self.card()
        with mock.patch.object(worker, 'write_rendered_verification', side_effect=_Crash):
            with self.assertRaises(_Crash):
                self.apply_locked(target, raw, job)
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r'^compile-[0-9a-f]{64}\.json$')
        self.assertNotEqual(json.loads(files[0].read_text(encoding='utf-8'))['cardSha'], sha(target.read_bytes()))
        self.assertIn('status: generated', target.read_text(encoding='utf-8'))


class PendingReplay(SelectedCase):
    SOURCE_HASH = 'a' * 16

    def stage(self):
        from memvane.wiki.card import api as card
        note = self.project.write_note('notes/replay-source.md', 'Synthetic replay source.\n')
        revision = self.project.revision(note)
        self.live = self.project.wiki / 'concepts' / 'replay.md'
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_text('previous live card\n', encoding='utf-8')
        self.pending = self.live.with_name('.replay.md.pending-' + self.SOURCE_HASH)
        raw = ('---\ntitle: Replay\nstatus: generated\ncreatedBy: memvane\nsourceRevisions:\n  - '
               + card.dump_source_revisions([revision])[0] + '\n---\n'
               '<!-- qmd:auto:start id="main" sourceHash="' + self.SOURCE_HASH + '" -->\n'
               'Synthetic claim.\n<!-- qmd:auto:end -->\n')
        self.pending.write_text(raw, encoding='utf-8')
        job = {'targetPath': self.live.relative_to(self.root).as_posix(),
               'pendingPath': self.pending.relative_to(self.root).as_posix(),
               'expectedTargetHash': sha(self.live.read_bytes()), 'sourceHash': self.SOURCE_HASH,
               'engine': 'claude', 'sourceRevisions': [revision],
               'sourceFingerprints': self.fingerprints([revision])}
        return raw, job

    def interrupted(self, raw, job, crash=False):
        """Stamp the pending candidate and its intent; the publish step does not finish."""
        checkout = self.checkout()

        def publish(*_args, **_kwargs):
            if crash:
                raise _Crash()
            return 'retry'
        with workspace.card_write_lock(self.root), workspace.mutation_lock(self.root), \
                mock.patch.object(worker, 'publish_pending_replacement', side_effect=publish):
            return worker.apply_verify_verdict_locked(
                self.root, self.config(), {}, {}, job, self.pending, self.SOURCE_HASH, raw, 'claude', 'self', {},
                'pass', CLAIMS, [], [], self.log, spans=[], checkout=checkout)

    def replay(self, mode):
        raw, job = self.stage()
        with mock.patch('memvane.wiki.card.api.archive_card', return_value=True):
            self.assertEqual(self.interrupted(raw, job), (False, True))
            if mode in ('live', 'same-live'):
                os.replace(self.pending, self.live)
            elif mode == 'stale':
                self.live.write_text('new live card\n', encoding='utf-8')
            if mode in ('pending', 'live'):
                self.git('checkout', '-q', '-b', 'replay-next')
            self.assertEqual(worker.process_verify_job(self.root, self.config(), {}, {}, job, self.log),
                             (True, False))
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        return files[0]

    def assert_superseded(self, mode):
        self.assertTrue(self.replay(mode).name.endswith('.superseded.json'))

    def test_a_stamped_pending_replay_after_a_branch_change_supersedes_the_intent(self):
        self.assert_superseded('pending')
        self.assertIn('status: verified', self.live.read_text(encoding='utf-8'))

    def test_a_live_replay_after_a_branch_change_supersedes_the_intent(self):
        self.assert_superseded('live')
        self.assertIn('status: verified', self.live.read_text(encoding='utf-8'))

    def test_a_stale_pending_replay_supersedes_the_intent_and_keeps_the_newer_card(self):
        self.assert_superseded('stale')
        self.assertEqual(self.live.read_text(encoding='utf-8'), 'new live card\n')

    def test_a_same_view_replay_keeps_the_intent(self):
        for mode in ('same-pending', 'same-live'):
            with self.subTest(mode=mode):
                intent = self.replay(mode)
                self.assertFalse(intent.name.endswith('.superseded.json'))
                self.assertIn('status: verified', self.live.read_text(encoding='utf-8'))
                intent.unlink()

    def test_a_crash_after_the_pending_stamp_leaves_the_live_card_and_a_matching_intent(self):
        raw, job = self.stage()
        with self.assertRaises(_Crash):
            self.interrupted(raw, job, crash=True)
        files = self.intent_files()
        self.assertEqual(len(files), 1)
        stamped = self.pending.read_bytes()
        self.assertEqual(json.loads(files[0].read_text(encoding='utf-8'))['cardSha'], sha(stamped))
        self.assertIn(b'status: verified', stamped)
        self.assertEqual(self.live.read_text(encoding='utf-8'), 'previous live card\n')


if __name__ == '__main__':
    unittest.main()
