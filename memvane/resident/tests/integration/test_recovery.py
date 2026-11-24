"""Resident retry state and admission under a controlled clock."""
import errno
import json
import os
from pathlib import Path

from tests.support.case import FixtureCase
from unittest.mock import Mock, patch
from memvane.provider import api as provider
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as svc
from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred


class InlineThread:
    def __init__(self, *, target, args, **_kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class RecoveryChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home / 'r'
        self.base.mkdir(mode=0o700)
        self.manifest = {'serviceSocketPath': str(self.base / 'embed.sock'),
                         'embeddingFingerprint': 'a' * 64,
                         'approvedProviderId': 'synthetic',
                         'providerArtifactSha256': 'b' * 64,
                         'modelId': 'synthetic-model',
                         'dimension': 4}

    def stamp(self, reason, *, terminal=True, attempts=3):
        with patch.object(owner.time, 'time', return_value=1000):
            owner._write_state(self.manifest, reason, terminal=terminal, attempts=attempts)

    def cold(self, now):
        return (patch.object(owner, 'probe', return_value='absent'),
                patch.object(owner.time, 'time', return_value=now))

    def test_legacy_transient_terminal_recovers_after_cooldown(self):
        for reason in ('startup_failed', 'worker_exited'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(1301)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_called_once()

    def test_transient_terminal_waits_without_extending_cooldown(self):
        self.stamp('startup_failed')
        probe, clock = self.cold(1299)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        self.assertEqual(owner.read_owner_state(self.manifest)['at'], 1000)

    def test_identity_failure_remains_terminal(self):
        self.stamp('identity_invalid')
        probe, clock = self.cold(100000)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()

    def test_attestation_retries_from_the_recorded_time_or_after_clock_rollback(self):
        self.stamp('attestation_invalid')
        for now, retry in ((4599, False), (4600, True), (999, True)):
            with self.subTest(now=now):
                probe, clock = self.cold(now)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    if retry:
                        with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                            owner.ensure_running('/project', self.manifest)
                        adopt.assert_called_once()
                    else:
                        with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                            owner.ensure_running('/project', self.manifest)
                        adopt.assert_not_called()
                self.assertEqual(owner.read_owner_state(self.manifest)['at'], 1000)

    def test_abrupt_third_launch_enters_recoverable_cooldown(self):
        self.stamp('ready', terminal=False)
        probe, clock = self.cold(1021)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        probe, clock = self.cold(1322)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_called_once()

    def test_service_retries_after_three_start_failures(self):
        project = str(self.base / 'project')
        Path(project).mkdir()
        decision = {'projectRoot': project}
        proof = {'providerManifest': self.manifest}
        calls, attests = [], []
        def start_worker(*_args):
            calls.append(1)
            if len(calls) <= 3:
                raise RuntimeError('synthetic_ready_timeout')
            return object()
        (self.base / 'svc').mkdir(mode=0o700)
        service = svc.Service(self.base / 'svc', bundle={'version': '1.0.0', 'digest': 'a'},
                              prove=lambda _p: (decision, proof, 'selected'),
                              attest=lambda *_a: attests.append(1),
                              start_worker=start_worker, admit=lambda: True,
                              )
        key = svc._worker_key(self.manifest)
        worker = svc.Worker(key, str(self.base / 'worker.sock'))
        service.workers[key] = worker
        for now in (1000, 1011, 1032):
            with patch.object(svc.time, 'monotonic', return_value=now), \
                 patch.object(owner.time, 'time', return_value=now):
                service._load_attested(project, decision, proof, 'selected',
                                       self.manifest, worker, 'seal')
        with patch.object(svc.threading, 'Thread', InlineThread), \
             patch.object(svc.time, 'monotonic', return_value=1333), \
             patch.object(owner.time, 'time', return_value=1333):
            service._load(project, decision, proof, 'selected', self.manifest, key, 'seal')
        self.assertEqual((len(calls), len(attests), worker.state), (4, 4, 'ready'))

    def test_repeated_moved_selection_deferrals_back_off_as_start_failures(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        busy = ResourceDeferred('writer_proof_physical_busy')
        delays = [service._deferred_retry('/project', self.manifest, moved, 0)
                  for _ in range(svc.MOVED_DEFER_LIMIT + 2)]
        state = owner.read_owner_state(self.manifest)
        self.assertEqual(len(set(delays[:svc.MOVED_DEFER_LIMIT])), 1)
        self.assertGreater(delays[-1], delays[-2])
        self.assertEqual((state['reason'], state['attempts'], state['terminal']),
                         ('startup_failed', 2, False))
        self.assertEqual(service._deferred_retry('/other', self.manifest, busy, 0), delays[0])
        self.assertNotIn('/other', service.moved)

    def test_moved_deferral_budget_isolated_by_installation_socket(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT + 2):
            service._deferred_retry('/project', self.manifest, moved, 0)
        other = dict(self.manifest, serviceSocketPath=str(self.base / 'other.sock'))
        self.assertEqual(service._deferred_retry('/project', other, moved, 0), 5)
        self.assertIsNone(owner.read_owner_state(other))

    def test_other_deferral_breaks_consecutive_moved_sequence(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT):
            service._deferred_retry('/project', self.manifest, moved, 0)
        service._deferred_retry('/project', self.manifest, ResourceDeferred('writer_proof_physical_busy'), 0)
        self.assertEqual(service._deferred_retry('/project', self.manifest, moved, 0), 5)
        self.assertIsNone(owner.read_owner_state(self.manifest))

    def test_moved_deferral_preserves_prior_attempt_after_restart(self):
        self.stamp('startup_failed', terminal=False, attempts=2)
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        with patch.object(owner.time, 'time', return_value=1011):
            prior = owner._failure_decision(self.manifest)
            delay = service._deferred_retry('/project', self.manifest,
                                            ResourceDeferred('worker_selection_changed'), prior['attempts'])
        self.assertGreater(delay, 5)
        self.assertEqual(owner.read_owner_state(self.manifest)['attempts'], 3)

    def test_restart_preserves_previous_startup_failure_count(self):
        self.stamp('startup_failed', terminal=False, attempts=2)
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        class ParkedThread:
            def __init__(self, **_kwargs):
                pass
            def start(self):
                pass
        key = svc._worker_key(self.manifest)
        with patch.object(svc.threading, 'Thread', ParkedThread), \
             patch.object(owner.time, 'time', return_value=1011):
            service._load(project, {'projectRoot': project}, {}, 'selected',
                          self.manifest, key, 'seal')
        # Before proof succeeds the prior attempts belong only to this install.
        self.assertEqual(service.workers[key].failures, 0)
        self.assertEqual(owner.read_owner_state(self.manifest)['attempts'], 2)

    def test_resource_deferral_keeps_process_failures_out_of_install_budget(self):
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        decision = {'projectRoot': project}
        proof = {'providerManifest': self.manifest}
        service = svc.Service(root, bundle={'version': '1.0.0'},
                              prove=lambda _p: (decision, proof, 'selected'),
                              admit=lambda: False)
        worker = svc.Worker(svc._worker_key(self.manifest), str(self.base / 'worker.sock'))
        worker.failures = 2
        service._load_attested(project, decision, proof, 'selected', self.manifest, worker, 'seal')
        row = owner.read_owner_state(self.manifest)
        self.assertEqual((row['reason'], row['attempts'], worker.failures),
                         ('resource_deferred', 0, 2))

    def test_warm_attach_respects_existing_failure_cooldown(self):
        self.stamp('startup_failed', terminal=False, attempts=3)
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        proof = {'providerManifest': self.manifest}
        attest_calls = []
        def defer(*_args):
            attest_calls.append(1)
            raise ResourceDeferred('worker_selection_changed')
        service = svc.Service(root, bundle={'version': '1.0.0'},
                              prove=lambda _p: ({'projectRoot': project}, proof, 'selected'),
                              attest=defer)
        worker = svc.Worker(svc._worker_key(self.manifest), str(self.base / 'worker.sock'))
        worker.alive = lambda: True
        service.workers[worker.identity] = worker
        with patch.object(owner, '_owner_identity', return_value={'id': 'synthetic'}), \
             patch.object(owner.time, 'time', return_value=1001):
            service._adopt(project)
        self.assertEqual(attest_calls, [])

    def test_attest_retries_and_logs_distinct_failures_on_cold_and_warm_workers(self):
        for warm in (False, True):
            with self.subTest(warm=warm):
                root = self.base / ('warm' if warm else 'cold')
                root.mkdir(mode=0o700)
                project = str(root)
                manifest = dict(self.manifest, serviceSocketPath=str(root / 'embed.sock'))
                decision = {'projectRoot': project, 'installId': root.name,
                            'journalPath': str(root / 'install.journal')}
                proof = {'providerManifest': manifest}
                reasons = ['selected_route_changed', 'selected_route_changed',
                           'selection_transition_pending', 'io error', 'io error', 'io error',
                           'provider_artifact_hash_mismatch', 'provider_artifact_hash_mismatch']
                attest = Mock(side_effect=[IndexContractError(code) for code in reasons] + [None])
                service = svc.Service(root, bundle={'version': '1.1.1'},
                                      prove=lambda _p: (decision, proof, 'selected'),
                                      attest=attest, admit=lambda: True, start_worker=Mock())
                key = svc._worker_key(manifest)
                if warm:
                    worker = service.workers[key] = svc.Worker(key, str(root / 'worker.sock'))
                    worker.alive = lambda: True
                with patch.object(owner, '_owner_identity', return_value='fixture'), \
                     patch.object(svc.threading, 'Thread', InlineThread), \
                     patch.object(owner.time, 'time') as clock:
                    def adopt(at):
                        clock.return_value = at
                        service._adopt(project)
                        return owner.read_owner_state(manifest)

                    for now in (100, 110, 120):
                        row = adopt(now)
                        self.assertFalse(row and row['terminal'])
                        self.assertEqual(row['attempts'] if row else 0, 0)
                    for count, now in enumerate((130, 140, 150), 1):
                        row = adopt(now)
                        self.assertEqual((row['reason'], row['attempts'], row['terminal']),
                                         ('startup_failed', count, False))
                    adopt(449)
                    self.assertEqual(attest.call_count, 6)
                    row = adopt(450)
                    self.assertEqual((row['reason'], row['terminal'], row['at']),
                                     ('attestation_invalid', True, 450))
                    adopt(4049)
                    self.assertEqual(attest.call_count, 7)
                    self.assertEqual(adopt(4050)['at'], 4050)
                    adopt(7649)
                    self.assertEqual(attest.call_count, 8)
                    service.start_worker.assert_not_called()
                    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
                    self.assertEqual([(e['event'], e['install'], e['outcome'], e['reason']) for e in events],
                                     [('resident.adopt', root.name, outcome, code) for outcome, code in
                                      [('waiting', 'selected_route_changed'),
                                       ('waiting', 'selection_transition_pending'),
                                       ('unproved', 'IndexContractError'),
                                       ('unproved', 'provider_artifact_hash_mismatch')]])
                    # Re-enter the first waiting reason, then bind and start a new reporting episode.
                    service._report_proof_failure(project, reasons[0], 'waiting', decision)
                    with patch.object(service, '_accept_loop'):
                        adopt(7650)
                    self.assertEqual(attest.call_count, 9)
                    if warm:
                        self.assertEqual(owner.read_owner_state(manifest)['reason'], 'ready')
                        service._release(project, reason='starting')
                        service._attest_failed(project, decision, manifest, IndexContractError(reasons[0]), 0)
                        service._attest_failed(project, decision, manifest, IndexContractError(reasons[-1]), 0)
                        events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
                        self.assertEqual([(e['outcome'], e['reason']) for e in events[-2:]],
                                         [('waiting', reasons[0]), ('unproved', reasons[-1])])
                    else:
                        service.start_worker.assert_called_once()

    def test_artifact_read_error_on_either_launch_check_allows_retry(self):
        # The final authorization can also surface a stat error wrapped by the protected provider.
        missing = IndexContractError('provider_artifact_missing')
        missing.__cause__ = FileNotFoundError('fixture artifact')
        for stage, failure_at, error in (('preflight', 1, OSError(5, 'fixture I/O')),
                                         ('final', 3, OSError(5, 'fixture I/O')), ('stat', 3, missing)):
            with self.subTest(stage=stage):
                root = self.base / stage
                root.mkdir(mode=0o700)
                worker_path, provider_path = root / 'worker', root / 'provider'
                worker_path.write_bytes(b'worker')
                provider_path.write_bytes(b'provider')
                node, module, model = (root / name for name in ('node', 'module', 'model'))
                node.write_bytes(b'fixture node')
                module.write_bytes(b'fixture module')
                model.write_bytes(b'GGUFfixture model')
                manifest = dict(self.manifest, serviceSocketPath=str(root / 'e.sock'),
                                providerRelativePath=provider_path.name,
                                workerSha256=provider.hash_file(str(worker_path)),
                                providerArtifactSha256=provider.hash_file(str(provider_path)),
                                nodePath=str(node), nodeSha256=provider.hash_file(str(node)),
                                moduleEntryPath=str(module), moduleEntrySha256=provider.hash_file(str(module)),
                                modelPath=str(model), modelSha256=provider.hash_file(str(model)),
                                modelId=provider.default_embed_model_uri(),
                                runtimeId='qmd-fork-f4633f9-node-llama-cpp-3.20.0',
                                recipeId='qmd-query-format-single-document-v1',
                                dependencyClosureApproved=True, dependencyClosureSha256='d' * 64)
                manifest['embeddingFingerprint'] = provider.recipe_fingerprint(
                    model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
                    runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
                    provider_sha256=manifest['providerArtifactSha256'], worker_sha256=manifest['workerSha256'],
                    node_sha256=manifest['nodeSha256'], module_entry_sha256=manifest['moduleEntrySha256'],
                    closure_sha256=manifest['dependencyClosureSha256'])
                decision = {'projectRoot': str(root)}
                proof = {'providerManifest': manifest}
                attempts = []
                def start(row, admit):
                    attempts.append(None)
                    if len(attempts) == 1:
                        return owner.start_attested_worker(row, admit, execution_state=root,
                                                           worker=worker_path, provider=provider_path)
                    return object()
                service = svc.Service(root, bundle={'version': '1.1.1'},
                                      prove=lambda project: (decision, proof, 'selected'),
                                      attest=Mock(), admit=lambda: True, start_worker=start)
                key = svc._worker_key(manifest)
                digests = [manifest['workerSha256'], manifest['providerArtifactSha256']]
                reads = digests[:failure_at - 1] + [error]
                with patch.object(provider, 'hash_file', side_effect=reads) as hashed, \
                     patch.object(svc.threading, 'Thread', InlineThread), \
                     patch.object(svc.time, 'monotonic') as mono, patch.object(owner.time, 'time') as clock:
                    decision.update(installId='fixture', journalPath=str(root / 'journal'), scopeId='scope',
                                    storePath=str(root / 'store'), storeIncarnation='store-id', vectorProfile={})
                    svc.register(root, root=root)
                    for at in (100, 109, 110):
                        clock.return_value = mono.return_value = at
                        service.tick()
                        if at < 110:
                            row = owner.read_owner_state(manifest)
                            self.assertEqual((row['reason'], row['attempts'], row['terminal']),
                                             ('startup_failed', 1, False))
                            self.assertEqual(len(attempts), 1)
                            self.assertFalse(service.attested)
                    self.assertEqual((hashed.call_count, len(attempts), service.workers[key].state),
                                     (failure_at, 2, 'ready'))

    def test_reattestation_ledger_read_error_retries_and_unapproved_pair_stays_terminal(self):
        from memvane.kernel import bundle_paths
        worker = bundle_paths.bundle_file(bundle_paths.WORKER_RELATIVE_PATH)
        artifact = bundle_paths.bundle_file(bundle_paths.PROVIDER_RELATIVE_PATH)
        previous = self.base / 'previous-provider'
        previous.write_bytes(b'previous fixture adapter')
        manifest = dict(self.manifest, modelSha256='e' * 64, workerPath=str(worker), providerPath=str(artifact),
                        workerSha256=provider.hash_file(str(worker)), providerArtifactSha256=provider.hash_file(str(previous)))
        for approved in (False, True):
            with self.subTest(approved=approved):
                root = self.base / ('approved' if approved else 'unapproved')
                root.mkdir(mode=0o700)
                row = dict(manifest, serviceSocketPath=str(root / 'e.sock'))
                # Seal the old bundle, then authorize the current pair only for the approved fixture.
                def old_bundle(relative):
                    return previous if relative == bundle_paths.PROVIDER_RELATIVE_PATH else worker
                with patch.object(bundle_paths, 'bundle_file', side_effect=old_bundle):
                    provider.reattestation_update(root, row)
                if approved:
                    provider.reattestation_update(root, row)
                self.assertEqual(provider.reattestation_check(root, row), approved)
                decision = {'projectRoot': str(root)}
                proof = {'providerManifest': row}
                def start(item, admit):
                    return owner.start_attested_worker(item, admit, execution_state=root, worker=worker, provider=artifact)
                launch = Mock(side_effect=start)
                service = svc.Service(root, bundle={'version': '1.1.1'}, attest=Mock(), admit=lambda: True,
                                      prove=lambda _: (decision, proof, 'selected'), start_worker=launch)
                key = svc._worker_key(row)
                real_open = os.open
                ledger_opens = []
                def open_ledger(path, *args, **kwargs):
                    if Path(path) == root / 'provider-reattest-pairs.json':
                        ledger_opens.append(path)
                        if approved and len(ledger_opens) == 1:
                            raise OSError(errno.EIO, 'fixture ledger read')
                    return real_open(path, *args, **kwargs)
                with patch.object(os, 'open', side_effect=open_ledger), \
                     patch.object(provider, 'start_owned_embed_service', return_value=object()) as spawn, \
                     patch.object(svc.threading, 'Thread', InlineThread), \
                     patch.object(svc.time, 'monotonic') as mono, patch.object(owner.time, 'time') as clock:
                    decision.update(installId='fixture', journalPath=str(root / 'journal'), scopeId='scope',
                                    storePath=str(root / 'store'), storeIncarnation='store-id', vectorProfile={})
                    svc.register(root, root=root)
                    for at in (100, 109, 110):
                        clock.return_value = mono.return_value = at
                        service.tick()
                        if not approved or at < 110:
                            state = owner.read_owner_state(row)
                            self.assertEqual((state['reason'], state['terminal'], state['attempts'], state['at']),
                                             ('startup_failed', False, 1, 100) if approved
                                             else ('attestation_invalid', True, 3, 100))
                            self.assertEqual(launch.call_count, 1)
                            spawn.assert_not_called()
                    self.assertEqual(len(ledger_opens), 2 if approved else 1)
                    self.assertEqual((launch.call_count, spawn.call_count), (2, 1) if approved else (1, 0))
                    if approved:
                        self.assertEqual(service.workers[key].state, 'ready')

    def test_compared_artifact_hash_mismatch_remains_terminal(self):
        worker, artifact = self.base / 'worker', self.base / 'provider'
        worker.write_bytes(b'attested worker')
        artifact.write_bytes(b'attested provider')
        manifest = dict(self.manifest, workerSha256=provider.hash_file(str(worker)),
                        providerArtifactSha256=provider.hash_file(str(artifact)), modelSha256='d' * 64)
        worker.write_bytes(b'changed worker')
        decision = {'projectRoot': str(self.base)}
        proof = {'providerManifest': manifest}
        def start(row, admit):
            return owner.start_attested_worker(row, admit, execution_state=self.base,
                                               worker=worker, provider=artifact)
        service = svc.Service(self.base, bundle={'version': '1.1.1'}, attest=Mock(),
                              start_worker=start, admit=lambda: True)
        with patch.object(svc.threading, 'Thread', InlineThread), \
             patch.object(provider, 'start_owned_embed_service') as launch:
            service._load(str(self.base), decision, proof, 'selected',
                          manifest, svc._worker_key(manifest), 'seal')
        row = owner.read_owner_state(manifest)
        self.assertEqual((row['reason'], row['terminal'], row['attempts']), ('attestation_invalid', True, 3))
        launch.assert_not_called()

    def test_shared_worker_admission_preserves_other_install_terminal_deadline(self):
        a, b = [str(self.base / name) for name in ('a', 'b')]
        manifests = {}
        for project in (a, b):
            Path(project).mkdir(mode=0o700)
            svc.register(project, root=self.base)
            manifests[project] = dict(self.manifest, serviceSocketPath=str(Path(project) / 'e.sock'))
        def prove(project):
            return {'projectRoot': project}, {'providerManifest': manifests[project]}, 'selected'
        attest = Mock(side_effect=IndexContractError('provider_recipe_mismatch'))
        service = svc.Service(self.base, bundle={'version': '1.1.1'}, prove=prove, attest=attest,
                              admit=Mock(side_effect=[True, False, True]), start_worker=Mock())
        with patch.object(owner, '_owner_identity', side_effect=lambda d, p: d['projectRoot']), \
             patch.object(svc.threading, 'Thread', InlineThread), \
             patch.object(svc.time, 'monotonic') as mono, patch.object(owner.time, 'time') as clock:
            for at, project in ((100, a), (101, b), (107, a), (3699, a), (3700, a)):
                clock.return_value = mono.return_value = at
                service._adopt(project)
                row = owner.read_owner_state(manifests[a])
                self.assertEqual((row['reason'], row['terminal'], row['at'], attest.call_count),
                                 ('attestation_invalid', True, 100 if at < 3700 else 3700,
                                  1 if at < 3700 else 2))
                if project == b:
                    self.assertEqual(owner.read_owner_state(manifests[b])['reason'], 'resource_deferred')
        service.start_worker.assert_not_called()

    def test_worker_launch_rejection_also_reproves_after_one_hour(self):
        manifest = self.manifest
        project = str(self.base)
        decision = {'projectRoot': project}
        proof = {'providerManifest': manifest}
        start = Mock(side_effect=[IndexContractError('provider_recipe_mismatch'), object()])
        service = svc.Service(self.base, bundle={'version': '1.1.1'}, attest=Mock(),
                              start_worker=start, admit=lambda: True)
        key = svc._worker_key(manifest)
        with patch.object(svc.threading, 'Thread', InlineThread), patch.object(owner.time, 'time') as clock:
            for at, starts in ((100, 1), (3699, 1), (3700, 2)):
                clock.return_value = at
                service._load(project, decision, proof, 'selected', manifest, key, 'seal')
                self.assertEqual((service.attest.call_count, start.call_count), (starts, starts))
                if starts == 1:
                    row = owner.read_owner_state(manifest)
                    self.assertEqual((row['reason'], row['terminal'], row['at']),
                                     ('attestation_invalid', True, 100))
                    self.assertNotIn('seal', service.attested)
        self.assertEqual(service.workers[key].state, 'ready')
