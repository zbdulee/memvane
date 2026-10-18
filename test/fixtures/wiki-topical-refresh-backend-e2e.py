"""Synthetic real backend functions -> multisource delete -> isolated QMD CUD."""
import hashlib
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh

SNAP = Path(os.environ.get('QMD_PUBLISH_E2E_MODEL_SNAPSHOT',
    '/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529'))
QMD = Path(os.environ.get('QMD_PUBLISH_E2E_QMD_BIN',
    shutil.which('qmd') or '/Users/dulee/work/.qmd-tools/bin/qmd'))
if not QMD.is_file() or not (SNAP / 'models').is_dir() or not (SNAP / 'offline.cjs').is_file():
    print(json.dumps({'skipped': 'isolated offline QMD runtime unavailable'}))
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix='qmd-refresh-backend-') as temporary:
    root = Path(temporary).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    sources = root / 'sources'; sources.mkdir()
    wiki = root / '.auto-context/wiki'; wiki.mkdir(parents=True)
    collection = 'fixture-refresh-wiki'
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': [collection],
        'collectionPaths': {collection: '.auto-context/wiki'},
        'collectionRoles': {collection: 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    quotes = {'a.md': 'The amber bell marks dawn.',
              'b.md': 'The silver bell marks dusk.'}
    for name, quote in quotes.items(): (sources / name).write_text(quote + '\n')
    def claim(name, quote):
        rev = topical.source_snapshot(root, 'sources/' + name, {})[0]
        return {'claimId': name[:-3] + '-rule', 'statement': quote,
                'state': 'rule', 'timeScope': 'chapter-1', 'condition': '',
                'evidence': [{'sourcePath': 'sources/' + name,
                              'sourceRevisionSha256': rev['sha256'],
                              'startLine': 1, 'endLine': 1, 'quoteAnchor': quote,
                              'quoteSha256': topical.digest(quote.encode())}]}
    def card(names):
        return {'cardId': 'two-bells', 'title': 'Two bells', 'category': 'world-rule',
                'details': '', 'claims': [claim(name, quotes[name]) for name in names]}
    first = backend.stage_generation_response(root, {'schema': topical.SCHEMA,
        'cards': [card(['a.md', 'b.md'])]})
    old_id = first['generationId']
    old = experiment.load_staged_card(root, old_id, 'two-bells')
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    calls = []
    def verify_runner(argv, payload, timeout, cwd):
        calls.append(('verify', len(payload['sources'])))
        checks = [{'claimId': c['claimId'], 'sourcePath': span['sourcePath'],
                   'quoteSha256': span['quoteSha256'], 'quoteAnchor': span['quoteAnchor'],
                   'supported': True}
                  for c in payload['card']['claims'] for span in c['evidence']]
        return {'verdict': 'pass', 'checks': checks, 'reasons': []}, None, 0
    assert backend.run_verification_backend(root, old_id, 'two-bells', cfg, 'codex',
        allow_backend_execution=True, runner=verify_runner)['status'] == 'backend_pass'
    publisher.publish(root, old_id, 'two-bells')
    qmd_config = root / 'qmd-config/index.yml'; qmd_config.parent.mkdir()
    qmd_config.write_text('collections: {}\nmodels:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n'
        '  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n')
    (root / 'qmd-cache/qmd').mkdir(parents=True)
    (root / 'qmd-cache/qmd/models').symlink_to(SNAP / 'models', target_is_directory=True)
    (root / 'qmd-db').mkdir()
    os.environ.update(QMD_TOPICAL_SYNTHETIC_RUNTIME='1',
        QMD_BIN=str(QMD), INDEX_PATH=str(root / 'qmd-db/index.sqlite'),
        QMD_CONFIG_DIR=str(qmd_config.parent), XDG_CACHE_HOME=str(root / 'qmd-cache'),
        NODE_OPTIONS='--require=' + str(SNAP / 'offline.cjs'),
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    assert publisher.sync(root)['verifiedCards'] == 1
    reconcile.reconcile(root, ['sources'], [old], trusted_card_ids=['two-bells'])
    auto_config=root/'.topical-auto-refresh.json'
    auto_config.write_text(json.dumps({'schema':'qmd-topical-auto-refresh-v1',
        'enabled':True,'sourceRoots':['sources'],'engine':'codex',
        'compileCfg':cfg,'maxEstimatedCents':20}))
    auto_config.chmod(0o600)
    # A Stop batch for an unrelated new source cannot be claimed by this card.
    (sources / 'unrelated.md').write_text('An unrelated synthetic note.\n')
    unrelated = reconcile.start_batch(root, ['sources'], [old],
        trusted_card_ids=['two-bells'])
    assert unrelated['started'] and set(unrelated['batch']['sources']) == {'sources/unrelated.md'}
    (sources / 'a.md').unlink()
    blocked = refresh.refresh_one(root, ['sources'], old, old_id, cfg, 'codex',
        allow_backend_execution=True,
        generation_runner=lambda *_: (_ for _ in ()).throw(AssertionError('wrong_batch_backend')),
        verification_runner=verify_runner)
    assert blocked == {'status': 'batch_running'}
    assert not (root / 'topical-refresh-state.json').exists()
    assert reconcile._read(root)['inFlight']['batchId'] == unrelated['batch']['batchId']
    (sources / 'unrelated.md').unlink()
    assert reconcile.supersede_changed_batch(root, ['sources'],
        unrelated['batch']['batchId']) is True
    preclaimed = reconcile.start_batch(root, ['sources'], [old],
        trusted_card_ids=['two-bells'])
    assert preclaimed['started'] and set(preclaimed['batch']['sources']) == {'sources/a.md'}
    projected = reconcile.safe_projection(root, ['sources'], [old],
                                           trusted_card_ids=['two-bells'])
    assert projected['two-bells']['state'] == 'excluded_stale'
    states = [c['state'] for c in projected['two-bells']['claims']]
    assert states == ['stale', 'prior_evidence_unchanged']
    forged = json.loads(json.dumps(old))
    forged['title'] = 'Forged old card'
    try:
        publisher.retire_stale(root, old_id, 'two-bells', ['sources'], [forged])
    except topical.TopicalError as error:
        assert error.code == 'stale_card_identity_mismatch'
    else:
        raise AssertionError('forged_stale_card_authorized_retirement')
    assert (wiki / 'topical-v2' / old_id / 'two-bells.md').is_file()
    def generation_runner(argv, payload, timeout, cwd):
        calls.append(('generate', len(payload['sources'])))
        assert [s['path'] for s in payload['sources']] == ['sources/b.md']
        assert payload['existingWikiCandidates'] == []
        # A second worker cannot enter this batch while the first owns the
        # refresh lock, even before its generation response is recorded.
        try:
            refresh.refresh_one(root, ['sources'], old, old_id, cfg, 'codex',
                allow_backend_execution=True, generation_runner=generation_runner,
                verification_runner=verify_runner)
        except topical.TopicalError as error:
            assert error.code == 'refresh_busy'
        else:
            raise AssertionError('concurrent_refresh_entered_batch')
        return {'schema': topical.SCHEMA, 'cards': [card(['b.md'])]}, None, 0
    original_save = refresh._save
    def interrupt_before_claim_record(root_arg, row):
        if row['phase'] == 'claimed':
            raise RuntimeError('synthetic_crash_before_refresh_journal')
        return original_save(root_arg, row)
    refresh._save = interrupt_before_claim_record
    try:
        refresh.auto_refresh_pending(root, generation_runner=generation_runner,
            verification_runner=verify_runner)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_before_refresh_journal'
    else:
        raise AssertionError('synthetic_claim_interrupt_missing')
    finally:
        refresh._save = original_save
    assert reconcile._read(root)['inFlight']['batchId'] == preclaimed['batch']['batchId']
    assert not (root / 'topical-refresh-state.json').exists()
    assert sum(kind == 'generate' for kind, _ in calls) == 0
    def interrupt_after_generation(root_arg, row):
        if row['phase'] == 'generated':
            raise RuntimeError('synthetic_crash_after_completed_generation')
        return original_save(root_arg, row)
    refresh._save = interrupt_after_generation
    try:
        refresh.auto_refresh_pending(root, generation_runner=generation_runner,
            verification_runner=verify_runner)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_after_completed_generation'
    else:
        raise AssertionError('synthetic_generation_interrupt_missing')
    finally:
        refresh._save = original_save
    assert json.loads((root / 'topical-refresh-state.json').read_text())['batchId'] == preclaimed['batch']['batchId']
    assert sum(kind == 'generate' for kind, _ in calls) == 1
    safe_recovery = refresh.recover_pending(root)
    assert safe_recovery == {'status': 'pending_review',
                             'reason': 'verification_requires_explicit_backend'}
    assert sum(kind == 'generate' for kind, _ in calls) == 1
    original_attestation = backend.publish_pass_attestation
    def interrupt_after_verifier_response(*args, **kwargs):
        raise RuntimeError('synthetic_crash_before_attestation')
    backend.publish_pass_attestation = interrupt_after_verifier_response
    try:
        refresh.refresh_one(root, ['sources'], old, old_id, cfg, 'codex',
            allow_backend_execution=True, generation_runner=generation_runner,
            verification_runner=verify_runner)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_before_attestation'
    else:
        raise AssertionError('synthetic_verifier_interrupt_missing')
    finally:
        backend.publish_pass_attestation = original_attestation
    assert sum(kind == 'verify' for kind, _ in calls) == 2
    original_sync = publisher.sync
    def interrupt_after_retirement(*args, **kwargs):
        if (root / 'topical-retired' / old_id / 'two-bells.md').is_file():
            raise RuntimeError('synthetic_crash_before_qmd_sync')
        return original_sync(*args, **kwargs)
    publisher.sync = interrupt_after_retirement
    try:
        second_attempt = refresh.recover_pending(root)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_before_qmd_sync'
    else:
        raise AssertionError('synthetic_sync_interrupt_missing: ' + repr(second_attempt))
    finally:
        publisher.sync = original_sync
    assert (root / 'topical-retired' / old_id / 'two-bells.md').is_file()
    query_fixture = root / 'stale-query.json'
    query_fixture.write_text(json.dumps({'results': [{'file':
        f'qmd://{collection}/topical-v2/{old_id}/two-bells.md',
        'title': 'Two bells', 'score': 1.0, 'line': 1}]}))
    stale_recall = subprocess.run([sys.executable, 'core/recall.py'],
        input=json.dumps({'prompt': 'The silver bell marks dusk', 'cwd': str(root),
                          'hook_event_name': 'UserPromptSubmit'}), text=True,
        capture_output=True, env={**os.environ, 'QMD_QUERY_FIXTURE': str(query_fixture),
                                  'QMD_RECALL_LOG': ''}, check=True)
    assert not stale_recall.stdout.strip(), stale_recall.stdout
    new_id = json.loads((root / 'topical-refresh-state.json').read_text())['generationId']
    original_finish = reconcile.finish_backend_batch
    def interrupt_after_sync(*args, **kwargs):
        raise RuntimeError('synthetic_crash_after_qmd_sync')
    reconcile.finish_backend_batch = interrupt_after_sync
    try:
        refresh.refresh_one(root, ['sources'], old, old_id, cfg, 'codex',
            allow_backend_execution=True, generation_runner=generation_runner,
            verification_runner=verify_runner)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_after_qmd_sync'
    else:
        raise AssertionError('synthetic_post_sync_interrupt_missing')
    finally:
        reconcile.finish_backend_batch = original_finish
    with sqlite3.connect(root / 'qmd-db/index.sqlite') as db:
        assert db.execute('SELECT active FROM documents WHERE collection=? AND path=?',
            (collection, f'topical-v2/{new_id}/two-bells.md')).fetchone()[0] == 1
    (root / 'hook-cards.json').write_text(json.dumps([old]))
    (root / '.topical-reconcile-hook.json').write_text(json.dumps({
        'sourceRoots':['sources'],'cardsFile':'hook-cards.json',
        'trustedCardIds':['two-bells'],'skipPaths':[]}))
    hook = subprocess.run(['bash', 'hooks/run-hook', 'topical-reconcile', 'codex'],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, 'QMD_TOPICAL_PROJECT_ROOT': str(root),
             'CLAUDE_PLUGIN_ROOT': str(Path.cwd()), 'QMD_RECALL_LOG': ''})
    assert hook.returncode == 0, hook.stderr
    deadline = time.monotonic() + 30
    while (root / 'topical-refresh-state.json').exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert not (root / 'topical-refresh-state.json').exists(), 'hook_recovery_not_completed'
    assert sum(kind == 'generate' for kind, _ in calls) == 1
    assert (root / 'topical-retired' / old_id / 'two-bells.md').is_file()
    assert not (wiki / 'topical-v2' / old_id / 'two-bells.md').exists()
    new = experiment.load_staged_card(root, new_id, 'two-bells')
    assert reconcile.safe_projection(root, ['sources'], [new],
                                     trusted_card_ids=['two-bells'])['two-bells']['state'] == 'eligible_existing_attestation'
    with sqlite3.connect(root / 'qmd-db/index.sqlite') as db:
        old_active = db.execute('SELECT active FROM documents WHERE collection=? AND path=?',
            (collection, f'topical-v2/{old_id}/two-bells.md')).fetchone()
        new_active = db.execute('SELECT active FROM documents WHERE collection=? AND path=?',
            (collection, f'topical-v2/{new_id}/two-bells.md')).fetchone()
    assert old_active is None and new_active and new_active[0] == 1
    # Stop recorded an edit, then the source was deleted before its worker ran.
    # No backend journal exists yet, so the changed handoff is superseded by
    # the final deletion snapshot rather than looping on batch_running.
    (sources / 'b.md').write_text('The silver bell marks late dusk.\n')
    predeleted = reconcile.start_batch(root, ['sources'], [new],
        trusted_card_ids=['two-bells'])
    assert predeleted['started'] and 'sources/b.md' in predeleted['batch']['sources']
    (sources / 'b.md').unlink()
    deletion_batches = []
    def commit_then_interrupt(*args, **kwargs):
        deletion_batches.append(args[2])
        original_finish(*args, **kwargs)
        raise RuntimeError('synthetic_crash_after_reconcile_commit')
    reconcile.finish_backend_batch = commit_then_interrupt
    try:
        refresh.refresh_one(root, ['sources'], new, new_id, cfg, 'codex',
            allow_backend_execution=True, generation_runner=generation_runner,
            verification_runner=verify_runner)
    except RuntimeError as error:
        assert str(error) == 'synthetic_crash_after_reconcile_commit'
    else:
        raise AssertionError('synthetic_commit_interrupt_missing')
    finally:
        reconcile.finish_backend_batch = original_finish
    assert deletion_batches and deletion_batches[0] != predeleted['batch']['batchId']
    deleted = refresh.recover_pending(root)
    assert deleted['status'] == 'already_completed' and deleted['generationId'] is None
    with sqlite3.connect(root / 'qmd-db/index.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM documents WHERE collection=? AND active=1',
                          (collection,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM documents WHERE collection=?',
                          (collection,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM content_vectors').fetchone()[0] == 0
    assert calls == [('verify', 2), ('generate', 1), ('verify', 1)]
    print(json.dumps({'backendGenerationAndVerification': True,
                      'multisourceReverseReference': True,
                      'forgedRetirementBlocked': True,
                      'survivingClaimRegenerated': True,
                      'completedAttemptReused': True, 'midPublishResumed': True,
                      'staleIndexExcludedDuringCrash': True,
                      'sessionStartRecovery': True,
                      'committedBatchRecovered': True,
                      'preclaimedStopBatchResumed': True,
                      'unrelatedBatchRejected': True,
                      'claimCrashRetried': True,
                      'concurrentRefreshRejected': True,
                      'changedBatchSuperseded': True,
                      'staleCardRetired': True, 'lastSourceDeletedFromQmd': True,
                      'retiredDocumentAndVectorReclaimed': True,
                      'backendCalls': len(calls), 'externalCalls': 0}))
