"""Actual detached hook -> synthetic teacher CLI -> isolated QMD index/embed."""
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SNAP = Path(os.environ.get('QMD_PUBLISH_E2E_MODEL_SNAPSHOT',
    '/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529'))
QMD = Path(os.environ.get('QMD_PUBLISH_E2E_QMD_BIN',
    shutil.which('qmd') or '/Users/dulee/work/.qmd-tools/bin/qmd'))
if not QMD.is_file() or not (SNAP / 'models').is_dir() or not (SNAP / 'offline.cjs').is_file():
    print(json.dumps({'skipped': 'isolated offline QMD runtime unavailable'}))
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix='qmd-create-hook-') as name:
    root = Path(name).resolve()
    (root / '.qmd-topical-sandbox').write_text('synthetic only\n')
    (root / '.qmd-topical-fake-only').write_text('synthetic only\n')
    (root / 'sources').mkdir()
    (root / 'sources/ignored.md').write_text('Excluded synthetic note.\n')
    wiki = root / '.auto-context/wiki'; wiki.mkdir(parents=True)
    collection = 'fixture-create-wiki'
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': [collection],
        'collectionPaths': {collection: '.auto-context/wiki'},
        'collectionRoles': {collection: 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    (root / 'cards.json').write_text('[]\n')
    (root / '.topical-reconcile-hook.json').write_text(json.dumps({
        'sourceRoots': ['sources'], 'cardsFile': 'cards.json',
        'trustedCardIds': [], 'skipPaths': ['ignored.md']}))
    teacher = Path.cwd() / 'test/fixtures/wiki-topical-approved-fake-teacher.py'
    cfg = {'extractor': {'backends': {'codex': [sys.executable, str(teacher)]}},
           'verify': {'backends': {'codex': [sys.executable, str(teacher)]},
                      'crossEngine': 'off'}}
    auto = root / '.topical-auto-refresh.json'
    auto.write_text(json.dumps({'schema': 'qmd-topical-auto-refresh-v1',
        'enabled': True, 'sourceRoots': ['sources'], 'engine': 'codex',
        'compileCfg': cfg, 'maxEstimatedCents': 30}))
    auto.chmod(0o600)
    config = root / 'qmd-config/index.yml'; config.parent.mkdir()
    config.write_text('collections: {}\nmodels:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n'
        '  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n')
    (root / 'qmd-cache/qmd').mkdir(parents=True)
    (root / 'qmd-cache/qmd/models').symlink_to(SNAP / 'models', target_is_directory=True)
    (root / 'qmd-db').mkdir()
    env = {**os.environ, 'QMD_TOPICAL_PROJECT_ROOT': str(root),
           'QMD_TOPICAL_SYNTHETIC_RUNTIME': '1',
           'QMD_BIN': str(QMD), 'INDEX_PATH': str(root / 'qmd-db/index.sqlite'),
           'QMD_CONFIG_DIR': str(config.parent), 'XDG_CACHE_HOME': str(root / 'qmd-cache'),
           'NODE_OPTIONS': '--require=' + str(SNAP / 'offline.cjs'),
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    def hook():
        result = subprocess.run(['bash', 'hooks/run-hook', 'topical-reconcile', 'codex'],
            env=env, input='', text=True, capture_output=True, timeout=3, check=True)
        assert not result.stdout
    def wait_jobs():
        deadline = time.monotonic() + 50
        queue = root / '.topical-hook-jobs'
        while time.monotonic() < deadline:
            if queue.is_dir() and not list(queue.glob('*.json')): return
            time.sleep(.05)
        raise AssertionError((root / 'topical-hook-worker.log').read_text())
    hook(); wait_jobs()  # Establish empty baseline before the new source exists.
    assert not json.loads((root / 'topical-reconcile-state.json').read_text())['queue']
    assert not (root / 'fake-teacher-calls.jsonl').exists()
    policy = auto.read_bytes()
    auto.unlink()
    (root / 'sources/new.md').write_text('The glass bell marks noon.\n')
    hook(); wait_jobs()
    pending = json.loads((root / 'topical-hook-status.json').read_text())
    assert pending['result'] == {'status': 'pending_review',
                                 'reason': 'auto_teacher_policy_required'}, pending
    assert not (root / 'fake-teacher-calls.jsonl').exists()
    auto.write_bytes(policy); auto.chmod(0o600)
    env['QMD_SYNTHETIC_TEACHER_FAIL_ONCE'] = '1'
    hook(); wait_jobs()
    failed = json.loads((root / 'topical-hook-status.json').read_text())
    assert failed['result'] == {'status': 'pending_review',
                                'reason': 'generation_failed_retry_scheduled'}, failed
    assert not list(wiki.glob('topical-v2/*/*.md'))
    hook(); wait_jobs()
    status = json.loads((root / 'topical-hook-status.json').read_text())
    assert status['status'] == 'completed', status
    state = json.loads((root / 'topical-reconcile-state.json').read_text())
    assert not state['queue'] and 'synthetic-new' in state['projection']
    assert state['projection']['synthetic-new']['state'] == 'eligible_existing_attestation'
    pages = list(wiki.glob('topical-v2/*/synthetic-new.md'))
    assert len(pages) == 1 and 'The glass bell marks noon.' in pages[0].read_text()
    calls = [json.loads(line)['task'] for line in (root / 'fake-teacher-calls.jsonl').read_text().splitlines()]
    assert calls.count('generate_topical_candidates_sandbox_only') == 2
    assert calls.count('verify_topical_claims_sandbox_only') == 1
    attempts = sorted((root / 'topical-backend-audit').glob('generation.*.attempt.json'))
    assert len(attempts) == 2 and sorted(json.loads(p.read_text())['state'] for p in attempts) == [
        'completed', 'failed']
    import sqlite3
    db = sqlite3.connect(root / 'qmd-db/index.sqlite')
    row = db.execute('SELECT hash FROM documents WHERE collection=? AND active=1',
                     (collection,)).fetchone()
    assert row and db.execute('SELECT 1 FROM content_vectors WHERE hash=?', row).fetchone()
    db.close()
    hook(); wait_jobs()
    unchanged = json.loads((root / 'topical-reconcile-state.json').read_text())
    assert 'synthetic-new' in unchanged['projection'] and not unchanged['queue']
    (root / 'sources/new.md').write_text('The glass bell marks midnight.\n')
    (root / 'sources/extra.md').write_text('The copper bell marks dawn.\n')
    hook(); wait_jobs()
    mixed = json.loads((root / 'topical-reconcile-state.json').read_text())
    assert 'sources/extra.md' in mixed['queue'] and 'sources/new.md' not in mixed['queue']
    assert mixed['projection']['synthetic-new']['state'] == 'eligible_existing_attestation'
    hook(); wait_jobs()
    new_result = json.loads((root / 'topical-hook-status.json').read_text())['result']
    if new_result and new_result.get('reason') == 'similarity_unresolved':
        review = root / 'topical-similarity-review.json'
        review.write_text(json.dumps({'schema': 'qmd-topical-similarity-review-v1',
            'decisions': [{'pairSha256': pair['pairSha256'], 'verdict': 'distinct',
                           'reviewerId': 'synthetic-test-reviewer',
                           'reason': 'Synthetic facts concern different bells.'}
                          for pair in new_result['pairs']]}))
        review.chmod(0o600)
        hook(); wait_jobs()
    final_state = json.loads((root / 'topical-reconcile-state.json').read_text())
    assert not final_state['queue'] and {'synthetic-new', 'synthetic-extra'} <= set(final_state['projection'])
    assert len(list(wiki.glob('topical-v2/*/synthetic-extra.md'))) == 1
    print(json.dumps({'hookCreate': True, 'policyGate': True, 'syntheticTeacherCalls': len(calls),
        'qmdIndexedEmbedded': True, 'teacherRetryNextSession': True,
        'nextSessionPreservesCard': True, 'mixedRefreshAndCreate': True,
        'skipPathsRespected': True,
        'externalTeacherCalls': 0}))
