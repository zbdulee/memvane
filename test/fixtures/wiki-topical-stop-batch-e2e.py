"""One Stop drains mixed synthetic sources through the real isolated QMD path."""
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import topical_hook_queue as queue
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_similarity as similarity

SNAP = Path(os.environ.get('QMD_PUBLISH_E2E_MODEL_SNAPSHOT',
    '/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529'))
QMD = Path(os.environ.get('QMD_PUBLISH_E2E_QMD_BIN',
    shutil.which('qmd') or '/Users/dulee/work/.qmd-tools/bin/qmd'))
if not QMD.is_file() or not (SNAP / 'models').is_dir() or not (SNAP / 'offline.cjs').is_file():
    print(json.dumps({'skipped': 'isolated offline QMD runtime unavailable'}))
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix='qmd-stop-batch-') as name:
    root = Path(name).resolve()
    (root / '.qmd-topical-sandbox').write_text('synthetic only\n')
    (root / '.qmd-topical-fake-only').write_text('synthetic only\n')
    sources = root / 'sources'; sources.mkdir()
    wiki = root / '.auto-context/wiki'; wiki.mkdir(parents=True)
    collection = 'fixture-stop-wiki'
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': [collection],
        'collectionPaths': {collection: '.auto-context/wiki'},
        'collectionRoles': {collection: 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    teacher = Path.cwd() / 'test/fixtures/wiki-topical-approved-fake-teacher.py'
    cfg = {'extractor': {'backends': {'codex': [sys.executable, str(teacher)]}},
           'verify': {'backends': {'codex': [sys.executable, str(teacher)]},
                      'crossEngine': 'off'}}
    auto = root / '.topical-auto-refresh.json'
    auto.write_text(json.dumps({'schema': 'qmd-topical-auto-refresh-v1',
        'enabled': True, 'sourceRoots': ['sources'], 'engine': 'codex',
        'compileCfg': cfg, 'maxEstimatedCents': 30}))
    auto.chmod(0o600)
    qmd_config = root / 'qmd-config/index.yml'; qmd_config.parent.mkdir()
    qmd_config.write_text('collections: {}\nmodels:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n'
        '  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n')
    (root / 'qmd-cache/qmd').mkdir(parents=True)
    (root / 'qmd-cache/qmd/models').symlink_to(SNAP / 'models', target_is_directory=True)
    (root / 'qmd-db').mkdir()
    os.environ.update(QMD_TOPICAL_SYNTHETIC_RUNTIME='1', QMD_SETUP_GUARD_FIXTURE='1',
        QMD_BIN=str(QMD), INDEX_PATH=str(root / 'qmd-db/index.sqlite'),
        QMD_CONFIG_DIR=str(qmd_config.parent), XDG_CACHE_HOME=str(root / 'qmd-cache'),
        NODE_OPTIONS='--require=' + str(SNAP / 'offline.cjs'),
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    def boundary(turn):
        return queue._run(root, {'action': 'boundary', 'turnKey': turn})
    def db_counts():
        with sqlite3.connect(root / 'qmd-db/index.sqlite') as db:
            return (db.execute('SELECT count(*) FROM documents WHERE collection=? AND active=1',
                               (collection,)).fetchone()[0],
                    db.execute('SELECT count(*) FROM content_vectors').fetchone()[0])
    def pending():
        state = reconcile._read(root)
        return sorted(state['queue']), state['inFlight']
    # Keep this test on batch/settlement, while similarity review is covered by
    # its own pair-hash tests. No new source is accepted from semantic scores.
    with patch.object(similarity, 'retrieve', return_value=[]), \
         patch.object(similarity, 'evaluate', return_value={
             'status': 'distinct_or_no_candidates', 'pairs': []}):
        assert queue._run(root, {'action': 'reconcile', 'turnKey': 'baseline'})['status'] == 'no_net_changes'
        (sources / 'a.md').write_text('Amber foxes mark the spring equinox.\n')
        (sources / 'b.md').write_text('Silver owls guard the autumn gate.\n')
        first = boundary('codex:two-new')
        assert first['status'] == 'backend_batch_synced' and first['completedInHandoff'] == 2, first
        assert pending() == ([], None) and db_counts() == (2, 2)
        (sources / 'a.md').write_text('Amber foxes mark the summer solstice.\n')
        (sources / 'b.md').unlink()
        (sources / 'c.md').write_text('Copper cranes count the winter stars.\n')
        (sources / 'd.md').write_text('Violet turtles map the ocean floor.\n')
        mixed = boundary('codex:edit-delete-two-new')
        assert mixed['status'] == 'backend_batch_synced' and mixed['completedInHandoff'] == 4, mixed
        assert pending() == ([], None) and db_counts() == (3, 3)
        assert {'synthetic-a', 'synthetic-c', 'synthetic-d'} == {
            page.stem for page in wiki.glob('topical-v2/*/*.md')}
        # Crash after a successful deletion and after the next card has been
        # generated, before its QMD sync. The same durable Stop job resumes it.
        (sources / 'a.md').unlink()
        (sources / 'd.md').write_text('Violet turtles map the mountain ridge.\n')
        (sources / 'e.md').write_text('Golden moths trace the midnight wind.\n')
        real_sync = publisher.sync
        failed = [False]
        def fail_once(*args, **kwargs):
            journal = root / 'topical-refresh-state.json'
            if (journal.is_file() and
                    json.loads(journal.read_text()).get('oldCardId') == 'synthetic-d'
                    and not failed[0]):
                failed[0] = True
                raise RuntimeError('synthetic_qmd_sync_failure')
            return real_sync(*args, **kwargs)
        with patch.object(queue, '_spawn_worker'):
            assert queue.enqueue(root, 'boundary', {'turn_id': 'failure-middle'})['status'] == 'queued'
            with patch.object(publisher, 'sync', side_effect=fail_once):
                queue.worker(root)
            assert failed[0]
            status = json.loads((root / 'topical-hook-status.json').read_text())
            assert status['status'].startswith('failed:RuntimeError:synthetic_qmd_sync_failure'), status
            assert len(list((root / queue.QUEUE).glob('*.json'))) == 1
            assert pending()[0] and pending()[1]
            calls_before = (root / 'fake-teacher-calls.jsonl').read_text().splitlines()
            queue.worker(root)  # Same durable job; no new Stop or SessionStart.
        status = json.loads((root / 'topical-hook-status.json').read_text())
        assert status['status'] == 'completed' and status['result']['status'] == 'backend_batch_synced', status
        assert not list((root / queue.QUEUE).glob('*.json'))
        assert pending() == ([], None) and db_counts() == (3, 3)
        calls_after = (root / 'fake-teacher-calls.jsonl').read_text().splitlines()
        # Recovery reuses d's completed model audit and only calls for new e.
        assert len(calls_after) == len(calls_before) + 2
        print(json.dumps({'twoNewOneStop': True, 'mixedFourOneStop': True,
            'failedMiddleJobRetained': True, 'sameJobRecovered': True,
            'completedAuditReused': True, 'finalDocs': 3, 'finalVectors': 3,
            'externalTeacherCalls': 0}))
