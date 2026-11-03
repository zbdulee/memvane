#!/usr/bin/env python3
"""Actual released writer entrypoints against disposable published v4/v5.

Only bundle/executing-copy/provider-file identity uses the existing synthetic
authority. Historical code is extracted locally at test runtime, never fetched.
Sentinels stop before provider/review/lifecycle work; they are not CLI success.
"""
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('legacy_entry_recovery', HERE / 'laya-journal-reader-recovery-checks.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
base = recovery.base
OLD_PLUGIN_ID_COMMIT = 'f9c33094e30d523415fe983ec7f3b4c35d5a21c0'

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


ADAPTER_CHILD = CHILD.split('module_name = dict(', 1)[0] + r'''
sys.path.insert(0, str(core.parent))
from laya_index_host_route import select_route
baseline = None
if entry == 'hermes' and p['version'] == 4:
    baseline = verify(select_route(p['project'], action='recall')).get('productRouteAllowed')
    assert baseline is True, 'actual released public full proof failed'
selectors = []
if entry == 'hermes':
    import hermes_adapter.core_bridge as adapter
    import laya_host_response as response
    assert pathlib.Path(adapter.__file__).resolve().parent == core.parent / 'hermes_adapter'
    # Serving is the bounded ingress sentinel; classification and reselect
    # remain actual, including the real historical selector subprocess.
    response.serve_selected = stop('hermes-serve-selected')
    trace_root = pathlib.Path(p['selectorTraceRoot'])
    trace_result = trace_root / 'observed.json'
    actual_run = adapter.subprocess.run
    def observe_run(argv, *args, **kwargs):
        # The real selector CLI still executes its actual argv; Python's
        # startup observer only counts SQLite statements, never returns proof.
        child_env = dict(kwargs.get('env', os.environ))
        child_env['PYTHONPATH'] = str(trace_root)
        child_env['LEGACY_SELECTOR_SQL_TRACE'] = str(trace_result)
        kwargs['env'] = child_env
        result = actual_run(argv, *args, **kwargs)
        selectors.append(dict(argv=[str(value) for value in argv],
            returncode=result.returncode, stdout=result.stdout[:4096], stderr=result.stderr[:4096],
            sqlTrace=json.loads(trace_result.read_text())))
        return result
    adapter.subprocess.run = observe_run
    assert not adapter._is_noop_env(), 'sandbox/headless no-op is not adapter evidence'
    try:
        answer = adapter.recall_context(user_message='otter', cwd=p['project'])
        outcome = dict(kind='returned', result=answer)
    except Boundary as exc:
        outcome = dict(kind='sentinel', reason=str(exc))
    module_path = str(adapter.__file__)
else:
    # This is the actual old-id reader boundary used by OLD_PROBE, not a
    # version-number substitute or a current reader impersonating old code.
    import laya_index_host_route as route
    project, auto = pathlib.Path(p['project']), pathlib.Path(p['auto'])
    decision = route._classify_record(project, auto, auto / route.RECORD, project)
    try:
        J._route((auto / route.RECORD).read_bytes(), pathlib.Path(p['state']) / 'install-journal.sqlite')
        staged = 'accepted'
    except Exception as exc:
        staged = str(exc)
    outcome = dict(kind='old-id-reader', state=decision['state'],
                   reason=decision.get('reason'), staged=staged)
    module_path = str(route.__file__)
print(json.dumps(dict(entry=entry, version=p['version'], releasedModule=module_path,
    actualReleasedCommit=p['commit'], baselineFullProof=baseline, stats=stats,
    reasons=reasons, selectorSubprocesses=selectors, outcome=outcome,
    actualHostExecuted=False), sort_keys=True))
'''


OLD_ID_CHILD = CHILD.split("proof = p['proof']", 1)[0] + r'''
import laya_index_host_route as route
assert pathlib.Path(route.__file__).resolve().parent == core
project, auto = pathlib.Path(p['project']), pathlib.Path(p['auto'])
decision = route._classify_record(project, auto, auto / route.RECORD, project)
public_route = route.select_route(str(project), action='recall')
proof = p['proof']
plugin = pathlib.Path(p['plugin'])
J._inspect_identity = lambda *a, **k: proof
J._verified_executing_root = lambda *a, **k: str(plugin)
J._provider_file_identity = lambda *a, **k: [[str(plugin / 'p.py'), 1, 2, 3, 4, 5]]
def inspect_owned(root):
    if pathlib.Path(root) != plugin: raise bundle.BundleEvidenceError('fixture_bundle_not_owned')
    return proof
J.inspect_full_plugin_candidate = bundle.inspect_full_plugin_candidate = inspect_owned
try:
    verified = J.verify_selected_route(decision)
    staged = verified.get('productRouteAllowed')
except Exception as exc:
    staged = str(exc)
journal_read = None
if p['version'] == 5:
    try:
        J.inspect_journal(pathlib.Path(p['state']) / 'install-journal.sqlite')
        journal_read = 'accepted'
    except Exception as exc:
        journal_read = str(exc)
print(json.dumps(dict(entry=entry, version=p['version'], releasedModule=str(route.__file__),
    actualReleasedCommit=p['commit'], stats=stats, actualHostExecuted=False,
    outcome=dict(state=public_route['state'], recordState=decision['state'],
        reason=public_route.get('reason'), fullProof=staged, journalRead=journal_read)), sort_keys=True))
'''


class LegacyEntryChecks(unittest.TestCase):
    def extract_tracked(self, commit, root, *prefixes):
        """Local archive only; bounded regular tracked files, no link extraction."""
        result = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar',
            commit, '--', *prefixes], capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, 'local historical archive unavailable: ' +
                         result.stderr.decode('utf8', 'replace'))
        self.assertLessEqual(len(result.stdout), 64 * 1024 * 1024)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            members = archive.getmembers()
            self.assertLessEqual(len(members), 4096)
            for member in members:
                rel = PurePosixPath(member.name)
                self.assertFalse(rel.is_absolute())
                self.assertNotIn('..', rel.parts)
                self.assertIn(rel.parts[0], prefixes)
                self.assertTrue(member.isdir() or member.isfile(), 'historical links refused')
                destination = root.joinpath(*rel.parts)
                if member.isdir():
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    self.assertLessEqual(member.size, 4 * 1024 * 1024)
                    self.assertFalse(destination.exists(), 'archive must not overwrite fixture data')
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        destination.write_bytes(source.read())
                    destination.chmod(0o600)

    def check_adapter(self, entry, version):
        helper = base.JournalReaderChecks('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        case = helper.converted()[0] if version == 5 else helper.published_case()
        if entry == 'hermes':
            old_root = recovery.RecoveryChecks('runTest').old_tree(case)
            commit = recovery.READER_BRIDGE_FIXTURE_COMMIT
            self.extract_tracked(commit, old_root, 'hermes_adapter')
            for name in ('__init__.py', 'core_bridge.py', 'plugin.py'):
                self.assertTrue((old_root / 'hermes_adapter' / name).is_file())
        else:
            commit = OLD_PLUGIN_ID_COMMIT
            old_root = case.home / 'released-old-plugin-id'
            self.extract_tracked(commit, old_root, 'core')
        payload = case.home / 'adapter-entry-payload.json'
        trace_root = case.home / 'selector-sql-observer'
        trace_root.mkdir(mode=0o700)
        startup = trace_root / 'sitecustomize.py'
        startup.write_text('''import atexit, json, os, sqlite3
stats = {'fileDML': 0, 'memorySchemaStatements': 0, 'connections': 0}
connect = sqlite3.connect
def traced(*args, **kwargs):
    db = connect(*args, **kwargs)
    stats['connections'] += 1
    target = str(args[0] if args else kwargs['database'])
    def observe(sql):
        word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
        if word in ('INSERT','UPDATE','DELETE','REPLACE','CREATE','DROP','ALTER','VACUUM'):
            stats['memorySchemaStatements' if target == ':memory:' else 'fileDML'] += 1
    db.set_trace_callback(observe)
    return db
sqlite3.connect = traced
def finish():
    fd = os.open(os.environ['LEGACY_SELECTOR_SQL_TRACE'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump(stats, stream)
atexit.register(finish)
''', encoding='utf8')
        startup.chmod(0o600)
        payload.write_text(json.dumps(dict(project=str(case.project), auto=str(case.auto),
            state=str(case.state), plugin=str(case.plugin), proof=case.proof,
            version=version, commit=commit, selectorTraceRoot=str(trace_root))), encoding='utf8')
        payload.chmod(0o600)
        paths = [p for root in (case.project, case.state) for p in root.rglob('*')
                 if p.is_file() and not p.is_symlink() and
                 (p.suffix in ('.sqlite', '.json', '.jsonl', '.md') or p == case.journal)]
        before = {str(path): path.read_bytes() for path in paths}
        env = {**os.environ, 'HOME': str(case.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        for name in ('QMD_SANDBOX', 'HERMES_SANDBOX', 'CLAUDE_SANDBOX', 'CODEX_SANDBOX',
                     'GEMINI_SANDBOX', 'HERMES_HEADLESS', 'CLAUDE_HEADLESS', 'MEMVANE_BRIDGE_PREFLIGHT'):
            env.pop(name, None)
        # Only this owned child clears dispatcher sandbox; fixture guard and
        # isolated runtime remain set. No actual host CLI or provider runs.
        script = ADAPTER_CHILD if entry == 'hermes' else OLD_ID_CHILD
        result = subprocess.run([sys.executable, '-B', '-I', '-c', script,
            str(old_root / 'core'), str(payload), entry], capture_output=True,
            encoding='utf8', timeout=30, cwd=str(case.home), env=env)
        self.assertEqual(result.returncode, 0, 'adapter child setup failed: ' + result.stderr)
        answer = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(answer, sort_keys=True), flush=True)
        self.assertEqual({str(path): path.read_bytes() for path in paths}, before)
        for name in ('provider', 'queued', 'wake', 'dml'):
            self.assertEqual(answer['stats'][name], 0, answer)
        if entry == 'hermes':
            selectors = answer['selectorSubprocesses']
            self.assertEqual(len(selectors), 1, answer)
            self.assertEqual(selectors[0]['returncode'], 0, answer)
            self.assertEqual(selectors[0]['sqlTrace']['fileDML'], 0, answer)
            self.assertEqual(Path(selectors[0]['argv'][1]), old_root / 'core/laya_index_host_route.py')
            if version == 4:
                self.assertTrue(answer['baselineFullProof'], answer)
                self.assertEqual(selectors[0]['stdout'].strip(), 'candidate', answer)
                self.assertEqual(answer['outcome']['kind'], 'sentinel', answer)
                self.assertEqual(answer['stats']['sentinel'], 'hermes-serve-selected', answer)
            else:
                self.assertEqual(selectors[0]['stdout'].strip(), 'blocked', answer)
                self.assertIsNone(answer['stats']['sentinel'], answer)
                self.assertEqual(answer['outcome']['kind'], 'returned', answer)
                self.assertIn('request refused', answer['outcome']['result']['context'], answer)
        else:
            self.assertEqual(answer['outcome']['recordState'], 'candidate', answer)
            if version == 4:
                self.assertIs(answer['outcome']['fullProof'], True, answer)
            else:
                self.assertEqual(answer['outcome']['state'], 'blocked', answer)
                self.assertEqual(answer['outcome']['fullProof'], 'host_route_unverified', answer)
                self.assertEqual(answer['outcome']['journalRead'], 'journal_schema_unknown', answer)
            self.assertIsNone(answer['stats']['sentinel'], answer)

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

    def test_released_hermes_v4_actual_selector_reaches_serve_boundary(self):
        self.check_adapter('hermes', 4)

    def test_released_hermes_v5_actual_selector_blocks_without_serve(self):
        self.check_adapter('hermes', 5)

    def test_old_plugin_id_v4_public_full_proof_control(self):
        self.check_adapter('old-id', 4)

    def test_old_plugin_id_refuses_current_v5_route(self):
        self.check_adapter('old-id', 5)


if __name__ == '__main__':
    unittest.main()
