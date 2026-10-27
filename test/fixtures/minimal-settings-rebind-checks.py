#!/usr/bin/env python3
"""Minimal-settings migration of a selected Laya route, with crash recovery.

A real-shaped selected install in a temp HOME: a v2 route record, the private
reviewed route, an install journal (sqlite) activated on that route, a sealed
production attestation, a fresh-wiki selection ledger and marker, and real
Laya index/claim-queue databases. Only bundle identity reads are patched (no
plugin cache is built) and the per-user provider lane is replaced so no file
outside the temp HOME is touched.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager, nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import config  # noqa: E402
import laya_active_upgrade as active  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_qmd_cutover as qmd  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_inactive_upgrade as inactive  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_settings_rebind as rebind  # noqa: E402
import laya_wiki_discovery as discovery  # noqa: E402
import resolve_paths  # noqa: E402
import settings_migration  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_claim_queue import ClaimQueue  # noqa: E402
from laya_product_bundle import (BundleEvidenceError,  # noqa: E402
                                 PRODUCTION_ATTESTATION_SCHEMA)


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def vector_profile():
    model, fingerprint, dimension = 'synthetic-model', 'a' * 64, 3
    identity = json.dumps([model, fingerprint, dimension, 'cosine'], sort_keys=True,
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'profileId': sha(identity), 'modelId': model,
            'embeddingFingerprint': fingerprint, 'dimension': dimension,
            'metric': 'cosine', 'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': 'core/synthetic_provider.py'}


def legacy_settings(raw_name, wiki_name, raw_path='.', name='project'):
    """The live legacy shape whose effective identity minimal settings derive."""
    return {
        'indexing': True, 'name': name,
        'collections': [raw_name, wiki_name],
        'collectionPaths': {raw_name: raw_path, wiki_name: '.auto-context/wiki'},
        'collectionRoles': {raw_name: 'raw', wiki_name: 'wiki'},
        'skipPaths': ['build'], 'recallStrategy': 'hierarchical',
        'compile': {'mode': 'auto-wiki', 'triggers': ['post_tool_source', 'manual'],
                    'extractor': {'builtins': ['claude']}},
    }


class SelectedProject(unittest.TestCase):
    RAW, WIKI = 'project', 'project-wiki'
    RAW_PATH, NAME = '.', 'project'
    ORIGIN = 'fresh'

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='minimal-rebind-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.home / 'project'
        self.auto = self.project / '.auto-context'
        (self.auto / 'wiki').mkdir(parents=True)
        (self.project / self.RAW_PATH).mkdir(parents=True, exist_ok=True)
        self.settings = self.auto / 'settings.json'
        self.legacy = (json.dumps(legacy_settings(self.RAW, self.WIKI, self.RAW_PATH,
                                                  self.NAME), indent=2,
                                  ensure_ascii=False) + '\n').encode()
        self.settings.write_bytes(self.legacy)
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.plugin = self.home / 'installed-laya'
        self.plugin.mkdir()
        self.user_qmd = self.home / 'user-qmd'
        self.user_qmd.mkdir()
        self.journal = self.state / 'install-journal.sqlite'
        self.db, self.queue = self.state / 'index.sqlite', self.state / 'claims.sqlite'
        with closing(LayaIndexA(self.db, create=True)) as index:
            self.incarnation = index.incarnation()
        with closing(ClaimQueue.create(self.queue, index_incarnation=self.incarnation)):
            pass
        for name in ('catalog.sqlite', 'binding.sqlite'):
            (self.state / name).write_bytes(b'')
        self.proof = {'status': 'exact_commit_full_plugin_candidate_consistent',
                      'pluginRoot': str(self.plugin), 'sourceCommit': 'c' * 40,
                      'bundleManifestSha256': 'd' * 64,
                      'ownershipManifestSha256': 'e' * 64,
                      'vectorProfile': vector_profile()}
        self.route = {'schema': host_route.SCHEMA_V2, 'status': 'active',
                      'projectRoot': str(self.project),
                      'settingsSha256': sha(self.legacy),
                      'dbPath': str(self.db), 'queuePath': str(self.queue),
                      'dbIncarnation': self.incarnation, 'scopeId': '6' * 32,
                      'collections': self.collections(),
                      'pluginRoot': str(self.plugin), 'journalPath': str(self.journal),
                      'installId': '3' * 32, 'generation': 'g-' + '4' * 32,
                      'bundleManifestSha256': 'd' * 64,
                      'ownershipManifestSha256': 'e' * 64,
                      'vectorProfile': vector_profile()}
        self.route_raw = canon(self.route)
        self.private_write(self.auto / host_route.RECORD, self.route_raw)
        self.private_write(self.state / 'reviewed-route.json', self.route_raw)
        attestation = {'schema': PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
                       'bundleManifestSha256': 'd' * 64,
                       'ownershipManifestSha256': 'e' * 64,
                       'journalPath': str(self.journal), 'vectorProfile': vector_profile(),
                       'providerManifest': {'providerPath': str(self.plugin / 'p.py')},
                       'dependencyClosure': []}
        att_raw = journal_mod._canon(attestation).encode('utf-8')
        self.private_write(self.state / journal_mod.PRODUCTION_ATTESTATION, att_raw)
        identity_rows = [[str(self.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        with mock.patch.object(journal_mod, 'inspect_full_plugin_candidate',
                               return_value=self.proof):
            created = journal_mod.create_journal(self.journal, self.proof, self.route_raw,
                                                 user_qmd_roots=[str(self.user_qmd)])
        with mock.patch.object(journal_mod, '_inspect_identity', return_value=self.proof), \
                mock.patch.object(journal_mod, '_bind_attestation',
                                  return_value=(sha(att_raw), identity_rows)):
            selected = journal_mod.cas_activate(self.journal, created['epoch'],
                                                self.route_raw)
        self.assertEqual(selected['state'], 'active')
        identity = lambda path: [path.stat().st_dev, path.stat().st_ino]
        self.ledger = {
            'schema': fresh.SCHEMA, 'epoch': 3, 'phase': 'active',
            'projectRoot': str(self.project), 'journalPath': str(self.journal),
            'routeSha256': sha(self.route_raw), 'collections': self.collections(),
            'candidateDb': str(self.db), 'queueDb': str(self.queue),
            'catalogDb': str(self.state / 'catalog.sqlite'),
            'bindingStore': str(self.state / 'binding.sqlite'),
            'scope': '6' * 32, 'indexGeneration': 'g-' + '7' * 32 + '-b' + '8' * 64,
            'sourceManifest': '9' * 64, 'bindingDigest': '8' * 64,
            'catalogScanId': 'a' * 32, 'catalogRawDigest': 'b' * 64,
            'vectorProfileId': vector_profile()['profileId'], 'vectorRows': 1,
            'candidateIncarnation': self.incarnation,
            'attestationSha256': sha(att_raw),
            'providerFileIdentitySha256': selected['providerFileIdentitySha256'],
            'candidateIdentity': identity(self.db), 'queueIdentity': identity(self.queue),
            'catalogIdentity': identity(self.state / 'catalog.sqlite'),
            'bindingStoreIdentity': identity(self.state / 'binding.sqlite')}
        if self.ORIGIN == 'fresh':
            self.ledger_module, self.ledger_name = fresh, fresh.LEDGER
        else:
            # A QMD-origin (v3) selection: it also pins the QMD source and backup.
            for name in ('source.sqlite', 'backup.sqlite'):
                (self.state / name).write_bytes(b'')
            self.ledger = {**self.ledger, 'schema': qmd.SCHEMA_V3, 'sourceDigest': 'c' * 64,
                           'sourceDb': str(self.state / 'source.sqlite'),
                           'sourceFormat': 'qmd-2.5.3',
                           'sourceIdentity': identity(self.state / 'source.sqlite'),
                           'backupDb': str(self.state / 'backup.sqlite'),
                           'backupIdentity': identity(self.state / 'backup.sqlite')}
            qmd._validated(dict(self.ledger), self.state / qmd.LEDGER)
            self.ledger_module, self.ledger_name = qmd, qmd.LEDGER
        self.ledger_module._write(self.state / self.ledger_name, self.ledger, replace=False)
        self.ledger_module._write(self.auto / self.ledger_module.MARKER,
                                  self.ledger_module._marker(self.ledger), replace=False)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(rebind, '_provider_lane', nullcontext))

    def load_ledger(self):
        return rebind._Ledger(self.ORIGIN).load(self.state / self.ledger_name)

    def collections(self):
        raw_root = (self.project / self.RAW_PATH).resolve()
        return [{'name': self.RAW, 'role': 'raw', 'sourceRoot': str(raw_root)},
                {'name': self.WIKI, 'role': 'wiki',
                 'sourceRoot': str(self.auto / 'wiki')}]

    @staticmethod
    def private_write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def decision(self):
        return host_route.select_route(str(self.project), action='recall')

    def assert_selected_and_readable(self):
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        return decision, proof

    def serve(self, action, decision, payload):
        calls = []

        def fake_recall(host, served, payload, proof, recheck, *, degraded=False):
            self.assertEqual(recheck(served), proof)
            calls.append({'state': served['state'], 'degraded': degraded,
                          'readOnly': proof.get('readOnlyAllowed')})
            return {'served': True}
        with mock.patch.object(response, '_serve_recall', fake_recall):
            out = response.serve_selected(action, 'claude', decision, payload)
        return out, calls

    def crash_after(self, step_name):
        """Run the migration and fail right after one rebind step."""
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        steps = rebind._file_steps

        def interrupted(intent, target):
            built = steps(intent, target)
            original = built[step_name]

            def then_crash():
                original()
                raise RuntimeError('synthetic_crash')
            return {**built, step_name: then_crash}
        with mock.patch.object(rebind, '_file_steps', interrupted):
            failed = settings_migration.apply(str(self.project),
                                              proposal['proposalSha256'])
        self.assertEqual(failed['reason'], 'settings_rebind_step_failed', failed)
        return proposal


class RebindChecks(SelectedProject):
    def test_selected_route_migrates_and_stays_searchable(self):
        self.assert_selected_and_readable()
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        self.assertEqual(proposal['rebind']['origin'], self.ORIGIN)
        self.assertEqual(proposal['rebind']['staleQmdBindings'], [])
        self.assertEqual(sorted(proposal['rebind']['routeDecisionChanges']),
                         ['recallSettings'])
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'name': 'project', 'skipPaths': ['build']})
        self.assertEqual(Path(done['backupPath']).read_bytes(), self.legacy)
        decision, _proof = self.assert_selected_and_readable()
        new_route = (self.auto / host_route.RECORD).read_bytes()
        self.assertEqual(json.loads(new_route)['settingsSha256'],
                         sha(self.settings.read_bytes()))
        self.assertEqual((self.state / 'reviewed-route.json').read_bytes(), new_route)
        row = self.load_ledger()
        self.assertEqual((row['routeSha256'], row['phase'], row['epoch']),
                         (sha(new_route), 'active', 4))
        self.assertEqual(journal_mod.inspect_journal(self.journal)['selectedRouteSha256'],
                         sha(new_route))
        self.assertFalse(rebind.intent_path(self.state).exists())
        self.assertTrue(Path(done['rebind']['receiptPath']).is_file())
        self.assertEqual(settings_migration.plan(str(self.project))['status'],
                         'already_minimal')
        # Recall: the full proof fails only on the (unbuilt) bundle here, so
        # the #57 drift path serves the published generation read-only.
        out, calls = self.serve('recall', decision, {'prompt': 'alpha', 'cwd': str(self.project)})
        self.assertEqual(out, {'served': True})
        self.assertEqual(calls, [{'state': 'candidate', 'degraded': True, 'readOnly': True}])
        # A later automatic bundle upgrade still sees one consistent selection.
        with mock.patch.object(active, 'inspect_full_plugin_candidate',
                               side_effect=BundleEvidenceError('gone')):
            old = active._old_state(self.project, self.state, self.journal,
                                    active._Ledger(self.ORIGIN))
        self.assertEqual(old[2], new_route)

    def test_crash_between_steps_serves_read_only_then_resumes(self):
        proposal = self.crash_after('route')
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        pending = settings_migration.plan(str(self.project))
        self.assertEqual(pending['status'], 'rebind_pending', pending)
        self.assertEqual(pending['proposalSha256'], proposal['proposalSha256'])
        decision = self.decision()
        self.assertEqual(decision['state'], 'rebinding', decision)
        self.assertEqual(host_route._select_route(str(self.project))['state'], 'blocked')
        printed = subprocess.run([sys.executable, '-B', str(CORE / 'laya_index_host_route.py'),
                                  'classify', 'recall', str(self.project)],
                                 capture_output=True, text=True, timeout=60,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(printed.stdout.strip(), 'rebinding', printed.stderr)
        out, calls = self.serve('recall', decision, {'prompt': 'alpha', 'cwd': str(self.project)})
        self.assertEqual(calls, [{'state': 'rebinding', 'degraded': True, 'readOnly': True}])
        notice, _ = self.serve('update', decision, {'hook_event_name': 'SessionStart',
                                                    'cwd': str(self.project)})
        self.assertIn('settings rebind', notice['hookSpecificOutput']['additionalContext'])
        # The writer-side selected checks still refuse a half-rebound route.
        with self.assertRaises(Exception):
            journal_mod.verify_selected_route(decision)
        refused = settings_migration.apply(str(self.project), 'f' * 64)
        self.assertEqual(refused['reason'], 'proposal_acceptance_required', refused)
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'name': 'project', 'skipPaths': ['build']})
        self.assert_selected_and_readable()

    def test_each_interrupted_step_keeps_recall_and_resumes(self):
        for step in rebind.FORWARD:
            with self.subTest(step=step):
                self.setUp()
                self.crash_after(step)
                decision = self.decision()
                self.assertIn(decision['state'], ('rebinding', 'candidate'), decision)
                _out, calls = self.serve('recall', decision,
                                         {'prompt': 'alpha', 'cwd': str(self.project)})
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]['readOnly'])
                pending = settings_migration.plan(str(self.project))
                done = settings_migration.apply(str(self.project),
                                                pending['proposalSha256'])
                self.assertEqual(done['status'], 'migrated', done)
                self.assert_selected_and_readable()

    def test_rollback_restores_previous_settings_and_route(self):
        proposal = self.crash_after('ledger')
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'],
                                        rollback=True)
        self.assertEqual(done['status'], 'rolled_back', done)
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        self.assertEqual((self.auto / host_route.RECORD).read_bytes(), self.route_raw)
        self.assertEqual(self.load_ledger()['routeSha256'],
                         sha(self.route_raw))
        self.assert_selected_and_readable()
        self.assertEqual(settings_migration.plan(str(self.project))['status'],
                         'review_required')

    def test_unknown_state_during_rebind_stays_blocked(self):
        self.crash_after('route')
        self.private_write(self.state / 'reviewed-route.json', b'{}')
        decision = self.decision()
        self.assertEqual(decision['state'], 'rebinding')
        with self.assertRaises(rebind.RebindRefused):
            rebind.verify_read_only(decision)
        (self.settings).write_bytes(b'{"name": "other"}\n')
        self.assertEqual(self.decision()['state'], 'blocked')

    def active_upgrade_intent(self, old_route_sha):
        """A finished bundle-upgrade receipt naming the current route as new."""
        route = self.route_raw.decode()
        return {'schema': 'laya-active-upgrade-intent-v1',
                'projectRoot': str(self.project), 'stateRoot': str(self.state),
                'oldRecord': {'identity': {'projectRoot': str(self.project),
                                           'stateRoot': str(self.state)}},
                'oldLedger': {**self.ledger, 'routeSha256': old_route_sha},
                'newLedger': {**self.ledger, 'phase': 'upgrading'},
                'oldRoute': route, 'newRoute': route, 'oldAttestation': '{}',
                'newAttestation': '{}', 'oldMarker': '{}', 'newMarker': '{}',
                'backup': str(self.state / 'backup'),
                **({'origin': 'qmd'} if self.ORIGIN == 'qmd' else {})}

    def test_finished_bundle_upgrade_receipt_is_settled_first(self):
        path = self.state / active.INTENT
        self.private_write(path, canon(self.active_upgrade_intent('f' * 64)))
        proposal = settings_migration.plan(str(self.project))
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        self.assertFalse(path.exists())
        self.assertEqual(len(list(self.state.glob('active-upgrade-*-settled.json'))), 1)

    def test_unsettled_bundle_upgrade_refuses_before_any_write(self):
        path = self.state / active.INTENT
        self.private_write(path, b'{"schema":"unknown"}')
        proposal = settings_migration.plan(str(self.project))
        refused = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(refused['reason'], 'active_upgrade_unsettled', refused)
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        self.assertEqual(list(self.auto.glob('settings.json.pre-minimal-*')), [])
        self.assertFalse(rebind.intent_path(self.state).exists())
        self.assert_selected_and_readable()

    def test_stale_qmd_setup_journal_is_reported_not_rewritten(self):
        journal = self.auto / 'install-update-journal.json'
        journal.write_text(json.dumps({'schema': 'qmd-install-update-v1',
                                       'phase': 'activated'}))
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['rebind']['staleQmdBindings'],
                         ['install-update-journal.json'], proposal)
        journal.write_text(json.dumps({'schema': 'qmd-install-update-v1',
                                       'phase': 'preparing'}))
        refused = settings_migration.plan(str(self.project))
        self.assertEqual(refused['reason'], 'settings_bound_by_unfinished_setup_journal')


class QmdOriginRebindChecks(RebindChecks):
    """The same migration on a QMD-origin (v3 ledger) selected route."""
    ORIGIN = 'qmd'


class IdentityChangeChecks(SelectedProject):
    RAW, WIKI = 'notes', 'notes-wiki'

    def test_selected_route_with_other_collection_names_is_refused(self):
        before = self.settings.read_bytes()
        self.assert_selected_and_readable()
        result = settings_migration.plan(str(self.project))
        self.assertEqual(result['reason'], 'bound_collection_identity_changed', result)
        self.assertEqual({row['collection'] for row in result['identityDiff']},
                         {'notes', 'notes-wiki', 'project', 'project-wiki'})
        applied = settings_migration.apply(str(self.project), None)
        self.assertEqual(applied['reason'], 'bound_collection_identity_changed')
        self.assertEqual(self.settings.read_bytes(), before)
        self.assert_selected_and_readable()


class KoreanNameChecks(SelectedProject):
    """A novel whose legacy ``name`` is a Korean title; ``--name`` keeps the slug."""
    RAW, WIKI, NAME = 'after-last-train', 'after-last-train-wiki', '막차 뒤의 피난소'

    def test_name_override_migrates_with_unchanged_identity(self):
        self.assertEqual(settings_migration.plan(str(self.project))['reason'],
                         'name_required')
        self.assertEqual(settings_migration.plan(str(self.project), '막차')['reason'],
                         'name_override_invalid')
        wrong = settings_migration.plan(str(self.project), 'last-train')
        self.assertEqual(wrong['reason'], 'name_override_mismatch', wrong)
        self.assertEqual(wrong['derivedCollections'], ['last-train', 'last-train-wiki'])
        proposal = settings_migration.plan(str(self.project), 'after-last-train')
        self.assertEqual(proposal['status'], 'review_required', proposal)
        self.assertEqual(proposal['minimalSettings'],
                         {'name': 'after-last-train', 'skipPaths': ['build']})
        self.assertIn('name', proposal['changedFields'])
        self.assertNotIn('rawCollection', proposal['rebind'])
        # The proposal binds the override: apply without it refuses unchanged.
        refused = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(refused['reason'], 'name_required', refused)
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'],
                                        name='after-last-train')
        self.assertEqual(done['status'], 'migrated', done)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'name': 'after-last-train', 'skipPaths': ['build']})
        decision, _proof = self.assert_selected_and_readable()
        self.assertEqual(decision['collections'], self.collections())

    def test_setup_entry_plan_accepts_name(self):
        printed = subprocess.run(
            [sys.executable, '-B', str(CORE / 'setup_entry.py'), 'plan-minimal-settings',
             '--project', str(self.project), '--name', 'after-last-train'],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        result = json.loads(printed.stdout)
        self.assertEqual(result['status'], 'review_required', printed.stderr)
        self.assertEqual(result['minimalSettings']['name'], 'after-last-train')
        self.assertEqual(self.settings.read_bytes(), self.legacy)


def card(project, collection, rel):
    """A verified auto card citing one raw file revision."""
    path = project / rel
    data = path.read_bytes()
    return ('---\ntitle: "card"\ntype: concept\nstatus: verified\n'
            'createdBy: qmd-auto-context\nsources:\n'
            '  - {kind: "file", path: "%s", collection: "%s"}\n'
            'sourceRevisions:\n'
            '  - {kind: "file", path: "%s", collection: "%s", sha256: "%s", '
            'size: %d, mtimeNs: %d}\n---\n\nA fact.\n'
            % (rel, collection, rel, collection, sha(data), len(data),
               path.stat().st_mtime_ns))


class RawMove(SelectedProject):
    """A selected QMD-origin route whose raw collection is a subfolder (rccar-like)."""
    RAW, RAW_PATH = 'project-docs', 'docs'
    ORIGIN = 'qmd'
    BASE = 'g-' + '4' * 32

    def setUp(self):
        super().setUp()
        (self.project / 'docs' / 'a.md').write_text('# a\n')
        (self.project / 'docs' / 'b.md').write_text('# b\n')
        (self.project / 'notes').mkdir(exist_ok=True)
        (self.project / 'notes' / 'c.md').write_text('# c\n')
        (self.project / 'README.md').write_text('# readme\n')
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        self.cursor = discovery._paths(decision)[0]
        old = self.RAW + '/'
        discovery._write(self.cursor, discovery._state_doc(
            decision, self.BASE,
            {old + 'a.md': sha((self.project / 'docs' / 'a.md').read_bytes()),
             old + 'b.md': 'f' * 64},
            {old + 'gone.md': 'e' * 64}, ['1' * 32]))
        self.old_cursor = self.cursor.read_bytes()

    def new_raw(self):
        return 'project'

    def drain(self, decision):
        """Discovery on the rebound route; the published wiki generation is unchanged."""
        with mock.patch.object(discovery, '_read_view',
                               return_value=(self.BASE, self.BASE, [], [])):
            return discovery.drain_after_sync(decision, {'grants': {}, 'provisional': {}},
                                              recheck=lambda: None)


class RawMoveChecks(RawMove):
    def test_raw_rename_and_root_expansion_migrates(self):
        before = self.decision()
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        moved = proposal['rebind']['rawCollection']
        self.assertEqual((moved['from']['name'], moved['to']['name']),
                         (self.RAW, self.new_raw()))
        self.assertEqual(moved['to']['sourceRoot'], str(self.project))
        self.assertEqual(proposal['rebind']['forfeits'],
                         ['qmd_origin_restore']
                         if self.ORIGIN == 'qmd' and self.RAW != self.new_raw() else [])
        self.assertTrue({'collections', 'collectionRoots'} <=
                        set(proposal['rebind']['routeDecisionChanges']))
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        decision, _proof = self.assert_selected_and_readable()
        wiki = [row for row in decision['collections'] if row['role'] == 'wiki']
        self.assertEqual(wiki, [row for row in before['collections'] if row['role'] == 'wiki'])
        self.assertEqual(decision['collectionRoots'][self.new_raw()], str(self.project))
        route = json.loads((self.auto / host_route.RECORD).read_bytes())
        self.assertEqual(route['collections'], decision['collections'])
        self.assertEqual(self.load_ledger()['collections'], decision['collections'])
        self.assertEqual((self.state / 'reviewed-route.json').read_bytes(),
                         (self.auto / host_route.RECORD).read_bytes())
        # The cursor moved with the raw collection: old snapshot and pending
        # keys re-keyed under docs/, the rest of the project as baseline.
        state = json.loads(self.cursor.read_text())
        new = self.new_raw() + '/'
        self.assertEqual(state['rawCollections'], [self.new_raw()])
        self.assertEqual(state['pending'], {new + 'docs/gone.md': 'e' * 64})
        self.assertEqual(state['rawSnapshot'][new + 'docs/b.md'], 'f' * 64)
        self.assertEqual(state['rawSnapshot'][new + 'README.md'],
                         sha((self.project / 'README.md').read_bytes()))
        self.assertIn(new + 'notes/c.md', state['rawSnapshot'])
        self.assertEqual((state['baseGen'], state['spentGrants']), (self.BASE, ['1' * 32]))
        # Raw discovery works on the new root: the stale b.md, the deleted
        # gone.md, and edits inside and outside the old root after migration.
        (self.project / 'docs' / 'new.md').write_text('# new\n')
        (self.project / 'notes' / 'c.md').write_text('# c changed\n')
        out = self.drain(decision)
        self.assertEqual((out['state'], out['reason']), ('pending', 'no_accepted_grant'), out)
        self.assertEqual(sorted(json.loads(self.cursor.read_text())['pending']),
                         [new + 'docs/b.md', new + 'docs/gone.md', new + 'docs/new.md',
                          new + 'notes/c.md'])
        # Recall verifies a card citing the new raw collection anywhere in the project.
        row = {'path': self.WIKI + '/c.md', 'body': card(self.project, self.new_raw(),
                                                         'README.md')}
        self.assertTrue(response._verified_wiki_row(row, decision))
        self.assertFalse(response._verified_wiki_row(row, before))
        out, calls = self.serve('recall', decision, {'prompt': 'alpha',
                                                     'cwd': str(self.project)})
        self.assertEqual(calls, [{'state': 'candidate', 'degraded': True, 'readOnly': True}])
        self.assertEqual(settings_migration.plan(str(self.project))['status'],
                         'already_minimal')

    def test_card_citing_old_raw_name_refuses(self):
        (self.auto / 'wiki' / 'a.md').write_text(card(self.project, self.RAW, 'docs/a.md'))
        compile_dir = self.auto / 'compile'
        compile_dir.mkdir()
        (compile_dir / 'source-queue.jsonl').write_text(
            json.dumps({'source': {'collection': self.RAW, 'path': 'docs/b.md'}}) + '\n')
        result = settings_migration.plan(str(self.project))
        self.assertEqual(result['reason'], 'raw_collection_bound_by_wiki_state', result)
        self.assertEqual((result['rawCollection'], result['citingCards'],
                          result['citingCardSample'], result['queuedRecords']),
                         (self.RAW, 1, ['a.md'], 1))
        applied = settings_migration.apply(str(self.project), 'f' * 64)
        self.assertEqual(applied['reason'], 'raw_collection_bound_by_wiki_state')
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        self.assertEqual(self.cursor.read_bytes(), self.old_cursor)
        self.assert_selected_and_readable()

    def crash_and_check(self, step):
        proposal = self.crash_after(step)
        decision = self.decision()
        self.assertIn(decision['state'], ('rebinding', 'candidate'), decision)
        _out, calls = self.serve('recall', decision, {'prompt': 'alpha',
                                                      'cwd': str(self.project)})
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['readOnly'])
        pending = settings_migration.plan(str(self.project))
        self.assertEqual(pending['status'], 'rebind_pending', pending)
        return proposal

    def test_each_interrupted_step_resumes(self):
        for step in rebind.FORWARD:
            with self.subTest(step=step):
                self.setUp()
                proposal = self.crash_and_check(step)
                done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
                self.assertEqual(done['status'], 'migrated', done)
                decision, _proof = self.assert_selected_and_readable()
                self.assertEqual(json.loads(self.cursor.read_text())['rawCollections'],
                                 [self.new_raw()])
                self.assertEqual(self.drain(decision)['state'], 'pending')

    def test_each_interrupted_step_rolls_back(self):
        for step in rebind.FORWARD:
            with self.subTest(step=step):
                self.setUp()
                proposal = self.crash_and_check(step)
                done = settings_migration.apply(str(self.project), proposal['proposalSha256'],
                                                rollback=True)
                self.assertEqual(done['status'], 'rolled_back', done)
                self.assertEqual(self.settings.read_bytes(), self.legacy)
                self.assertEqual((self.auto / host_route.RECORD).read_bytes(), self.route_raw)
                self.assertEqual(self.cursor.read_bytes(), self.old_cursor)
                self.assertEqual(self.load_ledger()['collections'], self.collections())
                decision, _proof = self.assert_selected_and_readable()
                self.assertEqual(self.drain(decision)['state'], 'pending')
                self.assertEqual(settings_migration.plan(str(self.project))['status'],
                                 'review_required')

    def test_changed_cursor_during_rebind_stops_the_step(self):
        proposal = self.crash_after('journal')
        state = json.loads(self.cursor.read_text())
        discovery._write(self.cursor, {**state, 'spentGrants': []})
        failed = settings_migration.apply(str(self.project), proposal['proposalSha256'],
                                          rollback=True)
        self.assertEqual(failed['reason'], 'settings_rebind_discovery_changed', failed)
        self.assertEqual(settings_migration.plan(str(self.project))['status'],
                         'rebind_pending')


    def test_cursor_changed_before_its_step_rolls_back(self):
        proposal = settings_migration.plan(str(self.project))
        steps = rebind._file_steps

        def drained_first(intent, target):
            built = steps(intent, target)
            original = built['discovery']

            def then():
                if target == 'new':
                    state = json.loads(self.cursor.read_text())
                    discovery._write(self.cursor, {**state, 'spentGrants': []})
                original()
            return {**built, 'discovery': then}
        with mock.patch.object(rebind, '_file_steps', drained_first):
            failed = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(failed['reason'], 'settings_rebind_discovery_changed', failed)
        changed = self.cursor.read_bytes()
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'],
                                        rollback=True)
        self.assertEqual(done['status'], 'rolled_back', done)
        self.assertEqual(self.cursor.read_bytes(), changed)
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        self.assert_selected_and_readable()


class RawRootOnlyChecks(RawMove):
    """Same raw name, root expanded from docs/ to the project: cards keep resolving."""
    RAW = 'project'

    def test_root_expansion_keeps_existing_cards(self):
        row = {'path': self.WIKI + '/a.md', 'body': card(self.project, self.RAW, 'docs/a.md')}
        (self.auto / 'wiki' / 'a.md').write_text(row['body'])
        self.assertTrue(response._verified_wiki_row(row, self.decision()))
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        self.assertEqual(proposal['rebind']['forfeits'], [])
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        decision, _proof = self.assert_selected_and_readable()
        self.assertTrue(response._verified_wiki_row(row, decision))
        self.assertEqual(self.drain(decision)['state'], 'pending')


class FreshRawRenameChecks(RawMoveChecks):
    """A fresh-wiki selection (raw is the project root) whose raw name changes."""
    RAW, RAW_PATH = 'notes-raw', '.'
    ORIGIN = 'fresh'

    def setUp(self):
        SelectedProject.setUp(self)
        (self.project / 'docs').mkdir()
        for rel in ('docs/a.md', 'docs/b.md', 'README.md'):
            (self.project / rel).write_text('# %s\n' % rel)
        (self.project / 'notes').mkdir()
        (self.project / 'notes' / 'c.md').write_text('# c\n')
        decision = self.decision()
        self.cursor = discovery._paths(decision)[0]
        old = self.RAW + '/'
        discovery._write(self.cursor, discovery._state_doc(
            decision, self.BASE,
            {old + 'docs/a.md': sha((self.project / 'docs' / 'a.md').read_bytes()),
             old + 'docs/b.md': 'f' * 64,
             old + 'README.md': sha((self.project / 'README.md').read_bytes()),
             old + 'notes/c.md': sha((self.project / 'notes' / 'c.md').read_bytes())},
            {old + 'docs/gone.md': 'e' * 64}, ['1' * 32]))
        self.old_cursor = self.cursor.read_bytes()


class WikiIdentityMoveChecks(RawMove):
    """Raw and wiki both differ from the derived identity: still refused."""
    WIKI = 'project-docs-wiki'

    def test_wiki_change_is_refused(self):
        result = settings_migration.plan(str(self.project))
        self.assertEqual(result['reason'], 'bound_collection_identity_changed', result)
        self.assertIn('project-docs-wiki', {row.get('collection')
                                            for row in result['identityDiff']})
        self.assertEqual(settings_migration.apply(str(self.project), None)['reason'],
                         'bound_collection_identity_changed')
        self.assertEqual(self.settings.read_bytes(), self.legacy)
        self.assertEqual(self.cursor.read_bytes(), self.old_cursor)


class MinimalQmdIndexChecks(unittest.TestCase):
    def setUp(self):
        # Temp roots live under /private, which resolve_paths treats as risky.
        patcher = mock.patch.object(resolve_paths, 'is_risky_path', lambda _path: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_minimal_raw_root_is_never_a_qmd_index_entry(self):
        with tempfile.TemporaryDirectory(prefix='minimal-qmd-') as temp:
            root = Path(temp).resolve()
            (root / '.auto-context' / 'wiki').mkdir(parents=True)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(minimal))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])
            self.assertEqual([row['name'] for row in resolved['sourceEntries']], ['project'])
            self.assertEqual(len(resolved['entries']), 2)
            legacy = config.normalize_config(legacy_settings('project', 'project-wiki'))
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(legacy))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project', 'project-wiki'])
            (root / '.auto-context' / 'settings.json').write_text(
                json.dumps({'name': 'project', 'skipPaths': []}))
            with mock.patch.dict(os.environ, {'HOME': str(root.parent)}):
                raw = config.load_project_config_raw(str(root))
            self.assertEqual(raw['settingsModel'], 'minimal')
            self.assertEqual(raw['collectionRoles'], {'project': 'raw', 'project-wiki': 'wiki'})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(raw))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])

    def test_shared_registry_drops_index_flag_only_for_minimal_raw(self):
        import qmd_shared_store as store
        with tempfile.TemporaryDirectory(prefix='minimal-registry-') as temp:
            base = Path(temp).resolve() / 'store'
            base.mkdir(mode=0o700)
            row = {'scope': '/p', 'path': '/p', 'indexed': True}
            registry = base / 'collections.json'
            registry.write_text(json.dumps({'project': row}))
            registry.chmod(0o600)
            demoted = {'project': {**row, 'indexed': False}}
            # An explicit role edit (no minimal settings) still needs reconciliation.
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, demoted)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            self.assertEqual(store.minimal_raw_names(minimal), {'project'})
            self.assertEqual(store.minimal_raw_names(
                config.normalize_config(legacy_settings('project', 'project-wiki'))), set())
            store._reserve_owners(base, demoted, demoted=store.minimal_raw_names(minimal))
            self.assertIs(json.loads(registry.read_text())['project']['indexed'], False)
            moved = {'project': {**row, 'path': '/q', 'indexed': False}}
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, moved, demoted={'project'})


if __name__ == '__main__':
    unittest.main()
