#!/usr/bin/env python3
"""Inactive initial-build checks: synthetic temp corpora, no model, QMD or host.

HOME is a temp directory, so the build pointer, state root and every store
(A-int DB, claim queue, catalog, binding store, build record) are disposable.
The provider is a fake that returns one fixed 2-D chunk per file and counts
calls. The installer journal and bundle attestation are stubbed at the
worker's import names; their own fixtures cover them.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_initial_build as initial_build
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_capacity import generation_digest
from laya_index_source_port import scan_collections
from laya_index_vector import profile_identity
import laya_product_install as product
import laya_product_sync_worker as worker
import laya_source_catalog
from laya_source_catalog import LayaSourceCatalog
import laya_vector_capacity as vc

SCOPE = 'c' * 32
INSTALL = '4' * 32
PROFILE = {'model_id': 'synthetic-precomputed-only', 'embedding_fingerprint': 'f' * 64,
           'dimension': 2}
PROFILE_ID = profile_identity(PROFILE['model_id'], PROFILE['embedding_fingerprint'],
                              PROFILE['dimension'])


class FakeProvider:
    calls = []

    def __init__(self, _manifest=None):
        self.profile = {**PROFILE, 'profile_id': PROFILE_ID}
        self.manifest = {'recipeId': vc.CANDIDATE_RECIPE}

    def embed_document_chunks(self, body, path):
        FakeProvider.calls.append(path)
        return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}]


class Coordinator:
    def __init__(self):
        self.ready = True
        self.lanes = []
        self.root = None

    @contextmanager
    def activity(self, lane, policy, report):
        self.lanes.append(lane)
        yield 1

    def check(self, policy, report):
        return {'ready': self.ready}


class Service:
    def __init__(self, log):
        self.log = log

    def stop(self):
        self.log.append('stop')


class BuildCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-initial-build-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / 'home'
        self.home.mkdir(mode=0o700)
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SANDBOX': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.project = base / 'project'
        self.docs = self.project / 'docs'
        self.wiki = self.project / 'wiki'
        self.docs.mkdir(parents=True)
        self.wiki.mkdir()
        cursor = self.home
        for name in ('.local', 'state', 'laya', INSTALL):
            cursor = cursor / name
            cursor.mkdir(mode=0o700)
        self.state = cursor
        self.collections = [{'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.docs)},
                            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        self.roots = {item['name']: item['sourceRoot'] for item in self.collections}
        FakeProvider.calls = []
        self.coordinator = Coordinator()
        self.service_log = []
        self.journal = {'state': 'candidate_ready', 'pending': {'synthetic': True},
                        'pendingRouteSha256': 'a' * 64, 'newWriteSeen': False, 'epoch': 3}
        self.attestation = 'b' * 64
        for target, value in (
                ('inspect_journal', lambda _path: dict(self.journal)),
                ('preflight_product_candidate', lambda _path, **_kwargs: {
                    'attestationSha256': self.attestation,
                    'pendingRouteSha256': self.journal['pendingRouteSha256'],
                    'providerManifest': {'synthetic': True}}),
                ('ReviewedLocalQmdProvider', FakeProvider),
                ('resource_report', lambda *_a, **_k: {'synthetic': True}),
                ('_higher_priority_wake', lambda _controller: False)):
            patcher = mock.patch.object(worker, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- stores --------------------------------------------------------------

    def identity(self, **changes):
        candidate = self.state / product.BUILD_CANDIDATE
        identity = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                    'installId': INSTALL, 'scopeId': SCOPE,
                    'collections': self.collections, 'candidateDb': str(candidate),
                    'queueDb': str(self.state / product.BUILD_QUEUE),
                    'catalogDb': str(candidate) + '.catalog.sqlite',
                    'bindingStore': str(candidate) + '.bindings.sqlite',
                    'dbIncarnation': '0' * 32,
                    'journalPath': str(self.state / 'install-journal.sqlite'),
                    'routeFile': str(self.state / 'reviewed-route.json'),
                    'routeSha256': 'a' * 64, 'attestationSha256': self.attestation,
                    'vectorProfileId': PROFILE_ID,
                    'sourceDb': str(self.project / 'qmd.sqlite'), 'sourceFormat': 'qmd-2.5.3',
                    'proposalSha256': 'd' * 64, 'providerProposalSha256': 'e' * 64}
        identity.update(changes)
        return identity

    def register(self):
        """The stores ``_register_initial_build`` creates, without bundle/journal."""
        candidate = self.state / product.BUILD_CANDIDATE
        LayaIndexA(candidate, create=True).close()
        incarnation = product._empty_or_bound_candidate(candidate, SCOPE)
        product._publish_claim_queue(self.state / product.BUILD_QUEUE, incarnation)
        product.prepare_capacity_stores(candidate, {'wiki': self.roots['wiki']},
                                        SCOPE, incarnation)
        identity = self.identity(dbIncarnation=incarnation)
        initial_build.create_or_verify(identity)
        initial_build.register_project(identity)
        return identity

    def write(self, rel, text):
        path = self.project / rel
        path.write_text(text, encoding='utf-8')
        return path

    def corpus(self, n, collection='wiki'):
        for i in range(n):
            self.write(f'{collection}/f{i:03d}.md', f'# F{i}\nbody {i}\n')

    def start(self, manifest, *, admit):
        self.service_log.append('start')
        if not admit():
            raise IndexContractError('provider_admission_deferred')
        return Service(self.service_log)

    def record(self):
        return initial_build.read(self.state)

    def wake(self):
        return worker.run_initial_build(self.project, self.record(),
                                        coordinator=self.coordinator, report={},
                                        start_service=self.start)

    def until_settled(self, limit=200):
        answers = []
        for _ in range(limit):
            answers.append(self.wake())
            if answers[-1]['state'] not in ('pending',):
                return answers
        raise AssertionError('build did not settle: %r' % answers[-3:])

    def head(self):
        with closing(LayaIndexA(self.state / product.BUILD_CANDIDATE)) as index:
            row = index.db.execute('SELECT gen FROM head WHERE scope=?', (SCOPE,)).fetchone()
            return row[0] if row else None

    def members(self, generation):
        with closing(LayaIndexA(self.state / product.BUILD_CANDIDATE)) as index:
            return [row[0] for row in index.db.execute(
                'SELECT path FROM membership WHERE gen=? ORDER BY path', (generation,))]

    def catalog(self):
        record = self.record()
        catalog = LayaSourceCatalog(record['identity']['catalogDb'],
                                    {'wiki': self.roots['wiki']}, SCOPE)
        self.addCleanup(catalog.close)
        return catalog


class BuildProgressTests(BuildCase):
    def test_registered_build_progresses_in_bounded_wakes_to_a_sealed_candidate(self):
        self.corpus(12)
        for i in range(3):
            self.write(f'wiki/extra{i:03d}.md', f'# Wiki {i}\nwiki body {i}\n')
        self.register()
        self.assertEqual(self.record()['phase'], 'registered')
        with mock.patch.object(worker, 'MAX_CAPACITY_ROUNDS', 1):
            answers = self.until_settled()
        self.assertGreater(len(answers), 2, 'more than one bounded wake')
        self.assertTrue(all(a['state'] == 'pending' and a['initialBuild'] == 'building'
                            for a in answers[:-1]))
        self.assertIn(answers[-1]['state'], ('indexed', 'unchanged'))
        self.assertEqual(answers[-1]['initialBuild'], 'sealed')
        record = self.record()
        sealed = record['sealed']
        self.assertEqual(record['phase'], 'sealed')
        self.assertEqual(sealed['generation'], self.head())
        self.assertEqual(generation_digest(sealed['generation']), sealed['bindingDigest'])
        self.assertEqual((sealed['files'], sealed['vectorRows']), (15, 15))
        self.assertEqual(sealed['vectorProfileId'], PROFILE_ID)
        self.assertEqual(sorted(FakeProvider.calls),
                         sorted([f'wiki/f{i:03d}.md' for i in range(12)] +
                                [f'wiki/extra{i:03d}.md' for i in range(3)]))
        self.assertEqual(len(self.members(sealed['generation'])), 15)
        # A sealed build ACKs without another model start or publish.
        calls, log = list(FakeProvider.calls), list(self.service_log)
        again = self.wake()
        self.assertEqual((again['state'], again['initialBuild']), ('unchanged', 'sealed'))
        self.assertEqual((FakeProvider.calls, self.service_log), (calls, log))
        self.assertFalse(initial_build.freshness_satisfied(self.record()))

    def test_no_total_corpus_admission_above_200_files_and_5_mb(self):
        self.corpus(205)
        filler = ('lorem ipsum dolor sit amet ' * 40 + '\n') * 830
        for i in range(6):
            self.write(f'wiki/big{i}.md', f'# Big {i}\n' + filler)
        self.register()
        answers = self.until_settled(limit=400)
        self.assertEqual(answers[-1]['initialBuild'], 'sealed', answers[-1])
        sealed = self.record()['sealed']
        self.assertEqual(sealed['files'], 211)
        self.assertGreater(sealed['bytes'], 5_000_000)
        self.assertEqual(len(FakeProvider.calls), 211)

    def test_one_synchronous_scan_bound_routes_to_background_not_refusal(self):
        self.corpus(201, collection='docs')
        class Empty:
            def list_exclusions(self, _scope):
                return []
        with self.assertRaises(IndexContractError) as caught:
            scan_collections(self.roots, SCOPE, Empty(), max_files=product.SYNC_ORIGIN_MAX_FILES,
                             max_bytes=product.SYNC_ORIGIN_MAX_BYTES)
        self.assertTrue(product._exceeds_one_scan(caught.exception))
        (self.docs / 'f000.md').unlink()
        (self.docs / 'f000.md').symlink_to(self.docs / 'f001.md')
        with self.assertRaises(IndexContractError) as unsafe:
            scan_collections(self.roots, SCOPE, Empty(), max_files=10_000)
        self.assertFalse(product._exceeds_one_scan(unsafe.exception))
        self.assertFalse(hasattr(product, 'MAX_INITIAL_FILES'))

    def test_large_single_file_is_hashed_by_one_admitted_step(self):
        self.corpus(4)
        big = self.write('wiki/zz-big.md', '# Big\n' + 'word ' * 800 + '\n')
        size = big.stat().st_size
        self.register()
        real = worker.sync_step
        budgets = []

        def small_steps(runtime, **kw):
            kw.setdefault('max_bytes', 1000)  # a step budget below one member
            budgets.append((kw.get('max_files'), kw['max_bytes']))
            return real(runtime, **kw)
        with mock.patch.object(worker, 'sync_step', small_steps):
            self.coordinator.ready = False
            deferred = self.wake()
            self.assertEqual((deferred['state'], deferred['reason']),
                             ('deferred', 'laya_build_resource_pressure'))
            active = self.catalog().status()['active']
            self.assertEqual(active['deferred_path'], 'wiki/zz-big.md')
            self.assertIsNone(self.head())
            self.assertEqual(self.service_log, [], 'no model start under pressure')
            self.coordinator.ready = True
            answers = self.until_settled()
        self.assertIn((1, size), budgets)
        self.assertEqual(answers[-1]['initialBuild'], 'sealed')
        self.assertIn('wiki/zz-big.md', self.members(self.record()['sealed']['generation']))

    def test_member_over_the_provider_text_limit_blocks_visibly(self):
        # Remaining gap: the reviewed provider embeds one document per
        # request, capped at 5,000,000 UTF-8 bytes. Hashing such a file is a
        # bounded single-file step; embedding it is not possible yet.
        self.corpus(1)
        self.write('wiki/huge.md', '# Huge\n' + 'x' * (vc.PROVIDER_MAX_TEXT_BYTES + 1))
        self.register()
        answers = self.until_settled()
        self.assertEqual((answers[-1]['state'], answers[-1]['reason'], answers[-1]['path']),
                         ('blocked', 'vector_member_rejected', 'wiki/huge.md'))
        record = self.record()
        self.assertEqual(record['phase'], 'blocked')
        self.assertEqual(record['blocked']['path'], 'wiki/huge.md')
        self.assertIsNone(self.head())
        self.assertNotIn('wiki/huge.md', FakeProvider.calls)

    def test_pressure_defers_before_model_start_and_resumes(self):
        self.corpus(3)
        self.register()
        self.coordinator.ready = False
        answers = [self.wake() for _ in range(5)]
        self.assertEqual(answers[-1], {'state': 'deferred',
                                       'reason': 'laya_build_resource_pressure',
                                       'initialBuild': 'building'})
        self.assertEqual((FakeProvider.calls, self.service_log), ([], []))
        self.assertEqual(self.record()['progress']['reason'], 'laya_build_resource_pressure')
        self.coordinator.ready = True
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(len(FakeProvider.calls), 3)

    def test_second_owner_defers_without_any_write(self):
        self.corpus(2)
        self.register()
        before = self.record()
        with initial_build.owner(self.state) as owned:
            self.assertTrue(owned)
            busy = self.wake()
        self.assertEqual(busy, {'state': 'deferred', 'reason': 'initial_build_busy'})
        self.assertEqual(self.record(), before)
        self.assertEqual(FakeProvider.calls, [])

    def test_foreign_executing_bundle_is_refused_before_model_or_store_write(self):
        self.corpus(1)
        self.register()
        before = self.record()
        def reject(_path, *, require_executing=False):
            self.assertTrue(require_executing)
            raise IndexContractError('executing_bundle_not_selected')
        with mock.patch.object(worker, 'preflight_product_candidate', reject):
            with self.assertRaisesRegex(IndexContractError,
                                        'executing_bundle_not_selected'):
                self.wake()
        self.assertEqual(self.record(), before)
        self.assertIsNone(self.head())
        self.assertEqual((FakeProvider.calls, self.service_log), ([], []))

    def test_changed_journal_or_attestation_stops_before_any_publish(self):
        self.corpus(2)
        self.register()
        self.attestation = '9' * 64
        with self.assertRaisesRegex(IndexContractError, 'initial_build_attestation_changed'):
            self.wake()
        self.attestation = self.record()['identity']['attestationSha256']
        flips = {'n': 0}

        def journal(_path):
            flips['n'] += 1
            # A write or selection appears after the first wake check.
            return {**self.journal, 'newWriteSeen': flips['n'] > 1}
        with mock.patch.object(worker, 'inspect_journal', journal):
            with self.assertRaisesRegex(IndexContractError,
                                        'initial_build_(proof|journal)_changed'):
                self.until_settled()
        self.assertIsNone(self.head())

    def test_selected_project_never_runs_the_inactive_build(self):
        self.corpus(1)
        self.register()
        with mock.patch.object(worker, 'select_route',
                               return_value={'state': 'candidate', 'reason': 'route'}):
            with self.assertRaisesRegex(IndexContractError, 'initial_build_project_selected'):
                self.wake()
        self.assertEqual(FakeProvider.calls, [])


class FreshnessAndRecoveryTests(BuildCase):
    def sealed_build(self, n=4):
        self.corpus(n)
        self.register()
        answers = self.until_settled()
        self.assertEqual(answers[-1]['initialBuild'], 'sealed')
        return self.record()

    def test_freshness_requires_a_scan_started_after_the_request(self):
        sealed = self.sealed_build()
        self.assertFalse(initial_build.freshness_satisfied(sealed))
        before = sealed['sealed']
        record = initial_build.request_freshness(sealed, self.catalog())
        self.assertEqual(record['phase'], 'building')
        self.assertIsNone(record['sealed'])
        self.assertFalse(initial_build.freshness_satisfied(record))
        calls = list(FakeProvider.calls)
        answers = self.until_settled()
        record = self.record()
        self.assertEqual(answers[-1]['initialBuild'], 'sealed')
        self.assertTrue(initial_build.freshness_satisfied(record))
        self.assertGreaterEqual(record['sealed']['scanStartedNs'],
                                record['freshness']['requestedNs'])
        # An unchanged rescan keeps the generation and embeds nothing.
        self.assertEqual(record['sealed']['generation'], before['generation'])
        self.assertNotEqual(record['sealed']['catalogScanId'], before['catalogScanId'])
        self.assertEqual(FakeProvider.calls, calls)

    def test_partial_catalog_never_publishes_or_deletes(self):
        sealed = self.sealed_build(6)['sealed']
        (self.wiki / 'f000.md').unlink()
        initial_build.request_freshness(self.record(), self.catalog())
        real = worker.sync_step

        def tiny(runtime, **kw):
            kw.setdefault('max_files', 1)
            return real(runtime, **kw)
        with mock.patch.object(worker, 'sync_step', tiny), \
                mock.patch.object(worker, 'MAX_CAPACITY_ROUNDS', 1):
            partial = self.wake()
        self.assertEqual((partial['state'], partial['initialBuild']), ('pending', 'building'))
        self.assertEqual(self.head(), sealed['generation'])
        self.assertEqual(len(self.members(sealed['generation'])), 6)
        final = self.until_settled()[-1]
        self.assertEqual(final['initialBuild'], 'sealed')
        fresh = self.record()['sealed']
        self.assertNotEqual(fresh['generation'], sealed['generation'])
        self.assertNotIn('wiki/f000.md', self.members(fresh['generation']))
        # The superseded generation is retained, never deleted.
        self.assertEqual(len(self.members(sealed['generation'])), 6)

    def test_crash_after_publish_resumes_from_durable_state(self):
        self.corpus(3)
        self.register()

        class Kill(BaseException):
            pass
        real = initial_build.advance
        state = {'armed': False}

        def advance(record, **changes):
            if changes.get('phase') == 'sealed' and not state['armed']:
                state['armed'] = True
                raise Kill()
            return real(record, **changes)
        with mock.patch.object(initial_build, 'advance', advance):
            with self.assertRaises(Kill):
                self.until_settled()
        published = self.head()
        self.assertIsNotNone(published)
        self.assertNotEqual(self.record()['phase'], 'sealed')
        calls = list(FakeProvider.calls)
        answer = self.until_settled()[-1]
        self.assertEqual((answer['initialBuild'], self.record()['sealed']['generation']),
                         ('sealed', published))
        self.assertEqual(FakeProvider.calls, calls, 'replay embeds nothing again')

    def test_explicit_rescan_clears_a_source_block(self):
        self.corpus(1)
        (self.wiki / 'link.md').symlink_to(self.wiki / 'f000.md')
        self.register()
        blocked = self.until_settled()[-1]
        self.assertEqual((blocked['state'], self.record()['phase']), ('blocked', 'blocked'))
        self.assertEqual(self.wake()['state'], 'blocked', 'a retry alone does not rescan')
        (self.wiki / 'link.md').unlink()
        rescan = product.request_initial_build_rescan(self.state)
        self.assertEqual(rescan['status'], 'rescan_requested')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')


class RecordAndInstallerTests(BuildCase):
    def test_record_and_pointer_are_create_or_verify(self):
        identity = self.identity()
        first = initial_build.create_or_verify(identity)
        self.assertEqual(initial_build.create_or_verify(identity), first)
        with self.assertRaisesRegex(IndexContractError, 'initial_build_identity_changed'):
            initial_build.create_or_verify({**identity, 'proposalSha256': '7' * 64})
        pointer = initial_build.register_project(identity)
        self.assertEqual(initial_build.register_project(identity), pointer)
        self.assertEqual(os.stat(pointer).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(initial_build.record_path(self.state)).st_mode & 0o777,
                         0o600)
        with self.assertRaisesRegex(IndexContractError, 'initial_build_already_registered'):
            initial_build.register_project({**identity, 'installId': '5' * 32})
        self.assertEqual(initial_build.lookup(self.project), first)
        self.assertIsNone(initial_build.lookup(self.project.parent))
        with self.assertRaisesRegex(IndexContractError, 'initial_build_stale_cas'):
            initial_build.advance({**first, 'epoch': 9}, phase='building')

    def test_pure_transition_never_seals_without_evidence(self):
        record = initial_build.create_or_verify(self.identity())
        changes = initial_build.transition(record, {'state': 'unchanged', 'generation': 'g'})
        self.assertEqual(changes['phase'], 'building')
        blocked = initial_build.transition(record, {'state': 'blocked',
                                                    'reason': 'source_invalid',
                                                    'detail': 'symlink: docs/x.md'})
        self.assertEqual((blocked['phase'], blocked['blocked']['reason']),
                         ('blocked', 'source_invalid'))
        with self.assertRaisesRegex(IndexContractError, 'initial_build_result_invalid'):
            initial_build.transition(record, {'state': 'weird'})

    def test_registration_creates_private_inactive_stores_idempotently(self):
        plan = {'projectRoot': str(self.project), 'settingsSha256': '1' * 64,
                'scopeId': SCOPE, 'collections': self.collections,
                'sourceDb': str(self.project / 'qmd.sqlite'), 'sourceFormat': 'qmd-2.5.3',
                'proposalSha256': 'd' * 64}
        proof = {'bundleManifestSha256': '2' * 64, 'ownershipManifestSha256': '3' * 64}
        staged = []

        def stage(**kw):
            staged.append(kw['route_bytes'])
            return {'attestationSha256': self.attestation,
                    'pendingRouteSha256': hashlib.sha256(kw['route_bytes']).hexdigest()}
        kwargs = dict(plan=plan, reviewed_provider={'vectorProfile': {}},
                      provider_proposal_sha256='e' * 64, source_root='/src',
                      plugin_root=str(self.project.parent / 'plugin'), state=self.state,
                      journal=self.state / 'install-journal.sqlite',
                      socket=self.state / 'runtime' / 'laya-embed.sock',
                      commit_id='0' * 40, install_id=INSTALL, user_qmd_roots=['/q'])
        with mock.patch.object(product, '_adopt_and_attest',
                               return_value=(proof, {'profileId': PROFILE_ID}, {})), \
                mock.patch.object(product, 'stage_host_loaded_candidate', side_effect=stage):
            first = product._register_initial_build(**kwargs)
            again = product._register_initial_build(**kwargs)
        self.assertEqual(first['status'], 'initial_build_registered')
        self.assertEqual({k: v for k, v in first.items() if k != 'wake'},
                         {k: v for k, v in again.items() if k != 'wake'})
        self.assertEqual(first['wake'], {'status': 'wake_skipped_in_fixture'})
        self.assertEqual(staged[0], staged[1])
        route = json.loads(staged[0])
        self.assertEqual(route['dbPath'], str(self.state / product.BUILD_CANDIDATE))
        record = self.record()
        self.assertEqual(record['phase'], 'registered')
        self.assertEqual(record['identity']['dbIncarnation'], route['dbIncarnation'])
        for name in (product.BUILD_CANDIDATE,
                     product.BUILD_CANDIDATE + '.catalog.sqlite',
                     product.BUILD_CANDIDATE + '.bindings.sqlite',
                     'reviewed-route.json', initial_build.RECORD):
            self.assertEqual((self.state / name).stat().st_mode & 0o077, 0, name)
        self.assertFalse((self.project / '.auto-context').exists(), 'no project file')
        self.assertIsNone(self.head())
        self.assertEqual(FakeProvider.calls, [])
        # The runtime wake reaches the build through the unchanged entrypoint.
        self.journal['pendingRouteSha256'] = record['identity']['routeSha256']
        self.write('wiki/a.md', '# A\nalpha\n')
        answers = []
        for _ in range(50):
            answers.append(worker.run(self.project, coordinator=self.coordinator, report={},
                                      start_service=self.start))
            if answers[-1]['state'] != 'pending':
                break
        self.assertEqual(answers[-1]['initialBuild'], 'sealed')
        self.assertEqual(FakeProvider.calls, ['wiki/a.md'])

    def test_run_without_a_registered_build_keeps_the_refusal(self):
        with self.assertRaisesRegex(IndexContractError, 'selected_route_unavailable'):
            worker.run(self.project, coordinator=self.coordinator, report={},
                       start_service=self.start)

    def test_finalize_reads_qmd_only_after_a_fresh_seal(self):
        # The full bind -> activate -> restore flow is in
        # laya-qmd-cutover-bound-checks.py; here only the freshness gate.
        self.corpus(3)
        identity = self.register()
        patches = [
            mock.patch.object(product, 'plan_qmd_origin',
                              return_value={'proposalSha256': identity['proposalSha256']}),
            mock.patch('laya_install_journal.inspect_journal',
                       side_effect=lambda _p: dict(self.journal)),
            mock.patch.object(product, 'preflight_product_candidate',
                              return_value={'attestationSha256': self.attestation}),
            mock.patch('laya_qmd_migration.inspect_qmd_collections',
                       side_effect=AssertionError('qmd read')),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        first = product.finalize_initial_build(project_root=self.project,
                                               state_root=self.state)
        self.assertEqual(first['status'], 'freshness_pending')
        requested = self.record()['freshness']
        scan = self.catalog().status()['active']['scan_id']
        second = product.finalize_initial_build(project_root=self.project,
                                                state_root=self.state)
        self.assertEqual(second['status'], 'freshness_pending')
        self.assertEqual(self.record()['freshness'], requested, 'no second request')
        self.assertEqual(self.catalog().status()['active']['scan_id'], scan)
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertTrue(initial_build.freshness_satisfied(self.record()))
        # Fresh and sealed: the next call reaches the QMD inventory read that
        # precedes the backup and the bound cutover.
        with self.assertRaisesRegex(AssertionError, 'qmd read'):
            product.finalize_initial_build(project_root=self.project, state_root=self.state)
        self.assertEqual(sorted(p.name for p in self.state.glob('qmd-origin-backup*')), [])
        self.assertEqual(self.record()['phase'], 'sealed')
        self.assertFalse((self.project / '.auto-context').exists())

    def test_finalize_refuses_a_candidate_that_changed_after_sealing(self):
        self.corpus(2)
        identity = self.register()
        self.until_settled()
        initial_build.request_freshness(self.record(), self.catalog())
        self.until_settled()
        record = self.record()
        forged = {**record['sealed'], 'vectorRows': record['sealed']['vectorRows'] + 1}
        with initial_build.owner(self.state):
            initial_build.advance(record, sealed=forged)
        with mock.patch.object(product, 'plan_qmd_origin',
                               return_value={'proposalSha256': identity['proposalSha256']}), \
                mock.patch('laya_install_journal.inspect_journal',
                           side_effect=lambda _p: dict(self.journal)), \
                mock.patch.object(product, 'preflight_product_candidate',
                                  return_value={'attestationSha256': self.attestation}):
            with self.assertRaisesRegex(product.InstallBlocked,
                                        'initial_build_candidate_changed'):
                product.finalize_initial_build(project_root=self.project,
                                               state_root=self.state)


class CatalogScaleTests(unittest.TestCase):
    def test_entry_bound_applies_per_listing_not_per_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'docs'
            for name in ('a', 'b'):
                (root / name).mkdir(parents=True)
                for i in range(4):
                    (root / name / f'{i}.md').write_text(f'# {i}\n', encoding='utf-8')
            roots = {'docs': str(root)}
            with mock.patch.object(laya_source_catalog, 'MAX_ENTRIES', 5), \
                    closing(LayaSourceCatalog(str(base / 'cat.sqlite'), roots, SCOPE,
                                              create=True)) as catalog:
                catalog.request_scan()
                for _ in range(100):
                    step = catalog.step()
                    if step['status'] != 'pending':
                        break
                self.assertEqual(step['status'], 'complete', step)
                self.assertEqual(catalog.status()['current']['files'], 8)
                for i in range(4, 6):
                    (root / 'a' / f'{i}.md').write_text('# x\n', encoding='utf-8')
                catalog.request_scan()
                for _ in range(100):
                    step = catalog.step()
                    if step['status'] != 'pending':
                        break
                self.assertEqual(step['status'], 'blocked')
                self.assertIn('too many entries in one directory', step['blocked']['detail'])


if __name__ == '__main__':
    unittest.main()
