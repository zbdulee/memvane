#!/usr/bin/env python3
"""DB provenance end to end on a selected 1.0 install (store format 4).

A temporary HOME holds one project onboarded through the 1.0 setup use cases
(plan, prepare, initial build, finalize, activate). A raw note is compiled
into a generated card with observed source fingerprints, the verify worker
judges it (synthetic engine), leaves one compile intent and stamps the card,
and the selected sync pass consumes the intent into a ``compiled``
attestation. Every check counts store rows, journal epochs and intent files;
none reads a wall clock.

Replaced only where a host install or a model is needed: the provider review,
the runtime file seal, the per-file provider identity proof, the build/sync
provider (one fixed vector per chunk), the warm-service start, the priority
probes that read the account wake queue, the query admission (recall stays
lexical), and the verifier engine plan and extractor call. Nothing outside
the temporary HOME is written.

Run one method per process through ``wiki-db-provenance.test.mjs``.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INSTALL_ID = '4' * 32
SETTINGS_NAME = 'provenance'
MODEL = 'synthetic-precomputed-only'
FINGERPRINT = 'f' * 64
DIMENSION = 2
PROVIDER_ID = 'synthetic-provider'
PROVIDER_REL = 'memvane/provider/adapters/local_embed_provider.py'
REVIEW_SHA = '9' * 64
CARD_REL = 'concepts/heron-dawn.md'
ANCHOR_REL = 'concepts/anchor.md'
PROMPT = 'emerald heron'
NO_PROVENANCE = {'consumed': 0, 'rejected': {}, 'waiting': 0, 'superseded': 0, 'unsafe': 0}
# Initial build and settle passes are bounded; each is a full sync pass.
BUILD_PASSES = 8
SETTLE_PASSES = 6


class _Crash(BaseException):
    """Stands in for a process killed between the intent write and the stamp."""


class _Coordinator:
    root = None

    @contextmanager
    def activity(self, lane, policy, report):
        yield 1

    def check(self, policy, report):
        return {'ready': True}


class _Service:
    def stop(self):
        pass


def _start_service(manifest, *, admit=None):
    return _Service()


def _profile():
    from memvane.provider.api import profile_identity
    return {'profileId': profile_identity(MODEL, FINGERPRINT, DIMENSION), 'modelId': MODEL,
            'embeddingFingerprint': FINGERPRINT, 'dimension': DIMENSION, 'metric': 'cosine',
            'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': PROVIDER_REL}


class _FixtureProvider:
    """Build/sync provider: the view recipe's own first-pass chunks, one fixed vector each."""

    def __init__(self, manifest):
        self.manifest = dict(manifest)
        self.profile = {'profile_id': _profile()['profileId'], 'model_id': MODEL,
                        'embedding_fingerprint': FINGERPRINT, 'dimension': DIMENSION,
                        'metric': 'cosine'}

    def embed_document_chunks(self, body, path, *, timeout=120):
        from memvane.store.api import qmd_first_pass
        return [{'seq': seq, 'pos': pos, 'vector': [1.0, 0.0]}
                for seq, (pos, _text) in enumerate(qmd_first_pass(body))]

    def embed_document(self, body, title):
        return [1.0, 0.0]


def _provider_proof(record, profile, *, allow_local_unavailable=False):
    """The attested manifest as recorded; this host has no reviewed runtime files."""
    manifest = dict(record['providerManifest'])
    return (manifest, True) if allow_local_unavailable else manifest


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


class WikiDbProvenance(unittest.TestCase):
    # -- install -----------------------------------------------------------------
    def setUp(self):
        home = Path(os.environ.get('HOME', '')).resolve()
        account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if (home == account or account in home.parents
                or os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1'
                or not os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')):
            self.fail('run through wiki-db-provenance.test.mjs: temporary HOME and fixture guards required')
        from memvane.settings import config as settings_config
        self.home = home
        self.project = home / 'project'
        self.project.mkdir(mode=0o700)
        self.git('init', '-q')
        self.git('commit', '--allow-empty', '-qm', 'fixture')
        time.sleep(1.1)  # Git racy-index window before the first checkout capture.
        self.raw_name, _wiki_name = settings_config.minimal_collection_names(SETTINGS_NAME)
        folder = self.project / '.memvane'
        self.wiki = folder / 'wiki'
        (self.wiki / 'concepts').mkdir(parents=True)
        (folder / 'settings.json').write_text(
            json.dumps({'name': SETTINGS_NAME, 'skipPaths': []}) + '\n', encoding='utf-8')
        notes = self.project / 'notes'
        notes.mkdir()
        anchor_note = notes / 'anchor.md'
        anchor_note.write_text('The emerald heron nests beside the north pond.\n', encoding='utf-8')
        self.source = notes / 'heron-source.md'
        self.source.write_text('At dawn the emerald heron fishes in the shallow reeds.\n',
                               encoding='utf-8')
        self.write_anchor(anchor_note)
        self.card = self.wiki / CARD_REL
        self.state = home.joinpath('.local', 'state', 'memvane', 'v1', 'installs', INSTALL_ID)
        self.log = home / 'verify-log.jsonl'
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patch_host(stack)
        self.onboard()
        self.settle()

    def git(self, *args):
        base = ['git', '-C', str(self.project), '-c', 'init.defaultBranch=main',
                '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        subprocess.run(base + list(args), check=True, capture_output=True, timeout=30)

    def revision(self, path):
        from memvane.wiki.card import api as card
        snapshot = card.snapshot_bytes(path)
        self.assertIsNotNone(snapshot)
        return {'kind': 'file', 'path': path.relative_to(self.project).as_posix(),
                'collection': self.raw_name, **snapshot[0]}

    def write_anchor(self, note):
        from memvane.wiki.card import api as card
        line = card.dump_source_revisions([self.revision(note)])[0]
        text = ('---\ntitle: "Emerald heron"\nstatus: verified\ncreatedBy: memvane\n'
                'sourceRevisions:\n  - ' + line + '\n---\n# Emerald heron\n'
                'The emerald heron nests beside the north pond.\n')
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + card.card_body_hash(text) + '\n', 1)
        (self.wiki / ANCHOR_REL).write_text(text, encoding='utf-8')

    def patch_host(self, stack):
        from memvane.kernel.memvane_paths import install_socket
        from memvane.kernel.resource_deferred import ResourceDeferred
        manifest = {'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': 'b' * 64,
                    'providerRelativePath': PROVIDER_REL,
                    'recipeId': 'qmd-regex-token-chunks-candidate-v1', 'modelId': MODEL,
                    'modelSha256': 'a' * 64, 'embeddingFingerprint': FINGERPRINT,
                    'moduleEntryPath': str(self.home / 'fixture-module'),
                    'serviceSocketPath': str(install_socket(self.state)), 'dimension': DIMENSION}
        review = {'status': 'review_required', 'proposalSha256': REVIEW_SHA,
                  'reviewedProvider': {'vectorProfile': _profile(), 'providerManifest': manifest,
                                       'dependencyClosure': []}}
        verdict = {'verdict': 'pass', 'claims': [{'claim': 'The emerald heron fishes at dawn.'}],
                   'reasons': []}
        attempt = {'engine': 'claude', 'argv': ['synthetic-verifier'], 'mode': 'cross-engine',
                   'key': 'claude'}
        for target, value in (
                ('memvane.install.application.onboarding_workflow.review_local_provider', lambda **_kw: review),
                ('memvane.provider.embed_runtime.sealed_files_sha256', lambda *_a, **_k: 'c' * 64),
                ('memvane.journal.install_journal._v1_provider_manifest', _provider_proof),
                ('memvane.sync.product_sync_worker.ReviewedLocalQmdProvider', _FixtureProvider),
                ('memvane.sync.product_sync_worker._higher_priority_wake', lambda _controller: False),
                ('memvane.sync.product_sync_worker._initial_build_waiting',
                 lambda _controller, _project: False),
                ('memvane.wiki.verify.wiki_verify_worker.plan_verify_attempts',
                 lambda *_a, **_k: ([attempt], 'prefer', None)),
                ('memvane.wiki.engines.api.run_extractor', lambda *_a, **_k: (copy.deepcopy(verdict), None, 0)),
                ('memvane.wiki.verify.wiki_verify_worker.reindex_wiki', lambda *_a, **_k: None),
                ('memvane.wiki.verify.wiki_verify_worker.wake_pending_retirement',
                 lambda *_a, **_k: None)):
            stack.enter_context(mock.patch(target, value))
        stack.enter_context(mock.patch('memvane.resident.embed_resident.admit_query',
                                       side_effect=ResourceDeferred('fixture_lexical_recall')))

    def onboard(self):
        from memvane.install.application import onboarding_workflow as workflow
        from memvane.app import setup_activation as activation
        from memvane.journal.api import select_route
        plan = workflow.plan_fresh_wiki_origin(self.project)
        self.assertEqual(plan['status'], 'review_required', plan)
        prepared = workflow.prepare_fresh_wiki_origin(
            project_root=str(self.project), plugin_root=str(ROOT), state_root=str(self.state),
            expected_proposal_sha256=plan['proposalSha256'], install_id=INSTALL_ID,
            accepted_provider_sha256=REVIEW_SHA, dimension=DIMENSION)
        self.assertEqual(prepared['status'], 'initial_build_registered', prepared)
        results = []
        for _ in range(BUILD_PASSES):
            results.append(self.run_worker())
            if results[-1].get('buildPhase') == 'ready':
                break
        else:
            self.fail('initial build not ready: %r' % results)
        finalized = activation.finalize_initial_build(project_root=str(self.project),
                                                    state_root=str(self.state))
        self.assertEqual(finalized['status'], 'initial_build_finalized', finalized)
        from memvane.install import api as install
        route = (self.state / 'route.json').read_bytes()
        with install.install_lock(self.state):
            selected = install.activate_origin_selection(self.project, self.state / 'journal.sqlite',
                                                         route)
        self.assertEqual(selected['status'], 'selected', selected)
        self.decision = select_route(str(self.project), action='recall')
        self.assertEqual(self.decision.get('state'), 'candidate', self.decision)
        self.journal = Path(self.decision['journalPath'])
        self.store_path = Path(self.decision['storePath'])
        self.intents = self.journal.parent / 'provenance-intents'
        with self.db() as db:
            self.assertEqual(db.execute('SELECT schema_version FROM store_meta').fetchone()[0], 4)

    # -- passes ------------------------------------------------------------------
    def run_worker(self):
        from memvane.sync import product_sync_worker as worker
        return worker.run(self.project, coordinator=_Coordinator(), report={},
                          start_service=_start_service)

    def epoch(self):
        from memvane.journal.api import inspect_journal
        return inspect_journal(self.journal)['epoch']

    def sync(self):
        result = self.run_worker()
        self.assertIn(result.get('state'), ('indexed', 'unchanged'), result)
        return result

    def settle(self):
        """Run passes until one changes nothing: no journal fence, unchanged view."""
        results = []
        for _ in range(SETTLE_PASSES):
            before = self.epoch()
            results.append(self.run_worker())
            if results[-1].get('state') == 'unchanged' and self.epoch() == before:
                return results[-1]
        self.fail('sync passes did not settle: %r' % results)

    # -- compile and verify ----------------------------------------------------------
    def compile_card(self):
        """The compile step's outputs: a generated card and the job's observed fingerprints."""
        from memvane.wiki.card import api as card
        from memvane.wiki.compile import wiki_compile_worker as compiler
        from memvane.wiki.workspace import api as workspace
        checkout = workspace.schema3_checkout(workspace.selected(self.project))
        self.assertIsNotNone(checkout)
        capture = checkout.capture()
        revision = self.revision(self.source)
        observed = compiler.source_fingerprints(self.project, checkout, capture, [revision], time.time_ns())
        first = observed['fingerprints'][0]
        fingerprints = dict(observed, scanId=first['scan_id'], observedNs=first['observed_ns'])
        line = card.dump_source_revisions([revision])[0]
        self.card.write_text(
            '---\ntitle: "Heron at dawn"\nstatus: generated\ncreatedBy: memvane\n'
            'sourceRevisions:\n  - ' + line + '\n---\n# Heron at dawn\n'
            'At dawn the emerald heron fishes in the shallow reeds.\n', encoding='utf-8')
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
        return verifier.process_verify_job(self.project, config, {}, {}, copy.deepcopy(self.job), self.log)

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
    def db(self):
        from memvane.kernel.errors import _query_only_uri
        connection = sqlite3.connect(_query_only_uri(self.store_path), uri=True)
        connection.execute('PRAGMA query_only=ON')
        return closing(connection)

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
