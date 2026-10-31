#!/usr/bin/env python3
"""Fresh-wiki bundle upgrades on real bound/selected stores; no model or host.

A real inactive initial build is sealed with the fake provider of
``laya-initial-build-checks.py`` and bound to a real ``fresh-wiki-cutover.json``
ledger and project marker. Only the installer journal, the provider review and
the bundle inspection are synthetic; ledger, marker, route, record and stores
are the real modules writing temp files under a temporary HOME.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
BUILD = runpy.run_path(str(HERE / 'laya-initial-build-checks.py'))

import config  # noqa: E402
import laya_active_upgrade as active  # noqa: E402
import laya_bound_upgrade as bound_upgrade  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_initial_build as initial_build  # noqa: E402
import laya_product_install as product  # noqa: E402
import laya_product_sync_worker as worker  # noqa: E402
import laya_wiki_discovery as discovery  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_host_route import (SCHEMA_V2 as ROUTE_V2,  # noqa: E402
                                   settings_identity_digest)
from laya_install_journal import PRODUCTION_ATTESTATION, _canon  # noqa: E402
from laya_product_bundle import BundleEvidenceError  # noqa: E402

PROFILE_ID = BUILD['PROFILE_ID']
SCOPE = BUILD['SCOPE']
OLD_COMMIT, NEW_COMMIT = '1' * 40, '2' * 40
PROFILE = {'profileId': PROFILE_ID, 'modelId': 'synthetic-model',
           'embeddingFingerprint': 'a' * 64, 'dimension': 3, 'metric': 'cosine',
           'approvedProviderId': 'synthetic-provider',
           'providerArtifactSha256': 'b' * 64,
           'providerRelativePath': 'core/laya_local_embed_provider.py'}


class Kill(BaseException):
    """A synthetic process death between two durable writes."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest(root):
    return {'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': 'core/laya_local_embed_provider.py',
            'providerPath': root + '/core/laya_local_embed_provider.py',
            'workerPath': root + '/core/laya_local_embed_worker.mjs',
            'workerSha256': 'c' * 64, 'modelPath': '/models/m.gguf',
            'modelSha256': 'd' * 64, 'dimension': 3,
            'serviceSocketPath': '/tmp/synthetic-install/runtime/laya-embed.sock'}


def proof(root, commit, *, profile=PROFILE):
    tag = _sha((root + commit).encode())
    return {'status': 'exact_commit_full_plugin_candidate_consistent',
            'pluginRoot': root, 'sourceCommit': commit,
            'bundleManifestSha256': tag, 'ownershipManifestSha256': _sha(tag.encode()),
            'vectorProfile': profile}


def identity(item):
    return {'pluginRoot': item['pluginRoot'], 'installId': BUILD['INSTALL'],
            'generation': 'g-' + '9' * 32, 'sourceCommit': item['sourceCommit'],
            'bundleManifestSha256': item['bundleManifestSha256'],
            'ownershipManifestSha256': item['ownershipManifestSha256'],
            'bundleKind': 'full_plugin', 'vectorProfile': item['vectorProfile']}


# Plugin roots whose files a host re-extracted in place (new inode rows).
REEXTRACTED = set()


def file_identity(attestation):
    path = attestation['providerManifest']['workerPath']
    inode = 9 if any(path.startswith(root + '/') for root in REEXTRACTED) else 2
    return [[path, 1, inode, 3, 4, 5]]


class Journal:
    """Installer journal CAS answers for both pending and selected bundles."""

    def __init__(self):
        self.state_root = None
        self.state, self.epoch, self.new_write = 'candidate_ready', 3, False
        self.pending = self.selected = self.previous = None
        self.route_sha = self.selected_sha = self.previous_sha = None
        self.selected_att = self.selected_fid = None
        self.attestation = None
        self.calls = []

    def _att(self):
        raw = (self.state_root / PRODUCTION_ATTESTATION).read_bytes()
        value = json.loads(raw)
        return _sha(raw), _sha(_canon(file_identity(value)).encode())

    def inspect(self, _path=None):
        ready = self.state in ('candidate_ready', 'pending')
        active = self.state in ('active', 'restored_old', 'pending')
        return {'state': self.state, 'epoch': self.epoch,
                'pending': self.pending if ready else None,
                'pendingRouteSha256': self.route_sha if ready else None,
                'selected': self.selected if active else None,
                'selectedRouteSha256': self.selected_sha if active else None,
                'previous': self.previous, 'previousRouteSha256': self.previous_sha,
                'newWriteSeen': self.new_write,
                'providerAttestationSha256': self.selected_att if active else None,
                'providerFileIdentitySha256': self.selected_fid if active else None}

    def approval(self, _path=None, **_kwargs):
        if self.state != 'candidate_ready':
            raise RuntimeError('journal_not_pending')
        att, fid = self._att()
        return {'attestationSha256': att, 'pendingRouteSha256': self.route_sha,
                'providerFileIdentitySha256': fid, 'providerManifest': {'synthetic': True}}

    def activate(self, _path, epoch, route_bytes):
        if (self.state not in ('candidate_ready', 'pending') or epoch != self.epoch or
                _sha(route_bytes) != self.route_sha):
            raise RuntimeError('journal_cas_failed')
        self.calls.append('activate')
        self.selected_att, self.selected_fid = self._att()
        self.state, self.epoch = 'active', self.epoch + 1
        self.selected, self.selected_sha = self.pending, self.route_sha
        self.pending = None
        return self.inspect()

    def restore(self, _path, epoch, route_sha):
        raise RuntimeError('restore_not_in_this_check')

    def replace(self, _path, epoch, current_raw, next_proof, next_raw):
        if (self.state != 'candidate_ready' or epoch != self.epoch or
                _sha(current_raw) != self.route_sha):
            raise RuntimeError('journal_cas_failed')
        self.calls.append('replace')
        self.pending = identity(next_proof)
        self.route_sha, self.epoch = _sha(next_raw), self.epoch + 1
        return self.inspect()

    def stage(self, _path, epoch, next_proof, route_raw):
        if self.state not in ('active', 'restored_old') or epoch != self.epoch:
            raise RuntimeError('journal_cas_failed')
        self.calls.append('stage')
        self.previous, self.previous_sha = self.selected, self.selected_sha
        self.pending, self.route_sha = identity(next_proof), _sha(route_raw)
        self.state, self.epoch, self.new_write = 'pending', self.epoch + 1, False
        return self.inspect()


class FreshBoundCase(BUILD['BuildCase']):
    """A minimal-settings project whose sealed build binds a fresh-wiki ledger."""

    def setUp(self):
        super().setUp()
        self.auto = self.project / '.auto-context'
        self.wiki = self.auto / 'wiki'
        self.wiki.mkdir(parents=True)
        self.settings = self.auto / 'settings.json'
        self.settings.write_text(json.dumps({'name': 'onboard', 'skipPaths': []}))
        raw, wiki = config.minimal_collection_names('onboard')
        self.collections = [{'name': raw, 'role': 'raw', 'sourceRoot': str(self.project)},
                            {'name': wiki, 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        self.roots = {item['name']: item['sourceRoot'] for item in self.collections}
        self.marker = self.auto / fresh.MARKER
        self.route = self.auto / fresh.ROUTE
        self.ledger = self.state / fresh.LEDGER
        self.journal_path = self.state / 'install-journal.sqlite'
        self.fj = Journal()
        self.fj.state_root = self.state
        for target, name, value in (
                (worker, 'inspect_journal', self.fj.inspect),
                (worker, 'preflight_product_candidate', self.fj.approval),
                (fresh, 'inspect_journal', self.fj.inspect),
                (fresh, 'preflight_product_candidate', self.fj.approval),
                (fresh, 'cas_activate', self.fj.activate),
                (fresh, 'cas_restore_fresh', self.fj.restore),
                (discovery, '_raw_snapshot', lambda *_a: {}),
                (discovery, 'seed_source_snapshot', lambda *_a: {}),
                (product, 'preflight_product_candidate', self.fj.approval),
                (product, 'inspect_journal', self.fj.inspect),
                (product, 'plan_fresh_wiki_origin',
                 lambda _project, **_kw: {'proposalSha256': 'd' * 64})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
                ('laya_install_journal.inspect_journal', self.fj.inspect),
                ('laya_install_journal._route',
                 lambda raw, _journal: (json.loads(raw.decode('utf-8')), _sha(raw)))):
            patcher = mock.patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_attestation(self, item, provider):
        value = {'schema': 'laya-production-provider-attestation-v2',
                 'sourceCommit': item['sourceCommit'],
                 'bundleManifestSha256': item['bundleManifestSha256'],
                 'ownershipManifestSha256': item['ownershipManifestSha256'],
                 'journalPath': str(self.journal_path), 'vectorProfile': PROFILE,
                 'providerManifest': provider, 'dependencyClosure': []}
        path = self.state / PRODUCTION_ATTESTATION
        path.write_bytes(_canon(value).encode())
        path.chmod(0o600)
        self.fj.attestation = _sha(path.read_bytes())

    def register(self):
        candidate = self.state / product.BUILD_CANDIDATE
        queue = self.state / product.BUILD_QUEUE
        LayaIndexA(candidate, create=True).close()
        incarnation = product._empty_or_bound_candidate(candidate, SCOPE)
        product._publish_claim_queue(queue, incarnation)
        wiki = next(item['name'] for item in self.collections if item['role'] == 'wiki')
        product.prepare_capacity_stores(candidate, {wiki: str(self.wiki)},
                                        SCOPE, incarnation)
        pending = self.fj.pending
        route = {'schema': ROUTE_V2, 'status': 'active', 'projectRoot': str(self.project),
                 'settingsSha256': settings_identity_digest(self.project, self.collections),
                 'dbPath': str(candidate), 'queuePath': str(queue),
                 'dbIncarnation': incarnation, 'scopeId': SCOPE,
                 'collections': self.collections, 'journalPath': str(self.journal_path),
                 'vectorProfile': PROFILE, 'pluginRoot': pending['pluginRoot'],
                 'installId': pending['installId'], 'generation': pending['generation'],
                 'bundleManifestSha256': pending['bundleManifestSha256'],
                 'ownershipManifestSha256': pending['ownershipManifestSha256']}
        self.route_bytes = json.dumps(route, sort_keys=True, separators=(',', ':')).encode()
        route_file = self.state / 'reviewed-route.json'
        fd = os.open(route_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(self.route_bytes)
        self.fj.route_sha = _sha(self.route_bytes)
        ident = self.identity(dbIncarnation=incarnation, routeSha256=self.fj.route_sha,
                              attestationSha256=self.fj.attestation)
        initial_build.create_or_verify(ident)
        initial_build.register_project(ident)
        return ident

    def bound(self, cards=3):
        """Register, build, request freshness, re-seal and bind."""
        for i in range(cards):
            (self.wiki / f'w{i}.md').write_text(f'# Wiki {i}\nwiki body {i}\n',
                                                encoding='utf-8')
        self.register()
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        finalize = lambda: product.finalize_initial_build(project_root=self.project,
                                                          state_root=self.state)
        self.assertEqual(finalize()['status'], 'freshness_pending')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        result = finalize()
        self.assertEqual(result['status'], 'cutover_prepared', result)
        return result

    def row(self):
        return fresh._load(self.ledger)

    def selection(self):
        return fresh.classify_marker(self.project, self.marker, self.route)

    def activate(self):
        return fresh.activate(self.project, self.journal_path, self.route_bytes)

    def kept(self):
        """Nothing a bundle upgrade may delete: candidate stores and sources."""
        for name in (product.BUILD_CANDIDATE, product.BUILD_QUEUE,
                     product.BUILD_CANDIDATE + '.catalog.sqlite',
                     product.BUILD_CANDIDATE + '.bindings.sqlite'):
            self.assertTrue((self.state / name).is_file(), name)
        self.assertTrue(any(self.wiki.glob('*.md')))


class UpgradeCase(FreshBoundCase):
    def setUp(self):
        super().setUp()
        # Runtime file pinning has its own checks (laya-embed-runtime,
        # laya-journal-v4). These synthetic manifests name no Laya runtime
        # generation, so each reads as one pinned generation here.
        pinned = mock.patch('laya_embed_runtime.sealed_files_sha256',
                            new=lambda _path: 'f' * 64)
        pinned.start()
        self.addCleanup(pinned.stop)
        self.old_root = str(self.home / 'claude-cache' / 'old')
        self.new_root = str(self.home / 'claude-cache' / 'new')
        self.old = proof(self.old_root, OLD_COMMIT)
        self.new = proof(self.new_root, NEW_COMMIT)
        self.fj.pending = identity(self.old)
        self.removed = set()
        self.bundles = {self.old_root: self.old, self.new_root: self.new}
        REEXTRACTED.clear()
        self.addCleanup(REEXTRACTED.clear)
        # The upgrade backup snapshots the journal file; its CAS is synthetic.
        import sqlite3
        with sqlite3.connect(self.journal_path) as db:
            db.execute('CREATE TABLE selection(id INTEGER PRIMARY KEY)')
        self.journal_path.chmod(0o600)
        self.write_attestation(self.old, manifest(self.old_root))
        self.review = {'proposalSha256': 'e' * 64,
                       'reviewedProvider': {'vectorProfile': PROFILE,
                                            'providerManifest': manifest(self.new_root),
                                            'dependencyClosure': []}}

    def inspect_bundle(self, root):
        if root in self.removed:
            raise BundleEvidenceError('bundle_root_unavailable')
        return self.bundles[root]

    def patches(self, module):
        stack = ExitStack()
        enter = stack.enter_context
        enter(mock.patch.object(product, 'review_local_provider',
                                side_effect=lambda **_kw: self.review))
        if module is active:
            enter(mock.patch.object(active, 'review_local_provider',
                                    side_effect=lambda **_kw: self.review))
            enter(mock.patch.object(active, 'cas_stage_update', self.fj.stage))
            enter(mock.patch.object(active, 'cas_activate', self.fj.activate))
            enter(mock.patch.object(active, '_locked_provider', nullcontext))
            enter(mock.patch('laya_embed_resident.wait_for_release', return_value=True))
        else:
            enter(mock.patch.object(module, 'cas_replace_inactive_candidate',
                                    self.fj.replace))
            enter(mock.patch.object(module, 'preflight_product_candidate',
                                    self.fj.approval))
            enter(mock.patch.object(module, 'checked_route',
                                    lambda raw, _j: (json.loads(raw), _sha(raw))))
        enter(mock.patch.object(module, 'inspect_journal', self.fj.inspect))
        enter(mock.patch.object(module, 'adopt_host_loaded_plugin',
                                side_effect=lambda _source, root, **_k: self.bundles[root]))
        enter(mock.patch.object(module, 'inspect_full_plugin_candidate',
                                side_effect=self.inspect_bundle))
        enter(mock.patch.object(module, 'validate_production_provider_attestation',
                                lambda *_a: None))
        enter(mock.patch.object(module, '_provider_file_identity', file_identity))
        return stack

    def run_upgrade(self, module, root=None, commit=NEW_COMMIT):
        with self.patches(module):
            return module.upgrade(project_root=str(self.project), state_root=str(self.state),
                                  source_root=str(self.home / 'marketplace'),
                                  plugin_root=root or self.new_root, commit_id=commit,
                                  accepted_provider_sha256='e' * 64,
                                  model_path='/models/m.gguf', dimension=3)

    def unchanged_fields(self, before, after, changed):
        return {key: before[key] for key in before if key not in changed} == \
            {key: after[key] for key in after if key not in changed}


class BoundUpgradeTests(UpgradeCase):
    def test_bound_build_moves_to_new_bundle_and_still_activates(self):
        self.bound()
        before, record = self.row(), self.record()
        self.removed.add(self.old_root)  # host GC'd the superseded cache
        result = self.run_upgrade(bound_upgrade)
        self.assertEqual(result['status'], 'bound_upgrade_complete', result)
        after = self.row()
        self.assertEqual(after['phase'], 'prepared')
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertTrue(self.unchanged_fields(before, after, bound_upgrade.LEDGER_CHANGES))
        self.assertNotEqual(after['routeSha256'], before['routeSha256'])
        route = json.loads((self.state / 'reviewed-route.json').read_bytes())
        self.assertEqual(route['pluginRoot'], self.new_root)
        self.assertEqual(_sha((self.state / 'reviewed-route.json').read_bytes()),
                         after['routeSha256'])
        self.assertEqual(fresh._read_json(self.marker), fresh._marker(after))
        rebound = self.record()
        self.assertEqual((rebound['phase'], rebound['sealed']), ('bound', record['sealed']))
        self.assertEqual(rebound['identity']['routeSha256'], after['routeSha256'])
        self.assertEqual(self.fj.pending['pluginRoot'], self.new_root)
        self.assertEqual(self.selection(), 'legacy')
        self.assertFalse((self.state / bound_upgrade.INTENT).exists())
        self.assertEqual(len(list(self.state.glob('inactive-upgrade-backup-*'))), 1)
        self.kept()
        # The reviewed activation still proves and selects the same generation.
        self.route_bytes = (self.state / 'reviewed-route.json').read_bytes()
        self.assertEqual(self.activate()['phase'], 'active')
        self.assertEqual(self.selection(), 'candidate')

    def test_bound_build_reextracted_in_place_readopts_its_root(self):
        self.bound()
        self.assertEqual(self.run_upgrade(bound_upgrade)['status'], 'bound_upgrade_complete')
        before = self.row()
        # The host re-extracts the bound bundle at the same root: same bytes,
        # new inodes, so another ownership digest than the journal pins.
        live = {**self.new, 'ownershipManifestSha256': _sha(b'reextracted')}
        self.bundles[self.new_root] = live
        REEXTRACTED.add(self.new_root)
        result = self.run_upgrade(bound_upgrade)
        self.assertEqual(result['status'], 'bound_upgrade_complete', result)
        after = self.row()
        self.assertEqual(after['phase'], 'prepared')
        self.assertTrue(self.unchanged_fields(before, after, bound_upgrade.LEDGER_CHANGES))
        route = json.loads((self.state / 'reviewed-route.json').read_bytes())
        self.assertEqual((route['pluginRoot'], route['ownershipManifestSha256']),
                         (self.new_root, live['ownershipManifestSha256']))
        self.assertEqual(self.fj.pending['ownershipManifestSha256'],
                         live['ownershipManifestSha256'])
        self.assertEqual(self.selection(), 'legacy')
        self.kept()

    def test_interrupted_bound_switch_is_blocked_then_resumes(self):
        self.bound()
        real = bound_upgrade.inactive._atomic_expected

        def interrupted(path, old, new):
            if Path(path).name == fresh.MARKER:
                raise Kill()
            return real(path, old, new)
        with mock.patch.object(bound_upgrade.inactive, '_atomic_expected', interrupted):
            with self.assertRaises(Kill):
                self.run_upgrade(bound_upgrade)
        self.assertEqual(self.selection(), 'blocked')  # never silently legacy
        self.assertTrue((self.state / bound_upgrade.INTENT).exists())
        self.assertEqual(self.run_upgrade(bound_upgrade)['status'], 'bound_upgrade_complete')
        self.assertEqual(self.selection(), 'legacy')

    def test_changed_vector_profile_refuses_before_any_write(self):
        self.bound()
        before = self.row()
        route = (self.state / 'reviewed-route.json').read_bytes()
        self.review = {**self.review, 'reviewedProvider': {
            **self.review['reviewedProvider'],
            'vectorProfile': {**PROFILE, 'providerArtifactSha256': 'f' * 64}}}
        with self.assertRaisesRegex(Exception, 'bound_upgrade_vector_profile_changed'):
            self.run_upgrade(bound_upgrade)
        self.assertEqual(self.row(), before)
        self.assertEqual((self.state / 'reviewed-route.json').read_bytes(), route)
        self.assertEqual(self.selection(), 'legacy')

    def test_qmd_origin_ledger_refuses_before_any_write(self):
        self.bound()
        before = self.row()
        (self.state / 'qmd-cutover.json').write_text('{}')
        with self.assertRaisesRegex(Exception, '^qmd_origin_detach_required$'):
            self.run_upgrade(bound_upgrade)
        self.assertEqual(self.row(), before)


class ActiveUpgradeTests(UpgradeCase):
    def test_selected_build_moves_to_new_bundle_without_index_change(self):
        self.bound()
        self.activate()
        self.assertEqual(self.selection(), 'candidate')
        before = self.row()
        self.removed.add(self.old_root)
        result = self.run_upgrade(active)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        after = self.row()
        self.assertEqual(after['phase'], 'active')
        self.assertTrue(self.unchanged_fields(before, after, active.LEDGER_CHANGES))
        self.assertEqual(json.loads(self.route.read_bytes())['pluginRoot'], self.new_root)
        self.assertEqual(_sha(self.route.read_bytes()), after['routeSha256'])
        self.assertEqual(fresh._read_json(self.marker), fresh._marker(after))
        self.assertEqual(self.fj.calls[-2:], ['stage', 'activate'])
        self.assertEqual(self.fj.selected['pluginRoot'], self.new_root)
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(result['vectorCounts']['vectorChunkSets'],
                         active.inactive._store_counts(self.record()['identity'])
                         ['vectorChunkSets'])
        self.kept()

    def test_interrupted_selected_switch_fences_readers_then_resumes(self):
        self.bound()
        self.activate()
        real = active.inactive._atomic_expected

        def interrupted(path, old, new):
            if Path(path).name == fresh.ROUTE:
                raise Kill()
            return real(path, old, new)
        with mock.patch.object(active.inactive, '_atomic_expected', interrupted):
            with self.assertRaises(Kill):
                self.run_upgrade(active)
        self.assertEqual(self.row()['phase'], 'upgrading')
        self.assertEqual(self.selection(), 'blocked')
        self.assertEqual(self.run_upgrade(active)['status'], 'active_upgrade_complete')
        self.assertEqual(self.selection(), 'candidate')

    def fence(self):
        """Interrupt an active upgrade after its fence: ledger ``upgrading``."""
        self.bound()
        self.activate()
        real = active.inactive._atomic_expected

        def interrupted(path, old, new):
            if Path(path).name == fresh.ROUTE:
                raise Kill()
            return real(path, old, new)
        with mock.patch.object(active.inactive, '_atomic_expected', interrupted):
            with self.assertRaises(Kill):
                self.run_upgrade(active)
        self.assertEqual(self.row()['phase'], 'upgrading')

    @staticmethod
    def run_main(fn, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_a_fenced_recall_asks_for_recovery_and_a_stalled_fence_is_refused(self):
        import laya_bundle_upgrade
        import laya_host_response as response
        import laya_index_host_route as host
        self.fence()
        project = str(self.project)
        payload = self.state.parent / 'recall-payload.json'
        payload.write_text(json.dumps({'cwd': project, 'prompt': 'wiki body',
                                       'hook_event_name': 'UserPromptSubmit'}))
        asked = []
        request = mock.patch.object(laya_bundle_upgrade, 'request',
                                    side_effect=lambda *a, **_k: asked.append(a) or
                                    {'status': 'queued'})
        with request, mock.patch.object(fresh, 'UPGRADE_WAIT_SECONDS', .05, create=True):
            # A live fence: no context, exit 0, and the upgrade worker is asked
            # to finish (it resumes an interrupted intent).
            decision = host.select_route_settled(project, action='recall')
            self.assertEqual(decision['reason'], 'bundle_upgrade_in_progress')
            self.assertEqual(asked, [(project, str(self.journal_path),
                                      'bundle_upgrade_fenced')])
            # SessionStart names the fence and asks for the worker too.
            notice = host.project_dir_notice_for(project)
            self.assertIn('bundle upgrade', notice or '')
            self.assertEqual(len(asked), 2)
            # A fence older than the bound is no longer an upgrade in
            # progress: the recall is refused (visible) and still asks.
            old = time.time() - fresh.UPGRADE_STALE_SECONDS - 60
            os.utime(self.ledger, (old, old))
            decision = host.select_route_settled(project, action='recall')
            self.assertEqual((decision['state'], decision['reason']),
                             ('blocked', 'bundle_upgrade_stalled'))
            self.assertEqual(self.run_main(host.main, 'classify-hook', 'recall',
                                           str(payload), project)[:2], (0, 'blocked\n'))
            self.assertEqual(self.run_main(response.main, 'serve-hook', 'recall', 'claude',
                                           str(payload), project)[0], 3)
            self.assertGreaterEqual(len(asked), 4)
        # The worker the recall asked for finishes the interrupted upgrade.
        self.assertEqual(self.run_upgrade(active)['status'], 'active_upgrade_complete')
        self.assertEqual(self.selection(), 'candidate')

    def test_recall_racing_a_finishing_upgrade_exits_zero(self):
        # The hook classified the route before the upgrade published; core
        # re-selects after it. Within the upgrade window that is not an error.
        import laya_bundle_upgrade
        import laya_host_response as response
        self.bound()
        self.activate()
        project = str(self.project)
        payload = self.state.parent / 'recall-payload.json'
        payload.write_text(json.dumps({'cwd': project, 'prompt': 'wiki body',
                                       'hook_event_name': 'UserPromptSubmit'}))
        classified = {'state': 'candidate', 'reason': 'before_upgrade'}
        with mock.patch.object(response, 'select_hook_route', return_value=classified), \
             mock.patch.object(response, 'select_route',
                               return_value={'state': 'candidate', 'reason': 'after'}):
            refused = self.run_main(response.main, 'serve-hook', 'recall', 'claude',
                                    str(payload), project)
            self.assertEqual(refused[0], 3)  # no upgrade: a changed route is refused
            laya_bundle_upgrade._write_status(self.state, status='complete',
                                              executingRoot=self.new_root, attempts=1,
                                              reason='active_upgrade_complete')
            code, out, err = self.run_main(response.main, 'serve-hook', 'recall', 'claude',
                                           str(payload), project)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('bundle upgrade', err)
            # Other actions keep the refusal.
            self.assertEqual(self.run_main(response.main, 'serve-hook', 'gate', 'claude',
                                           str(payload), project)[0], 3)

    def test_recall_during_the_upgrade_fence_never_exits_nonzero(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        import subprocess
        import tempfile
        import laya_host_response as response
        import laya_index_host_route as host
        self.bound()
        self.activate()
        real = active.inactive._atomic_expected

        def interrupted(path, old, new):
            if Path(path).name == fresh.ROUTE:
                raise Kill()
            return real(path, old, new)
        with mock.patch.object(active.inactive, '_atomic_expected', interrupted):
            with self.assertRaises(Kill):
                self.run_upgrade(active)
        self.assertEqual(self.row()['phase'], 'upgrading')
        project = str(self.project)
        payload = self.state.parent / 'recall-payload.json'
        payload.write_text(json.dumps({'cwd': project, 'prompt': 'wiki body',
                                       'hook_event_name': 'UserPromptSubmit'}))

        def run(fn, *args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = fn(list(args))
            return code, out.getvalue(), err.getvalue()
        with mock.patch.object(fresh, 'UPGRADE_WAIT_SECONDS', .05, create=True):
            # The fenced route stays blocked for every action; only a recall
            # names the in-flight upgrade and answers without context.
            decision = host.select_route_settled(project, action='recall')
            self.assertEqual((decision['state'], decision['reason']),
                             ('blocked', 'bundle_upgrade_in_progress'))
            self.assertEqual(host.select_route_settled(project, action='update')['state'],
                             'blocked')
            self.assertEqual(run(host.main, 'classify-hook', 'recall', str(payload), project)[:2],
                             (0, 'upgrading\n'))
            self.assertEqual(run(host.main, 'classify-hook', 'gate', str(payload), project)[:2],
                             (0, 'blocked\n'))
            code, out, err = run(response.main, 'serve-hook', 'recall', 'claude',
                                 str(payload), project)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('bundle upgrade in progress', err)
            self.assertEqual(run(response.main, 'serve-hook', 'gate', 'claude',
                                 str(payload), project)[0], 3)
        # The dispatcher maps the recall classification to a clean exit.
        with tempfile.TemporaryDirectory() as temp:
            for state, expected in (('upgrading', 0), ('blocked', 3)):
                stub = Path(temp) / state
                (stub / 'hooks').mkdir(parents=True)
                (stub / 'core').mkdir()
                hook = stub / 'hooks' / 'run-hook'
                hook.write_bytes((HERE.parents[1] / 'hooks' / 'run-hook').read_bytes())
                hook.chmod(0o700)
                (stub / 'core' / 'laya_index_host_route.py').write_text(
                    'print(%r)\n' % state)
                env = {key: value for key, value in os.environ.items()
                       if key not in ('QMD_SANDBOX', 'CLAUDE_HEADLESS', 'CLAUDE_SANDBOX')}
                done = subprocess.run(('bash', str(hook), 'recall', 'claude'), input='{}',
                                      capture_output=True, text=True, env=env,
                                      cwd=temp, timeout=30, check=False)
                self.assertEqual(done.returncode, expected, done.stderr)
        # An upgrade that finishes while the recall waits ends the wait at
        # once with the route as it now reads (this fixture's journal is
        # synthetic, so the host route itself stays unproven here).
        blocked = host.select_route(project, action='recall')
        finished = []

        def finish(_seconds):
            if not finished:
                finished.append(self.run_upgrade(active)['status'])
        settled = fresh.settle_decision(project, 'recall', blocked, host.select_route,
                                        sleep=finish)
        self.assertEqual(finished, ['active_upgrade_complete'])
        self.assertEqual(self.selection(), 'candidate')
        self.assertNotEqual(settled.get('reason'), fresh.UPGRADE_IN_PROGRESS)
        self.assertEqual(settled, host.select_route(project, action='recall'))

    def test_same_root_reextracted_in_place_readopts_and_proves_again(self):
        import laya_install_journal as install_journal
        self.bound()
        self.activate()
        self.assertEqual(self.run_upgrade(active)['status'], 'active_upgrade_complete')
        self.assertTrue((self.state / active.INTENT).exists())  # settled receipt
        bound = dict(self.fj.selected)
        # The host re-extracts the same bundle at the same root: same bytes,
        # new inodes, so a new ownership digest and provider file identity.
        live = {**self.new, 'ownershipManifestSha256': _sha(b'reextracted')}
        self.bundles[self.new_root] = live
        REEXTRACTED.add(self.new_root)
        verify = mock.patch.object(install_journal, 'inspect_full_plugin_candidate',
                                   side_effect=self.inspect_bundle)
        with verify:
            with self.assertRaisesRegex(install_journal.JournalEvidenceError,
                                        '^installed_bundle_changed$'):
                install_journal._inspect_identity(bound)
        before = self.row()
        result = self.run_upgrade(active)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        after = self.row()
        self.assertTrue(self.unchanged_fields(before, after, active.LEDGER_CHANGES))
        self.assertNotEqual(after['routeSha256'], before['routeSha256'])
        route = json.loads(self.route.read_bytes())
        self.assertEqual((route['pluginRoot'], route['ownershipManifestSha256']),
                         (self.new_root, live['ownershipManifestSha256']))
        self.assertEqual(self.fj.selected['ownershipManifestSha256'],
                         live['ownershipManifestSha256'])
        self.assertEqual(self.fj.selected['bundleManifestSha256'],
                         bound['bundleManifestSha256'])
        with verify:
            self.assertEqual(install_journal._inspect_identity(self.fj.selected), live)
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(len(list(self.state.glob('active-upgrade-*-settled.json'))), 1)
        self.kept()

    def test_same_root_unchanged_settled_intent_is_not_rerun(self):
        self.bound()
        self.activate()
        self.assertEqual(self.run_upgrade(active)['status'], 'active_upgrade_complete')
        before = self.row()
        self.assertEqual(self.run_upgrade(active)['status'], 'active_upgrade_complete')
        self.assertEqual(self.row(), before)
        self.assertEqual(list(self.state.glob('active-upgrade-*-settled.json')), [])

    def test_qmd_origin_intent_refuses(self):
        self.bound()
        self.activate()
        (self.state / active.INTENT).write_bytes(initial_build._canon({
            'schema': 'laya-active-upgrade-intent-v1', 'origin': 'qmd',
            'projectRoot': str(self.project), 'stateRoot': str(self.state),
            'backup': '', 'oldRecord': {}, 'oldLedger': {}, 'newLedger': {}}))
        (self.state / active.INTENT).chmod(0o600)
        with self.assertRaisesRegex(Exception, '^qmd_origin_detach_required$'):
            self.run_upgrade(active)
        self.assertEqual(self.row()['phase'], 'active')


class CrossHostUpgradeTests(UpgradeCase):
    """Claude 0.37.1 -> Codex 0.37.1 (newer commit, same label) -> Claude 0.37.2.

    The Codex upgrade leaves its settled intent behind; Codex then re-extracts
    or removes its cache directory before the next Claude bundle arrives.
    """
    CODEX_COMMIT, NEXT_COMMIT = '2' * 40, '3' * 40

    def setUp(self):
        super().setUp()
        cache = lambda host: self.home / host / 'plugins' / 'cache' / 'mkt' / 'qmd-auto-context'
        self.old_root = str(cache('.claude') / '0.37.1')
        self.new_root = str(cache('.codex') / '0.37.1')
        self.next_root = str(cache('.claude') / '0.37.2')
        self.old = proof(self.old_root, OLD_COMMIT)
        self.new = proof(self.new_root, self.CODEX_COMMIT)
        self.next = proof(self.next_root, self.NEXT_COMMIT)
        self.bundles = {self.old_root: self.old, self.new_root: self.new,
                        self.next_root: self.next}
        self.fj.pending = identity(self.old)
        self.write_attestation(self.old, manifest(self.old_root))
        self.review = self.reviewed_at(self.new_root)
        self.bound()
        self.activate()
        result = self.run_upgrade(active, self.new_root, self.CODEX_COMMIT)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        self.assertTrue((self.state / active.INTENT).exists())  # settled receipt stays
        self.review = self.reviewed_at(self.next_root)

    def reviewed_at(self, root):
        return {**self.review, 'reviewedProvider': {
            **self.review['reviewedProvider'], 'providerManifest': manifest(root)}}

    def assert_moved_to_next(self):
        before = self.row()
        result = self.run_upgrade(active, self.next_root, self.NEXT_COMMIT)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        after = self.row()
        self.assertTrue(self.unchanged_fields(before, after, active.LEDGER_CHANGES))
        self.assertEqual(json.loads(self.route.read_bytes())['pluginRoot'], self.next_root)
        self.assertEqual(self.fj.selected['pluginRoot'], self.next_root)
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(len(list(self.state.glob('active-upgrade-*-settled.json'))), 1)
        intent = json.loads((self.state / active.INTENT).read_bytes())
        self.assertEqual(intent['newProof']['pluginRoot'], self.next_root)
        self.kept()

    def test_codex_cache_reextracted_in_place_does_not_wedge_next_claude_bundle(self):
        REEXTRACTED.add(self.new_root)  # same files, new inodes
        self.assert_moved_to_next()

    def test_codex_cache_removed_does_not_wedge_next_claude_bundle(self):
        self.removed.add(self.new_root)
        self.assert_moved_to_next()

    def test_codex_cache_readopted_at_another_commit_is_treated_as_replaced(self):
        self.bundles[self.new_root] = proof(self.new_root, '4' * 40)
        self.assert_moved_to_next()

    def test_unsettled_foreign_intent_still_refuses(self):
        # An interrupted upgrade is not a receipt: another bundle must not
        # archive it, and its own resume still verifies the new provider.
        fenced = self.row()
        with mock.patch.object(active, '_switch', side_effect=Kill()):
            with self.assertRaises(Kill):
                self.run_upgrade(active, self.next_root, self.NEXT_COMMIT)
        self.assertEqual(self.row(), fenced)
        intent = json.loads((self.state / active.INTENT).read_bytes())
        self.assertEqual(intent['newProof']['pluginRoot'], self.next_root)
        self.bundles[self.new_root] = proof(self.new_root, '5' * 40)
        with self.assertRaisesRegex(Exception, 'active_upgrade_prior_unsettled'):
            self.run_upgrade(active, self.new_root, '5' * 40)
        REEXTRACTED.add(self.next_root)
        with self.assertRaisesRegex(Exception, 'active_upgrade_new_provider_changed'):
            self.run_upgrade(active, self.next_root, self.NEXT_COMMIT)
        self.assertEqual(self.row(), fenced)
        self.assertEqual(self.selection(), 'candidate')


if __name__ == '__main__':
    unittest.main()
