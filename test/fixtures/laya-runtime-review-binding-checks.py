"""Deterministic runtime generation races using existing producer/review APIs.

Real private G0/G1 generations contain only synthetic runtime files. Their
ordinary JS dependency differs; node, model, module entry and native bytes
stay equal. No vector service, native executable or model is run. Download
and npm in the Machine fixture are injected local file writers.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
EMBED = runpy.run_path(str(HERE / 'laya-embed-runtime-checks.py'))
PROVISION = runpy.run_path(str(HERE / 'laya-runtime-provision-checks.py'))
R, auto = EMBED['R'], EMBED['auto']
REAL_SEALED_FILES = R.sealed_files_sha256
REAL_SELECT_GENERATION = R.select_generation
FRESH = runpy.run_path(str(HERE / 'laya-fresh-bundle-upgrade-checks.py'))


@contextmanager
def fixture(kind='MovedProvider'):
    cls = PROVISION['Machine'] if kind == 'Machine' else EMBED[kind]
    case = cls('runTest')
    actual_run = subprocess.run
    def bounded_run(*args, **kwargs):
        kwargs.setdefault('timeout', 30)
        return actual_run(*args, **kwargs)
    with mock.patch.object(subprocess, 'run', side_effect=bounded_run):
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()


def files(runtime):
    root = Path(runtime['generation'])
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()}


def pointers(home):
    return {name: (R.root(home) / name).read_bytes() for name in (R.ACTIVE, R.PREVIOUS)}


@contextmanager
def install_fixture(mode):
    """Real temp install metadata/stores, with existing synthetic authorities."""
    case = FRESH['UpgradeCase']('runTest')
    try:
        case.setUp()
        # Keep the real provider review's Unix-socket path within 100 bytes.
        # No build record or store exists yet, so all identities are made below.
        state_temp = tempfile.TemporaryDirectory(prefix='lrib-', dir='/tmp')
        case.addCleanup(state_temp.cleanup)
        short = Path(state_temp.name).resolve() / 's'
        case.state.rename(short)
        case.state = short
        case.journal_path = short / 'install-journal.sqlite'
        case.ledger = short / FRESH['fresh'].LEDGER
        case.fj.state_root = short
        case.write_attestation(case.old, FRESH['manifest'](case.old_root))
        socket_bytes = len(str(short / 'runtime' / 'laya-embed.sock').encode())
        case.assertEqual(str(short.resolve(strict=True)), str(short))
        case.assertLessEqual(socket_bytes, 100)
        print(json.dumps({'case': 'install-review-path', 'state': str(short),
                          'resolved': str(short.resolve(strict=True)),
                          'socketBytes': socket_bytes}, sort_keys=True), flush=True)
        if mode == 'inactive':
            case.register()
        else:
            case.bound()
            if mode in ('active', 'recovery'):
                case.activate()
            if mode == 'recovery':
                atomic = FRESH['active'].inactive._atomic_expected
                def stop(path, old, new):
                    if Path(path).name == FRESH['fresh'].ROUTE:
                        raise FRESH['Kill']()
                    return atomic(path, old, new)
                with mock.patch.object(FRESH['active'].inactive, '_atomic_expected', stop):
                    with case.assertRaises(FRESH['Kill']):
                        case.run_upgrade(FRESH['active'])
        yield case
    finally:
        case.doCleanups()


def attest(case, runtime, source, commit):
    reviewed = case.review(source, commit)['reviewedProvider']
    attestation = case.attestation(reviewed['providerManifest'], reviewed['dependencyClosure'])
    attestation[R.FILES_SHA] = runtime[R.FILES_SHA]
    return attestation


@contextmanager
def review_authorities(case, module, mode):
    if mode == 'inactive':
        # Inactive upgrade has its own imports; it has no checked_route adapter.
        # Stop after actual review, so no downstream CAS/provider mocks are needed.
        with mock.patch.object(module, 'inspect_journal', case.fj.inspect), \
                mock.patch.object(module, 'inspect_full_plugin_candidate',
                                  side_effect=case.inspect_bundle):
            yield
    else:
        with case.patches(module):
            yield


def provision_sources(case):
    return R._provision(node=case.node, module_entry=case.module_entry,
                        model=case.model, expect={}, home=case.home)


def pair(case, *, machine=False):
    if machine:
        g0 = case.provision()
        source, commit = case.source_repo()
        record = attest(case, g0, source, commit)
        dependency = case.registry / 'isexe/index.js'
    else:
        g0, source, commit, record = case.pinned()
        candidates = list(case.qmd_runtime.rglob('isexe/index.js'))
        case.assertEqual(len(candidates), 1)
        dependency = candidates[0]
    original = dependency.read_bytes()
    try:
        dependency.write_bytes(original + b'\n// synthetic ordinary JS dependency G1\n')
        g1 = case.provision() if machine else provision_sources(case)
    finally:
        dependency.write_bytes(original)
    case.assertNotEqual(g0['generation'], g1['generation'])
    case.assertNotEqual(g0[R.FILES_SHA], g1[R.FILES_SHA])
    for key in ('nodeSha256', 'modelSha256', 'moduleEntrySha256'):
        case.assertEqual(g0[key], g1[key], key)
    native = lambda runtime: [(row['sha256'], row['bytes'])
                              for row in runtime['dependencyClosure']]
    case.assertEqual(native(g0), native(g1))
    att1 = attest(case, g1, source, commit)
    return g0, g1, record, att1, source, commit


@contextmanager
def switch_after_unlock(case, att1):
    """Run B through actual ensure once, after A's real flock is released."""
    original = R._lock
    evidence = {'fired': False}
    @contextmanager
    def lock(runtime):
        with original(runtime):
            yield
        if not evidence['fired']:
            evidence['fired'] = True  # B's own real lock must not recurse.
            evidence['chosen'] = R.select(case.home)['generation']
            chosen_b = R.ensure_for_attestation(att1, case.home)
            evidence['bReturned'] = chosen_b['generation']
    with mock.patch.object(R, '_lock', lock):
        yield evidence


class RuntimeReviewBinding(unittest.TestCase):
    def check_cutoff(self, case, label, g0, g1, att1, operation):
        before = {g0['generation']: files(g0), g1['generation']: files(g1)}
        with switch_after_unlock(case, att1) as evidence:
            returned = operation()
        current = R.select(case.home)
        print(json.dumps({'case': label, 'chosen': evidence.get('chosen'),
                          'expected': g0['generation'], 'returned': returned['generation'],
                          'active': current['generation'], 'g0Files': g0[R.FILES_SHA],
                          'g1Files': g1[R.FILES_SHA]}, sort_keys=True), flush=True)
        self.assertTrue(evidence['fired'])
        self.assertEqual(evidence['chosen'], g0['generation'])
        self.assertEqual(evidence['bReturned'], g1['generation'])
        self.assertEqual(current['generation'], g1['generation'])
        self.assertEqual(files(g0), before[g0['generation']])
        self.assertEqual(files(g1), before[g1['generation']])
        self.assertEqual(returned['generation'], g0['generation'])
        self.assertEqual(returned[R.FILES_SHA], g0[R.FILES_SHA])

    def test_owned_ensure_returns_the_generation_it_reactivated(self):
        with fixture() as case:
            g0, g1, record, att1, _source, _commit = pair(case)
            self.check_cutoff(case, 'owned-ensure-reactivate', g0, g1, att1,
                              lambda: R.ensure_for_attestation(record, case.home))

    def test_provision_returns_the_generation_it_sealed(self):
        with fixture() as case:
            g0, g1, _record, att1, _source, _commit = pair(case)
            self.check_cutoff(case, 'provision', g0, g1, att1,
                              lambda: provision_sources(case))

    def test_new_machine_provision_returns_the_generation_it_sealed(self):
        with fixture('Machine') as case:
            g0, g1, _record, att1, _source, _commit = pair(case, machine=True)
            self.check_cutoff(case, 'provision-new', g0, g1, att1, case.provision)


    def test_upgrade_real_review_keeps_the_generation_returned_by_ensure(self):
        with fixture() as case:
            g0, g1, record, att1, source, commit = pair(case)
            before = {g0['generation']: files(g0), g1['generation']: files(g1)}
            host = case.home / '.claude/plugins/cache/mkt/qmd-auto-context/9.9.9'
            shutil.copytree(case.plugin, host)
            R.ensure_for_attestation(record, case.home)
            baseline = case.review_at(host, source_root=str(source), plugin_root=str(host),
                                      state_root=str(case.state), commit_id=commit,
                                      dimension=EMBED['DIM'])
            R.ensure_for_attestation(att1, case.home)
            actual_ensure, actual_review_at = auto._ensure_embed_runtime, case.review_at
            evidence, proposals, calls = {}, [], []
            def ensure_then_switch(attestation):
                selected = actual_ensure(attestation)
                evidence['ensured'] = selected['generation']
                R.ensure_for_attestation(att1, case.home)
                return selected
            def review_at(*args, **kwargs):
                proposal = actual_review_at(*args, **kwargs)
                proposals.append(proposal)
                return proposal
            error, result = None, None
            with mock.patch.object(auto, '_ensure_embed_runtime', ensure_then_switch), \
                    mock.patch.object(case, 'review_at', side_effect=review_at):
                try:
                    result = case.upgrade_with(record, source, commit, host, calls)
                except auto.UpgradeRefused as exc:
                    error = exc
            proposal = proposals[-1] if proposals else None
            module = proposal['reviewedProvider']['providerManifest']['moduleEntryPath'] if proposal else None
            print(json.dumps({'case': 'ensure-then-review', 'chosen': g0['generation'],
                              'returned': evidence.get('ensured'),
                              'active': R.select(case.home)['generation'],
                              'proposalGeneration': proposal.get('embedRuntimeGeneration') if proposal else None,
                              'proposalModule': module,
                              'proposalFiles': R.sealed_files_sha256(module) if module else None,
                              'proposalSha256': proposal.get('proposalSha256') if proposal else None,
                              'error': str(error) if error is not None else None}, sort_keys=True), flush=True)
            self.assertEqual(evidence['ensured'], g0['generation'])
            self.assertEqual(R.select(case.home)['generation'], g1['generation'])
            self.assertEqual(files(g0), before[g0['generation']])
            self.assertEqual(files(g1), before[g1['generation']])
            self.assertIsNone(error)
            self.assertEqual(result['status'], 'active_upgrade_complete')
            self.assertEqual(len(calls), 1)
            self.assertEqual(proposal, baseline)
            self.assertEqual(proposal['embedRuntimeGeneration'], g0['generation'])
            self.assertEqual(R.sealed_files_sha256(module), g0[R.FILES_SHA])

    def test_named_generation_rechecks_files_without_reading_or_changing_pointers(self):
        with fixture() as case:
            g0, g1, _record, _att1, _source, _commit = pair(case)
            before, before_g0, before_g1 = pointers(case.home), files(g0), files(g1)
            with mock.patch.object(R, '_pointer', side_effect=AssertionError('pointer read')):
                named = R.select_generation(g0['generation'], case.home)
            self.assertEqual(named, g0)
            self.assertEqual(pointers(case.home), before)
            self.assertEqual(R.select(case.home), g1)
            self.assertEqual(files(g0), before_g0)
            self.assertEqual(files(g1), before_g1)

    def test_named_missing_corrupt_outside_and_symlink_refuse_without_pointer_changes(self):
        with fixture() as case:
            g0, g1, _record, _att1, source, commit = pair(case)
            before, sealed_g0, sealed_g1 = pointers(case.home), files(g0), files(g1)
            outside = case.base / 'outside' / Path(g0['generation']).name
            shutil.copytree(g0['generation'], outside)
            alias = R.root(case.home) / 'generations' / ('g-' + 'f' * 32)
            alias.symlink_to(outside, target_is_directory=True)
            traversal = str(R.root(case.home) / 'generations') + '/../generations/' + Path(g0['generation']).name
            for name, value in (('relative', Path(g0['generation']).name),
                                ('traversal', traversal), ('outside', str(outside)),
                                ('symlink', str(alias)), ('runtime-dict', g0)):
                with self.subTest(name=name), \
                        mock.patch.object(R, '_read_json', side_effect=AssertionError('metadata read')):
                    with self.assertRaises(R.RuntimeUnavailable):
                        R.select_generation(value, case.home)
                with self.assertRaises(EMBED['product'].InstallBlocked):
                    case.review(source, commit, runtime_generation=value)
                self.assertEqual(pointers(case.home), before)
            alias.unlink()
            with self.assertRaises(R.RuntimeUnavailable):
                R.select_generation(str(alias), case.home)
            with self.assertRaises(EMBED['product'].InstallBlocked):
                case.review(source, commit, runtime_generation=str(alias))
            dependency = next(Path(g0['generation']).rglob('isexe/index.js'))
            original = dependency.read_bytes()
            try:
                dependency.write_bytes(original + b'\n// corrupt sealed dependency\n')
                with self.assertRaises(R.RuntimeUnavailable):
                    R.select_generation(g0['generation'], case.home)
                with self.assertRaises(EMBED['product'].InstallBlocked):
                    case.review(source, commit, runtime_generation=g0['generation'])
            finally:
                dependency.write_bytes(original)
            self.assertEqual(pointers(case.home), before)
            self.assertEqual(R.select(case.home), g1)
            self.assertEqual(files(g0), sealed_g0)
            self.assertEqual(files(g1), sealed_g1)

    def test_active_bound_inactive_and_recovery_repeat_real_review_on_named_generation(self):
        class Reviewed(BaseException):
            pass
        for mode in ('active', 'bound', 'inactive', 'recovery'):
            with self.subTest(mode=mode), fixture() as runtime_case:
                g0, g1, _record, _att1, source, commit = pair(runtime_case)
                before = pointers(runtime_case.home)
                with install_fixture(mode) as install:
                    root = install.home / 'review-host'
                    shutil.copytree(runtime_case.plugin, root)
                    module = (FRESH['bound_upgrade'] if mode == 'bound' else
                              FRESH['active'].inactive if mode == 'inactive' else FRESH['active'])
                    args = dict(project_root=str(install.project), state_root=str(install.state),
                                source_root=str(source), plugin_root=str(root), commit_id=commit,
                                model_path=None, dimension=3, runtime_generation=g0['generation'])
                    proposals = []
                    named_selections = []
                    def actual_named_generation(*args, **kwargs):
                        runtime = REAL_SELECT_GENERATION(*args, **kwargs)
                        named_selections.append({'generation': runtime['generation'],
                                                 R.FILES_SHA: runtime[R.FILES_SHA]})
                        return runtime
                    with mock.patch.dict(os.environ, {'HOME': str(runtime_case.home)}), \
                            mock.patch('platform.system', return_value='Darwin'), \
                            mock.patch('platform.machine', return_value='arm64'), \
                            mock.patch.object(R, 'sealed_files_sha256', REAL_SEALED_FILES), \
                            mock.patch.object(R, 'select_generation', actual_named_generation):
                        baseline = EMBED['REAL_REVIEW'](**{key: value for key, value in args.items()
                                                         if key != 'project_root'})
                        def actual_review(**kwargs):
                            proposal = EMBED['REAL_REVIEW'](**kwargs)
                            proposals.append(proposal)
                            raise Reviewed()
                        with review_authorities(install, module, mode), \
                                mock.patch.object(EMBED['onboarding'], 'review_local_provider', actual_review), \
                                mock.patch.object(FRESH['active'], 'review_local_provider', actual_review):
                            with self.assertRaises(Reviewed):
                                module.upgrade(**args, accepted_provider_sha256=baseline['proposalSha256'])
                    self.assertEqual(len(proposals), 1)
                    actual = proposals[0]
                    actual_module = actual['reviewedProvider']['providerManifest']['moduleEntryPath']
                    print(json.dumps({'case': 'repeat-review-' + mode,
                                      'chosen': g0['generation'],
                                      'active': R.select(runtime_case.home)['generation'],
                                      'proposalGeneration': actual['embedRuntimeGeneration'],
                                      'proposalModule': actual_module,
                                      'proposalFiles': REAL_SEALED_FILES(actual_module),
                                      'reviewRuntimeFiles': [row[R.FILES_SHA] for row in named_selections],
                                      'proposalSha256': actual['proposalSha256']}, sort_keys=True), flush=True)
                    self.assertEqual(named_selections,
                                     [{'generation': g0['generation'], R.FILES_SHA: g0[R.FILES_SHA]}] * 2)
                    self.assertEqual(actual, baseline)
                    self.assertEqual(actual['embedRuntimeGeneration'], g0['generation'])
                    self.assertEqual(REAL_SEALED_FILES(actual_module), g0[R.FILES_SHA])
                    self.assertEqual(R.select(runtime_case.home), g1)
                    self.assertEqual(pointers(runtime_case.home), before)


if __name__ == '__main__':
    unittest.main()
