#!/usr/bin/env python3
"""DB provenance end to end on a selected 1.0 install (store format 4).

A temporary HOME holds one project onboarded through the 1.0 install use cases
(``tests.support.install_builder``: plan, prepare, initial build, finalize,
activate). A raw note is compiled into a generated card with observed source
fingerprints, the verify worker judges it (synthetic engine), leaves one
compile intent and stamps the card, and the selected sync pass consumes the
intent into a ``compiled`` attestation. Every check counts store rows, journal
epochs and intent files; none reads a wall clock.

Replaced only where a host install or a model is needed: the provider
(``tests.support.fake_provider``: review, runtime seal, identity proof, one
fixed vector per chunk, no warm service, lexical recall) and the verifier
extractor call (``tests.support.engines_fake``). Nothing outside the
temporary HOME is written.

Run one method per process through ``wiki-db-provenance.test.mjs``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402
from tests.support.engines_fake import FakeEngines  # noqa: E402
from tests.support.install_builder import build_install  # noqa: E402
from tests.support.project import make_project  # noqa: E402

INSTALL_ID = '4' * 32
SETTINGS_NAME = 'provenance'
CARD_REL = 'concepts/heron-dawn.md'
ANCHOR_REL = 'concepts/anchor.md'
PROMPT = 'emerald heron'
NO_PROVENANCE = {'consumed': 0, 'rejected': {}, 'waiting': 0, 'superseded': 0, 'unsafe': 0}
VERDICT = {'verdict': 'pass', 'claims': [{'claim': 'The emerald heron fishes at dawn.'}], 'reasons': []}


class _Crash(BaseException):
    """Stands in for a process killed between the intent write and the stamp."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _write_private(path, raw):
    """A settled private intent file (0600, one link), replacing any file there."""
    temporary = path.with_name('.fixture-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


class WikiDbProvenance(FixtureCase):
    # -- install -----------------------------------------------------------------
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home, name=SETTINGS_NAME)
        self.fixture.commit('fixture', everything=False)  # an empty commit: nothing tracked
        self.project, self.wiki, self.raw_name = self.fixture.root, self.fixture.wiki, self.fixture.raw_name
        anchor_note = self.fixture.write_note(
            'notes/anchor.md', 'The emerald heron nests beside the north pond.\n')
        self.source = self.fixture.write_note(
            'notes/heron-source.md', 'At dawn the emerald heron fishes in the shallow reeds.\n')
        self.fixture.write_card(ANCHOR_REL, title='Emerald heron', sources=[anchor_note],
                                body='The emerald heron nests beside the north pond.\n')
        self.card = self.wiki / CARD_REL
        self.log = self.home / 'verify-log.jsonl'
        self.engines = FakeEngines({'verify': VERDICT}).install(self)
        # Fixture runs queue no runtime wake (``runtime.api.request`` answers None there);
        # the verify worker's selected sync wake is recorded instead.
        self.wakes = []
        wake = mock.patch('memvane.runtime.api.request',
                          lambda kind, target='', **_kw: self.wakes.append((kind, target)) or {'status': 'queued'})
        wake.start()
        self.addCleanup(wake.stop)
        self.target = build_install(self, self.fixture, install_id=INSTALL_ID)
        self.state, self.decision = self.target.state, self.target.decision
        self.journal, self.store_path = self.target.journal, self.target.store_path
        self.intents = self.journal.parent / 'provenance-intents'
        with self.db() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0], 4)
        self.settle()

    def revision(self, path):
        return self.fixture.revision(path)

    # -- passes ------------------------------------------------------------------
    def run_worker(self):
        return self.target.run_sync()

    def epoch(self):
        return self.target.epoch()

    def sync(self):
        result = self.run_worker()
        self.assertIn(result.get('state'), ('indexed', 'unchanged'), result)
        return result

    def settle(self):
        """Run passes until one changes nothing: no journal fence, unchanged view."""
        return self.target.settle()

    def db(self):
        return self.target.db()

    # -- compile and verify ----------------------------------------------------------
    def compile_card(self):
        """The compile step's outputs: a generated card and the job's observed fingerprints."""
        from memvane.wiki.compile import wiki_compile_worker as compiler
        from memvane.wiki.workspace import api as workspace
        checkout = workspace.schema3_checkout(workspace.selected(self.project))
        self.assertIsNotNone(checkout)
        capture = checkout.capture()
        revision = self.revision(self.source)
        observed = compiler.source_fingerprints(self.project, checkout, capture, [revision], time.time_ns())
        first = observed['fingerprints'][0]
        fingerprints = dict(observed, scanId=first['scan_id'], observedNs=first['observed_ns'])
        self.fixture.write_card(CARD_REL, title='Heron at dawn', sources=[self.source], status='generated',
                                body='At dawn the emerald heron fishes in the shallow reeds.\n')
        self.job = {'targetPath': self.card.relative_to(self.project).as_posix(), 'engine': 'codex',
                    'sources': [{'kind': 'file', 'path': revision['path'], 'collection': self.raw_name}],
                    'sourceRevisions': [revision], 'sourceFingerprints': fingerprints,
                    'trigger': 'fixture'}
        # The generated card is a view member before it is verified.
        self.settle()
        return fingerprints

    def verify(self):
        from memvane.settings import api as settings
        from memvane.wiki.verify import wiki_verify_worker as verifier
        config = settings.load_project_config(str(self.project))
        compile_cfg = config['compile']  # the verify worker's own inputs (``wiki_verify_worker.main``)
        return verifier.process_verify_job(self.project, config, compile_cfg, compile_cfg.get('verify', {}),
                                           copy.deepcopy(self.job), self.log)

    def verify_log(self):
        return self.log.read_text(encoding='utf-8') if self.log.exists() else ''

    def pending(self):
        if not self.intents.exists():
            return []
        return sorted(path for path in self.intents.iterdir()
                      if path.name.startswith('compile-') and path.name.endswith('.json')
                      and path.name.count('.') == 1)

    def verified_intent(self):
        """Compile, then verify: one pending intent naming the stamped card bytes."""
        from memvane.wiki.verify import api as wiki_verify
        self.compile_card()
        self.assertEqual(self.verify(), (True, False), self.verify_log())
        pending = self.pending()
        self.assertEqual(len(pending), 1, self.verify_log())
        path = pending[0]
        intent = json.loads(path.read_bytes())
        stamped = self.card.read_bytes()
        self.assertIn(b'status: verified', stamped)
        self.assertEqual(intent['kind'], 'compile')
        self.assertEqual(intent['targetPath'], CARD_REL)
        self.assertEqual(intent['cardSha'], _sha(stamped))
        self.assertEqual(intent['sourceFingerprints'], self.job['sourceFingerprints'])
        self.assertEqual(path.name, 'compile-' + wiki_verify.compile_intent_id(intent) + '.json')
        self.assertEqual(path.read_bytes(), _canonical(intent))
        return path, intent

    def hold_and_settle(self, path):
        """Publish the stamped card while the intent is set aside, then put it back."""
        held = path.with_name(path.name[:-len('.json')] + '.held')
        os.rename(path, held)
        result = self.settle()
        self.assertEqual(self.pending(), [])
        os.rename(held, path)
        return result

    # -- store observation -----------------------------------------------------------
    def observe(self):
        with self.db() as db:
            heads = db.execute('SELECT g.gen_id,h.epoch FROM s_heads h JOIN s_generations g '
                               'ON g.view_id=h.view_id AND g.ordinal=h.ordinal').fetchall()
            member = db.execute(
                'SELECT c.blob_sha,c.trust_digest FROM s_cards c JOIN s_heads h ON h.view_id=c.view_id '
                'WHERE c.path=? AND c.gen_from<=h.ordinal AND (c.gen_to IS NULL OR c.gen_to>h.ordinal)',
                (CARD_REL,)).fetchall()
            counts = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('card_attestations', 'attestation_sources', 'source_fingerprints',
                                    's_generations')}
            compiled = db.execute("SELECT count(*) FROM card_attestations WHERE verification_kind='compiled'"
                                  ).fetchone()[0]
        self.assertEqual(len(heads), 1)
        return {'epoch': self.epoch(), 'head': tuple(heads[0]),
                'member': tuple(member[0]) if len(member) == 1 else None,
                'counts': counts, 'compiled': compiled}

    def compiled_rows(self, blob):
        with self.db() as db:
            return [tuple(row) for row in db.execute(
                "SELECT trust_digest,engine_identity,receipt_json,source_count FROM card_attestations "
                "WHERE verification_kind='compiled' AND blob_sha=?", (blob,))]

    def attested_sources(self, trust):
        with self.db() as db:
            return [tuple(row) for row in db.execute(
                'SELECT f.worktree_id,f.branch_key,f.path,f.source_sha,f.byte_length,f.head_oid,'
                'f.tracked_state,f.binding_state,f.scan_id,f.observed_ns,a.spans_json '
                'FROM attestation_sources a JOIN source_fingerprints f ON f.fingerprint_id=a.fingerprint_id '
                'WHERE a.trust_digest=? ORDER BY a.ordinal', (trust,))]

    def store_verify(self):
        from memvane.store.api import open_store
        with self.db() as db:
            repository = json.loads(db.execute(
                'SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()[0])
        store = open_store(self.store_path, repository=repository, scope_id=self.decision['scopeId'],
                           reader=True)
        try:
            return store.verify()
        finally:
            store.close()

    def recall(self, host):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        if host == 'codex':
            payload = {'session_id': 'c10-codex', 'turn_id': 'c10-turn', 'transcript_path': None,
                       'cwd': str(self.project), 'hook_event_name': 'UserPromptSubmit',
                       'model': 'synthetic', 'prompt': PROMPT}
        else:
            payload = {'session_id': 'c10-claude', 'transcript_path': str(self.home / 'transcript.jsonl'),
                       'cwd': str(self.project), 'hook_event_name': 'UserPromptSubmit',
                       'permission_mode': 'default', 'prompt': PROMPT}
        decision = select_route(str(self.project), action='recall')
        self.assertEqual(decision.get('state'), 'candidate', decision)
        return serve_selected('recall', host, decision, payload)

    # -- checks ------------------------------------------------------------------
    def test_verified_intent_is_consumed_once_without_member_change(self):
        path, intent = self.verified_intent()
        self.hold_and_settle(path)
        before = self.observe()
        self.assertIsNotNone(before['member'], before)
        self.assertEqual(before['member'][0], intent['cardSha'])
        self.assertEqual(before['compiled'], 0)

        result = self.sync()
        self.assertEqual(result.get('provenance'), {'consumed': 1, 'rejected': {}, 'waiting': 0,
                                                    'superseded': 0, 'unsafe': 0}, result)
        after = self.observe()
        self.assertEqual(after['epoch'], before['epoch'] + 1)  # one journal fence for the store write
        self.assertEqual(after['head'], before['head'])
        self.assertEqual(after['counts']['s_generations'], before['counts']['s_generations'])
        self.assertEqual(after['member'], before['member'])  # trust_digest unchanged
        self.assertEqual(after['compiled'], 1)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name(path.stem + '.consumed.json').is_file())

        from memvane.store.blob import blob_values
        rows = self.compiled_rows(intent['cardSha'])
        self.assertEqual(len(rows), 1)
        trust, engine, receipt_json, source_count = rows[0]
        self.assertNotEqual(trust, before['member'][1])  # evidence row, not the member's trust
        self.assertEqual(engine, intent['producingEngine'])
        receipt = json.loads(receipt_json)
        self.assertEqual(set(receipt), set(blob_values.RECEIPT_V2_FIELDS))
        self.assertEqual(receipt, {'schema': 'memvane-card-evidence-v2', 'verdict': 'verified',
                                   'result_digest': intent['result_digest'], 'prior_trust': None,
                                   'verdict_digest': intent['verdict_digest'],
                                   'verified_mode': intent['verifiedMode'],
                                   'verify_engine': intent['verifyEngine']})
        fingerprints = intent['sourceFingerprints']['fingerprints']
        self.assertEqual(source_count, len(fingerprints))
        self.assertEqual(self.attested_sources(trust), [
            (fp['worktree_id'], fp['branch_key'], fp['path'], fp['source_sha'], fp['byte_length'],
             fp['head_oid'], fp['tracked_state'], 'observed', fp['scan_id'], fp['observed_ns'], '[]')
            for fp in fingerprints])
        self.assertEqual(after['counts']['card_attestations'], before['counts']['card_attestations'] + 1)
        self.assertEqual(after['counts']['attestation_sources'],
                         before['counts']['attestation_sources'] + len(fingerprints))

        again = self.sync()
        self.assertEqual(again.get('provenance'), NO_PROVENANCE, again)
        final = self.observe()
        self.assertEqual(final['epoch'], after['epoch'])  # no fence: nothing to write
        self.assertEqual(final['counts'], after['counts'])
        self.assertEqual(final['head'], after['head'])
        self.assertEqual(final['member'], after['member'])
        self.assertTrue(self.store_verify())

    def test_intent_without_stamp_waits_without_store_write(self):
        self.compile_card()
        generated = self.card.read_bytes()
        with mock.patch('memvane.wiki.verify.wiki_verify_worker.write_rendered_verification',
                        side_effect=_Crash('killed_before_stamp')):
            with self.assertRaises(_Crash):
                self.verify()
        self.assertEqual(self.card.read_bytes(), generated)
        pending = self.pending()
        self.assertEqual(len(pending), 1, self.verify_log())
        raw = pending[0].read_bytes()
        intent = json.loads(raw)
        self.assertNotEqual(intent['cardSha'], _sha(generated))
        before = self.observe()
        self.assertIsNotNone(before['member'], before)
        self.assertEqual(before['member'][0], _sha(generated))

        result = self.sync()
        self.assertEqual(result.get('provenance'), {'consumed': 0, 'rejected': {}, 'waiting': 1,
                                                    'superseded': 0, 'unsafe': 0}, result)
        after = self.observe()
        self.assertEqual(after, before)  # no fence, no evidence row, no generation
        self.assertEqual(after['compiled'], 0)
        self.assertEqual(sorted(path.name for path in self.intents.iterdir()), [pending[0].name])
        self.assertEqual(pending[0].read_bytes(), raw)

    def test_unacknowledged_consumption_replays_idempotently(self):
        path, intent = self.verified_intent()
        self.hold_and_settle(path)
        first = self.sync()
        self.assertEqual(first.get('provenance', {}).get('consumed'), 1, first)
        consumed = path.with_name(path.stem + '.consumed.json')
        self.assertTrue(consumed.is_file())
        committed = self.observe()
        self.assertEqual(committed['compiled'], 1)
        # The attestation commit survived; the acknowledging rename did not.
        os.rename(consumed, path)

        replay = self.sync()
        self.assertEqual(replay.get('provenance'), {'consumed': 1, 'rejected': {}, 'waiting': 0,
                                                    'superseded': 0, 'unsafe': 0}, replay)
        after = self.observe()
        self.assertEqual(after['compiled'], 1)
        self.assertEqual(after['counts'], committed['counts'])  # the store write is idempotent
        self.assertEqual(after['head'], committed['head'])
        self.assertEqual(after['member'], committed['member'])
        self.assertEqual(len(self.compiled_rows(intent['cardSha'])), 1)
        self.assertFalse(path.exists())
        self.assertTrue(consumed.is_file())
        self.assertTrue(self.store_verify())

    def test_rejections_keep_other_intents_consumable_in_the_same_pass(self):
        from memvane.wiki.verify import api as wiki_verify
        path, intent = self.verified_intent()
        before = self.observe()

        def named(value):
            return self.intents / ('compile-' + wiki_verify.compile_intent_id(value) + '.json')
        corrupt = self.intents / ('compile-' + '0' * 64 + '.json')
        _write_private(corrupt, b'{"kind": "compile", ')
        noncanonical = self.intents / ('compile-' + '1' * 64 + '.json')
        _write_private(noncanonical, json.dumps(intent, indent=2).encode('utf-8'))
        misnamed = self.intents / ('compile-' + '2' * 64 + '.json')
        _write_private(misnamed, _canonical(intent))
        tampered_value = copy.deepcopy(intent)
        tampered_value['sourceFingerprints']['fingerprints'][0]['source_sha'] = 'b' * 64
        tampered_value['sourceFingerprints']['scanId'] = uuid.uuid4().hex
        tampered = named(tampered_value)
        _write_private(tampered, _canonical(tampered_value))
        view_value = copy.deepcopy(intent)
        view_value['viewId'] = 'e' * 64
        other_view = named(view_value)
        _write_private(other_view, _canonical(view_value))
        tree_value = copy.deepcopy(intent)
        tree_value['sourceFingerprints']['fingerprints'][0]['worktree_id'] = 'd' * 32
        tree_value['sourceFingerprints']['scanId'] = uuid.uuid4().hex
        other_tree = named(tree_value)
        _write_private(other_tree, _canonical(tree_value))
        self.assertEqual(len({path, corrupt, noncanonical, misnamed, tampered, other_view, other_tree}), 7)

        # One pass ingests the stamped card and consumes in the same view pass
        # (its vector generation may still be filling: provenance is per pass).
        result = self.run_worker()
        self.assertIn(result.get('state'), ('indexed', 'unchanged', 'pending'), result)
        self.assertEqual(result.get('provenance'), {
            'consumed': 1, 'waiting': 0, 'superseded': 0, 'unsafe': 0,
            'rejected': {'provenance_value_invalid': 3, 'provenance_parity_mismatch': 1}}, result)
        for rejected in (corrupt, noncanonical, misnamed, tampered):
            self.assertFalse(rejected.exists(), rejected.name)
            self.assertTrue(rejected.with_name(rejected.stem + '.rejected.json').is_file(), rejected.name)
        self.assertTrue(path.with_name(path.stem + '.consumed.json').is_file())
        for untouched, value in ((other_view, view_value), (other_tree, tree_value)):
            self.assertEqual(untouched.read_bytes(), _canonical(value))
        after = self.observe()
        self.assertEqual(after['compiled'], 1)
        self.assertEqual(len(self.compiled_rows(intent['cardSha'])), 1)
        self.assertEqual(after['member'][0], intent['cardSha'])
        self.assertNotEqual(after['head'], before['head'])  # the stamped card was published
        self.assertTrue(self.store_verify())

    def test_recall_is_identical_before_and_after_consumption(self):
        path, _intent = self.verified_intent()
        self.hold_and_settle(path)
        before = {host: self.recall(host) for host in ('claude', 'codex')}
        for host, response in before.items():
            context = response['hookSpecificOutput']['additionalContext']
            self.assertIn('heron', context.lower(), (host, response))
        result = self.sync()
        self.assertEqual(result.get('provenance', {}).get('consumed'), 1, result)
        after = {host: self.recall(host) for host in ('claude', 'codex')}
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
