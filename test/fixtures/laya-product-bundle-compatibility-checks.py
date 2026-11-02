"""Inspect an actual historical engine bundle without rewriting its evidence.

Only disposable fixture source/install paths are changed. The old producer
is loaded from the byte-preserved 463e029 source snapshot, not a copied manifest or a
mock validator. The journal boundary is its real identity inspection API;
this fixture does not claim a SQLite activation or model execution.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
BASE = runpy.run_path(str(HERE / 'laya-product-bundle-checks.py'))
bundle, journal = BASE['bundle'], BASE['journal']


@contextmanager
def fixture():
    case = BASE['ProductBundleChecks']('runTest')
    original_run = subprocess.run
    def bounded_run(*args, **kwargs):
        kwargs.setdefault('timeout', 30)
        return original_run(*args, **kwargs)
    with mock.patch.object(subprocess, 'run', side_effect=bounded_run), \
            mock.patch.dict(os.environ, {'GIT_ALLOW_PROTOCOL': 'file',
                                        'QMD_SANDBOX': '1', 'QMD_SETUP_GUARD_FIXTURE': '1'}):
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()


def snapshot(root):
    rows = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            info = path.stat()
            rows[str(path.relative_to(root))] = (
                path.read_bytes(), info.st_mode, info.st_dev, info.st_ino,
                info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
    return rows


class HistoricalEngineCompatibility(unittest.TestCase):
    def test_current_engine_imports_and_git_admission_are_self_contained(self):
        with fixture() as case:
            engine = case.install / 'current-engine'
            proof = bundle.prepare_product_candidate(case.source, engine, commit_id=case.commit1)
            self.assertEqual(bundle.inspect_product_candidate(engine), proof)
            before = snapshot(engine)
            cwd = case.root / 'isolated-child'
            cwd.mkdir(mode=0o700)
            script = '''
import json, pathlib, sys
core = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(core))
import laya_product_bundle as bundle
import laya_package as package
assert pathlib.Path(bundle.__file__).parent == core
assert pathlib.Path(package.__file__).parent == core
assert 'git_no_lazy' not in sys.modules
commit = bundle._git(pathlib.Path(sys.argv[2]), 'rev-parse', 'HEAD').decode().strip()
assert commit == sys.argv[3]
print(json.dumps({'bundleModule': bundle.__file__, 'packageModule': package.__file__,
                  'commit': commit}, sort_keys=True))
'''
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            env.pop('PYTHONHOME', None)
            result = subprocess.run((sys.executable, '-B', '-I', '-c', script,
                                     str(engine / 'core'), str(case.source), case.commit1),
                                    cwd=cwd, env=env, encoding='utf-8',
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=30, check=False)
            print(result.stdout, end='', flush=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence['commit'], case.commit1)
            self.assertEqual(snapshot(engine), before)
            self.assertEqual(bundle.inspect_product_candidate(engine), proof)

    def test_historical_prepared_bundle_and_journal_identity_keep_their_original_proof(self):
        with fixture() as case:
            # Exact bytes of 463e029:core/laya_product_bundle.py. Checked-in
            # source keeps this actual API executable in a shallow CI clone.
            module_path = HERE / 'legacy-product-bundle-463e029.py'
            historical = module_path.read_bytes()
            old = runpy.run_path(str(module_path))
            self.assertNotIn('git_no_lazy.py', old['ARTIFACTS'])
            # The synthetic source commit contains the actual historical
            # product module, so the old producer packages its own version.
            (case.source / 'core/laya_product_bundle.py').write_bytes(historical)
            BASE['_git'](case.source, 'add', 'core/laya_product_bundle.py')
            BASE['_git'](case.source, '-c', 'user.name=Historical Fixture',
                         '-c', 'user.email=fixture@example.invalid',
                         '-c', 'commit.gpgsign=false', 'commit', '-qm', 'historical bundle producer')
            commit = BASE['_git'](case.source, 'rev-parse', 'HEAD')
            engine = case.install / 'historical-engine'
            original = old['prepare_product_candidate'](case.source, engine, commit_id=commit)
            self.assertEqual(old['inspect_product_candidate'](engine), original)
            self.assertNotIn('git_no_lazy.py', original['includedModules'])
            candidate_before = snapshot(engine)
            source_before = snapshot(case.source / 'core')
            identity = {'bundleKind': 'engine_subbundle', 'pluginRoot': str(engine),
                        'sourceCommit': original['sourceCommit'],
                        'bundleManifestSha256': original['bundleManifestSha256'],
                        'ownershipManifestSha256': original['ownershipManifestSha256'],
                        'vectorProfile': None, 'installId': '3' * 32,
                        'generation': 'g-' + '4' * 32}
            boundaries = (
                ('current-bundle-inspection', lambda: bundle.inspect_product_candidate(engine)),
                ('current-journal-identity', lambda: journal._inspect_identity(identity)))
            for name, inspect in boundaries:
                with self.subTest(boundary=name):
                    result, error = None, None
                    try:
                        result = inspect()
                    except bundle.BundleEvidenceError as exc:
                        error = exc
                    print(json.dumps({'case': name, 'producerRevision': '463e029',
                                      'bundleManifestSha256': original['bundleManifestSha256'],
                                      'ownershipManifestSha256': original['ownershipManifestSha256'],
                                      'error': str(error) if error is not None else None},
                                     sort_keys=True), flush=True)
                    self.assertEqual(snapshot(engine), candidate_before)
                    self.assertEqual(snapshot(case.source / 'core'), source_before)
                    self.assertIsNone(error)
                    self.assertEqual(result, original)


if __name__ == '__main__':
    unittest.main()
