"""Kill a synthetic migration after its blob commit, before cursor update."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
from unittest import mock

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'core'))
sys.path.insert(0, str(root / 'test/fixtures'))
payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
os.environ['HOME'] = payload['home']
os.environ['MEMVANE_TEST_RUNTIME_ROOT'] = payload['runtime']
spec = importlib.util.spec_from_file_location('crash_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
import laya_blob_store as blobs
import laya_bridge_readiness as bridge
import laya_checkout_view as checkout
import laya_journal_reader as reader
import laya_ledger_common as ledger
import laya_project_dir_move as mover
import laya_reader_lease as lease
import laya_shadow_migration as migration
import laya_view_store as views
migration.MAX_BATCH = payload['batch_limit']

helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
store_type = views.ViewStore if payload.get('mode') in ('prepare-kill', 'publish-kill') else blobs.BlobStore
store = store_type.open(Path(payload['candidate']), repository=payload['repository'],
                        scope_id=payload['scope'])
checkout.capture_view(payload['project'], payload['repository'], payload['worktree'])
def crash(*args, **kwargs):
    print('blob-commit-before-intent', flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
with helper.full_authority(case, reader, bridge, lease, mover):
    if payload.get('mode') == 'restart-kill':
        def kill_before_restart_commit(*args, **kwargs):
            print('restart-proved-before-intent', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        with mock.patch.object(ledger, '_write', side_effect=kill_before_restart_commit):
            migration.advance_shadow(store, project=Path(payload['project']), executing_root=case.plugin,
                                     deadline=time.monotonic() + 30)
        raise RuntimeError('restart crash checkpoint not reached')
    elif payload.get('mode') == 'corpus-kill':
        def kill_before_corpus_cursor(*args, **kwargs):
            print('corpus-committed-before-intent', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        with mock.patch.object(ledger, '_write', side_effect=kill_before_corpus_cursor):
            migration.advance_corpus(store, project=Path(payload['project']), executing_root=case.plugin,
                                     sidecar=False, deadline=time.monotonic() + 30)
        raise RuntimeError('corpus crash checkpoint not reached')
    elif payload.get('mode') == 'verification-kill':
        def kill_before_cursor(*args, **kwargs):
            print('verified-before-intent', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        with mock.patch.object(ledger, '_write', side_effect=kill_before_cursor):
            migration.advance_verification(store, project=Path(payload['project']),
                                           executing_root=case.plugin, deadline=time.monotonic() + 30)
        raise RuntimeError('verification crash checkpoint not reached')
    elif payload.get('mode') == 'prepare-kill':
        intent = ledger._read_json(Path(payload['candidate']).parent / 'shadow-migration-intent.json')
        handle = store.capture_view(payload['worktree']['worktreeId'])
        with lease.resolve_endpoint(Path(payload['project']), action='recall', host_root=case.plugin) as endpoint:
            decision = reader.journal.select_route(payload['project'], action='recall')
            proof = {'observation': intent['observation'], 'endpoint': endpoint, 'decision': decision,
                     'provenance_scan': intent['provenance_scan']}
            def kill_after_slot(*args, **kwargs):
                print('slot-committed-before-seal', flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            with mock.patch.object(store, '_roots', side_effect=kill_after_slot):
                store.prepare_generation(handle, None, expected_head=store.head(handle), legacy_snapshot=proof)
        raise RuntimeError('prepare crash checkpoint not reached')
    elif payload.get('mode') == 'publish-kill':
        def kill_before_receipt(*args, **kwargs):
            print('private-head-before-intent', flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        with mock.patch.object(ledger, '_write', side_effect=kill_before_receipt):
            migration.publish_shadow_generation(store, payload['generation'],
                project=Path(payload['project']), executing_root=case.plugin, deadline=time.monotonic() + 30)
        raise RuntimeError('publication crash checkpoint not reached')
    elif payload.get('mode') == 'writer-overlap':
        original = migration.import_wiki_batch
        def paused_copy(*args, **kwargs):
            assert kwargs['snapshot'].connections[0].in_transaction
            print('snapshot-pinned', flush=True)
            if sys.stdin.readline().strip() != 'continue':
                raise RuntimeError('copy checkpoint not released')
            return original(*args, **kwargs)
        with mock.patch.object(migration, 'import_wiki_batch', side_effect=paused_copy):
            result = migration.advance_migration(store, project=Path(payload['project']),
                executing_root=case.plugin, deadline=time.monotonic() + 30)
        print(json.dumps({'phase': result['phase'], 'observation': result['observation']}), flush=True)
    else:
        with mock.patch.object(ledger, '_write', side_effect=crash):
            migration.advance_migration(store, project=Path(payload['project']),
                                        executing_root=case.plugin, deadline=time.monotonic() + 30)
        raise RuntimeError('crash checkpoint not reached')
