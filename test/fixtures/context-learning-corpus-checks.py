"""Synthetic active-index CUD and policy changes never rewrite captured gold."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, 'core')
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.corpus import snapshot, reconcile, current_evaluation_ready
from memvane.learning.context_learning.seam import observe

with tempfile.TemporaryDirectory(prefix='qmd-corpus-check-') as temporary:
    base = Path(temporary).resolve()
    project = base / 'project'; project.mkdir(mode=0o700)
    docs = project / 'docs'; docs.mkdir()
    source = docs / 'one.md'; source.write_text('Synthetic evidence version one.\n')
    state_parent = base / 'private'; state_parent.mkdir(mode=0o700)
    config_dir = base / 'qmd-config'; config_dir.mkdir()
    config_file = config_dir / 'index.yml'; config_file.write_text('models: {embed: synthetic-one}\n')
    index = base / 'qmd-db/index.sqlite'; index.parent.mkdir()
    os.environ['INDEX_PATH'] = str(index)
    os.environ['QMD_CONFIG_DIR'] = str(config_dir)
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                   ('docs', 'one.md', hashlib.sha256(source.read_bytes()).hexdigest()))
    config = {'collections': ['docs'], 'collectionPaths': {'docs': 'docs'},
              'collectionRoles': {'docs': 'raw'}, 'recallStrategy': 'flat',
              'topN': 3, 'minScore': 0, 'contextLearning': {
                  'capture': True, 'stateRoot': str(state_parent)}}
    first = snapshot(project, config)
    assert first is not None and first['activeDocuments'] == 1
    hit = {'file': 'docs/one.md', 'score': 1.0}
    status = observe({'prompt': 'Synthetic evidence request'}, config, project, [],
                     phases=[{'name': 'primary', 'results': [hit], 'wiki_scoped': False,
                              'returned_count': 1}], verdict=lambda *_: 'eligible')
    assert status == 'stored', status
    state = state_parent / digest(str(project))
    with sqlite3.connect(state / 'learning.sqlite3') as db:
        rid, original = db.execute('SELECT request_id,body FROM samples').fetchone()
        assert json.loads(original)['sampling']['index_revision'] == first['fingerprint']
        db.execute('INSERT INTO manifest_cases VALUES (?,?,?)', ('v1', rid, 'docs/one.md'))
    assert current_evaluation_ready(state, 'v1', first)
    seen = {first['fingerprint']}
    def changed(expected_count):
        current = snapshot(project, config)
        assert current and current['fingerprint'] not in seen
        seen.add(current['fingerprint'])
        result = reconcile(state, current)
        assert result['changed'] and result['queued'] == 1
        assert current['activeDocuments'] == expected_count
        assert not current_evaluation_ready(state, 'v1', current)
    source.write_text('Synthetic evidence version two.\n')
    with sqlite3.connect(index) as db:
        db.execute('UPDATE documents SET hash=? WHERE path=?',
                   (hashlib.sha256(source.read_bytes()).hexdigest(), 'one.md'))
    changed(1)
    second = docs / 'two.md'; second.write_text('A new synthetic document.\n')
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                   ('docs', 'two.md', hashlib.sha256(second.read_bytes()).hexdigest()))
    changed(2)
    source.unlink()
    with sqlite3.connect(index) as db:
        db.execute('UPDATE documents SET active=0 WHERE path=?', ('one.md',))
    changed(1)
    config = {**config, 'topN': 2}
    changed(1)
    config_file.write_text('models: {embed: synthetic-two}\n')
    changed(1)
    with sqlite3.connect(state / 'learning.sqlite3') as db:
        saved = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()[0]
        queued = db.execute('SELECT COUNT(*) FROM corpus_review_queue '
                            'WHERE request_id=? AND status=?', (rid, 'pending_review')).fetchone()[0]
    assert saved == original and queued == 5
    print(json.dumps({'capturedFingerprint': True, 'updateDetected': True,
                      'createDetected': True, 'deleteDetected': True,
                      'pluginPolicyDetected': True, 'qmdModelConfigDetected': True,
                      'historicalSampleUnchanged': True, 'currentEvaluationExcluded': True,
                      'reviewQueueEntries': queued, 'externalCalls': 0}))
