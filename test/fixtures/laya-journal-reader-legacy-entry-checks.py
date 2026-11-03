#!/usr/bin/env python3
"""Actual released writer entrypoints against disposable published v4/v5.

Only bundle/executing-copy/provider-file identity uses the existing synthetic
authority. Historical code is extracted locally at test runtime, never fetched.
Sentinels stop before provider/review/lifecycle work; they are not CLI success.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('legacy_entry_recovery', HERE / 'laya-journal-reader-recovery-checks.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
base = recovery.base

CHILD = r'''
import importlib, json, os, pathlib, sqlite3, sys
from unittest import mock
core, payload_path, entry = sys.argv[1:]
core = pathlib.Path(core).resolve()
sys.path.insert(0, str(core))
p = json.loads(pathlib.Path(payload_path).read_text())
import laya_install_journal as J
import laya_product_bundle as bundle
assert pathlib.Path(J.__file__).resolve().parent == core
stats = dict(provider=0, queued=0, wake=0, dml=0, memorySchemaStatements=0, proofPassed=0,
             proofAttempts=0, inspectAttempts=0, sentinel=None)
reasons = []
class Boundary(BaseException): pass
def stop(name):
    def hit(*args, **kwargs):
        stats['sentinel'] = name
        raise Boundary(name)
    return hit
def forbidden(name):
    def hit(*args, **kwargs):
        stats[name] += 1
        raise AssertionError('forbidden side effect: ' + name)
    return hit
connect = sqlite3.connect
def traced(*args, **kwargs):
    db = connect(*args, **kwargs)
    target = str(args[0] if args else kwargs['database'])
    def observe(sql):
        word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        if word in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER', 'VACUUM'):
            if target == ':memory:':
                stats['memorySchemaStatements'] += 1
            else:
                stats['dml'] += 1
    db.set_trace_callback(observe)
    return db
sqlite3.connect = traced
proof = p['proof']
plugin = pathlib.Path(p['plugin'])
def inspect_bundle(root):
    if pathlib.Path(root) != plugin: raise bundle.BundleEvidenceError('fixture_bundle_not_owned')
    return proof
J._inspect_identity = lambda *a, **k: proof
J._verified_executing_root = lambda *a, **k: str(plugin)
J._provider_file_identity = lambda *a, **k: [[str(plugin / 'p.py'), 1, 2, 3, 4, 5]]
J.inspect_full_plugin_candidate = bundle.inspect_full_plugin_candidate = inspect_bundle
actual_verify = J.verify_selected_route_for_write
def verify(*a, **k):
    stats['proofAttempts'] += 1
    try: result = actual_verify(*a, **k)
    except Exception as exc:
        reasons.append(str(exc)); raise
    stats['proofPassed'] += 1
    return result
J.verify_selected_route_for_write = verify
actual_inspect = J.inspect_journal
def inspect(*a, **k):
    stats['inspectAttempts'] += 1
    try: return actual_inspect(*a, **k)
    except Exception as exc:
        reasons.append(str(exc)); raise
J.inspect_journal = inspect
import runtime_work
runtime_work.request = forbidden('wake')
import laya_local_embed_provider as provider
provider.ReviewedLocalQmdProvider = forbidden('provider')
import laya_local_embed_service as service
service.start_owned_embed_service = forbidden('provider')
import laya_index_sync as sync
for name in ('sync_full', 'sync_step', 'prepare_candidate_batch_off_hook'):
    if hasattr(sync, name): setattr(sync, name, forbidden('queued'))
module_name = dict(sync='laya_product_sync_worker', compile='wiki_selected_laya', upgrade='laya_active_upgrade')[entry]
module = importlib.import_module(module_name)
assert pathlib.Path(module.__file__).resolve().parent == core
# A v4 control proves the actual public verifier before attempting its entry.
# A v5 negative enters the writer itself, without this preliminary rejection.
baseline = None
if p['version'] == 4:
    from laya_index_host_route import select_route
    decision = select_route(p['project'], action='compile' if entry == 'compile' else 'update')
    baseline = verify(decision).get('productRouteAllowed')
    assert baseline is True, 'fixture did not pass the real released full proof'
if entry == 'sync':
    module.ReviewedLocalQmdProvider = stop('provider-construction')
elif entry == 'compile':
    import wiki_lifecycle_request
    wiki_lifecycle_request.claim = stop('lifecycle-claim')
else:
    # The review boundary follows actual _old_state/inspect_journal/build.read.
    # Provider-lane and install/operation flock primitives remain real.
    module.review_local_provider = stop('provider-review')
    if hasattr(module, 'inspect_full_plugin_candidate'):
        module.inspect_full_plugin_candidate = inspect_bundle
try:
    if entry == 'upgrade':
        result = module.upgrade(project_root=p['project'], state_root=p['state'],
            source_root=p['project'], plugin_root=p['plugin'], commit_id=proof['sourceCommit'],
            accepted_provider_sha256='b' * 64, model_path=str(plugin / 'model.gguf'), dimension=3)
    else:
        result = module.run(pathlib.Path(p['project']))
    outcome = dict(kind='returned', result=result)
except Boundary as exc:
    outcome = dict(kind='sentinel', reason=str(exc))
except Exception as exc:
    outcome = dict(kind='refused', errorType=type(exc).__name__, reason=str(exc))
print(json.dumps(dict(entry=entry, version=p['version'], releasedModule=str(module.__file__),
    actualReleasedCommit=p['commit'], baselineFullProof=baseline,
    stats=stats, reasons=reasons, outcome=outcome, actualHostExecuted=False), sort_keys=True))
'''


class LegacyEntryChecks(unittest.TestCase):
    def check_entry(self, entry, version):
        helper = base.JournalReaderChecks('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        if version == 5:
            case = helper.converted()[0]
        else:
            case = helper.published_case()
        historic = recovery.RecoveryChecks('runTest')
        old_root = historic.old_tree(case)
        payload = case.home / 'legacy-entry-payload.json'
        payload.write_text(json.dumps(dict(project=str(case.project), state=str(case.state),
            plugin=str(case.plugin), proof=case.proof, version=version,
            commit=recovery.READER_BRIDGE_FIXTURE_COMMIT)), encoding='utf8')
        payload.chmod(0o600)
        # Snapshot actual stores/corpus/metadata/journal, not only selection rows.
        paths = [p for root in (case.project, case.state) for p in root.rglob('*')
                 if p.is_file() and not p.is_symlink() and
                 (p.suffix in ('.sqlite', '.json', '.jsonl', '.md') or p == case.journal)]
        before = {str(p): p.read_bytes() for p in paths}
        result = subprocess.run([sys.executable, '-B', '-I', '-c', CHILD,
            str(old_root / 'core'), str(payload), entry], capture_output=True,
            encoding='utf8', timeout=35, cwd=str(case.home), env={**os.environ,
            'HOME': str(case.home), 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, 'historical child setup failed: ' + result.stderr)
        answer = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(answer, sort_keys=True), flush=True)
        self.assertEqual({str(p): p.read_bytes() for p in paths}, before)
        for name in ('provider', 'queued', 'wake', 'dml'):
            self.assertEqual(answer['stats'][name], 0, answer)
        if version == 4:
            self.assertTrue(answer['baselineFullProof'], answer)
            self.assertEqual(answer['outcome']['kind'], 'sentinel', answer)
            self.assertIsNotNone(answer['stats']['sentinel'], answer)
            if entry != 'upgrade':
                self.assertGreaterEqual(answer['stats']['proofPassed'], 2, answer)
        else:
            self.assertEqual(answer['outcome']['kind'], 'refused', answer)
            self.assertIsNone(answer['stats']['sentinel'], answer)
            self.assertTrue(answer['outcome']['reason'], answer)
            self.assertIn('journal_schema_unknown', answer['reasons'], answer)
            self.assertEqual(answer['stats']['proofPassed'], 0, answer)
            self.assertTrue(answer['stats']['proofAttempts'] or answer['stats']['inspectAttempts'], answer)

    def test_released_sync_v4_full_proof_reaches_provider_boundary(self):
        self.check_entry('sync', 4)

    def test_released_sync_v5_refuses_before_any_writer_side_effect(self):
        self.check_entry('sync', 5)

    def test_released_compile_v4_full_proof_reaches_lifecycle_boundary(self):
        self.check_entry('compile', 4)

    def test_released_compile_v5_refuses_before_any_writer_side_effect(self):
        self.check_entry('compile', 5)

    def test_released_active_upgrade_v4_reaches_review_boundary(self):
        self.check_entry('upgrade', 4)

    def test_released_active_upgrade_v5_refuses_before_any_writer_side_effect(self):
        self.check_entry('upgrade', 5)


if __name__ == '__main__':
    unittest.main()
