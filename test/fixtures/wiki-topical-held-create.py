"""Legacy similarity review and interrupted two-card create recover without new calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_create as creator
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import topical_hook_queue as hook_queue
import wiki_topical_similarity as similarity

CASE = os.environ.get('QMD_HELD_CASE', 'complement')
assert CASE in ('complement', 'full_duplicate', 'conflict')

with tempfile.TemporaryDirectory(prefix='qmd-partial-legacy-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    (root / 'sources').mkdir()
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    collection = 'fixture-partial-legacy-wiki'
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': [collection],
        'collectionPaths': {collection: '.auto-context/wiki'},
        'collectionRoles': {collection: 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    legacy = wiki / 'legacy-card.md'
    legacy.write_text('K-42 corrections retain earlier wording.\n' if CASE == 'full_duplicate'
        else 'A subject may correct or withdraw their recorded words.\n')
    legacy_sha = hashlib.sha256(legacy.read_bytes()).hexdigest()
    nested = wiki / 'qmd-prospective/tr-support-catalog/catalog-probe.md'
    nested.parent.mkdir(parents=True)
    nested.write_text('# A nested old wiki entry\nAnother unrelated archive label.\n')
    nested_rel = nested.relative_to(wiki).as_posix()
    db = root / 'index.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE documents(collection TEXT, path TEXT, hash TEXT, active INTEGER)')
        conn.execute('INSERT INTO documents VALUES(?,?,?,1)', (collection, legacy.name, legacy_sha))
        conn.execute('INSERT INTO documents VALUES(?,?,?,1)', (collection, nested_rel,
            hashlib.sha256(nested.read_bytes()).hexdigest()))
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', return_value=SimpleNamespace(stdout=json.dumps([
             {'file': f'qmd://{collection}/{legacy.name}'},
             {'file': f'qmd://{collection}/{nested_rel}'}]))):
        rows = similarity.retrieve(root, 'Unrelated archive label')
    assert len(rows) == 2 and all(row['legacy'] is True for row in rows)
    assert rows[1]['path'] == nested_rel
    assert rows[0]['pageSha256'] == legacy_sha
    assert rows[0]['lead'].startswith(legacy.read_text())
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE documents SET hash=? WHERE path=?',
                     ('0' * 64, legacy.name))
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', return_value=SimpleNamespace(stdout=json.dumps([
             {'file': f'qmd://{collection}/{legacy.name}'}]))):
        try:
            similarity.retrieve(root, 'Unrelated archive label')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_index_stale'
        else:
            raise AssertionError('stale legacy QMD hash accepted')
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE documents SET hash=? WHERE path=?', (legacy_sha, legacy.name))

    def qmd_call(_root, _runtime, _argv):
        with sqlite3.connect(db) as conn:
            paths = [row[0] for row in conn.execute(
                'SELECT path FROM documents WHERE collection=? AND active=1', (collection,))]
        return SimpleNamespace(stdout=json.dumps([
            {'file': f'qmd://{collection}/{path}'} for path in paths]))

    reconcile.reconcile(root, ['sources'], [], trusted_card_ids=[])
    source = root / 'sources/new.md'
    source.write_text('K-42 corrections retain earlier wording.\nA keeper needs dated consent to inspect K-42.\n')
    reconcile.reconcile(root, ['sources'], [], trusted_card_ids=[])
    revision, _, _ = topical.source_snapshot(root, 'sources/new.md', {})
    def make_card(cid, line, statement):
        return {'cardId': cid, 'title': statement, 'category': 'world-rule',
            'details': '', 'claims': [{'claimId': 'main', 'statement': statement,
            'state': 'rule', 'timeScope': 'chapter-1', 'condition': '',
            'evidence': [{'sourcePath': 'sources/new.md',
                'sourceRevisionSha256': revision['sha256'], 'startLine': line,
                'endLine': line, 'quoteAnchor': statement,
                'quoteSha256': topical.digest(statement.encode())}]}]}
    cards = [make_card('amber', 1, 'K-42 corrections retain earlier wording.'),
             make_card('silver', 2, 'A keeper needs dated consent to inspect K-42.')]
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    calls = []
    def generate(argv, payload, timeout, cwd):
        calls.append('generate')
        return {'schema': topical.SCHEMA, 'cards': cards}, None, 0
    def verify(argv, payload, timeout, cwd):
        calls.append('verify')
        checks = [{'claimId': claim['claimId'], 'sourcePath': ev['sourcePath'],
            'quoteSha256': ev['quoteSha256'], 'quoteAnchor': ev['quoteAnchor'],
            'supported': True} for claim in payload['card']['claims']
            for ev in claim['evidence']]
        return {'verdict': 'pass', 'checks': checks, 'reasons': []}, None, 0
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        pending = creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
            160, generation_runner=generate, verification_runner=verify)
    assert pending['status'] == 'pending_review'
    assert pending['reason'] == 'similarity_unresolved', pending
    assert len(calls) == 3 and calls.count('generate') == 1 and calls.count('verify') == 2
    journal = json.loads((root / creator.JOURNAL).read_text())
    gid = journal['generationId']
    staged = {cid: experiment.load_staged_card(root, gid, cid) for cid in journal['cardIds']}
    decisions = {}
    for cid, card in staged.items():
        matches = similarity.evaluate(root, card, gid, rows)['pairs']
        for row in matches:
            # The legacy page contains a broad right to correct records. The
            # K-42 revision-retention detail is held; independent consent is safe.
            verdict = ('conflict' if CASE == 'conflict' else 'duplicate') if (
                cid == 'amber' and row['candidatePath'] == legacy.name) else 'distinct'
            decisions[row['pairSha256']] = {'pairSha256': row['pairSha256'],
                'verdict': verdict, 'reviewerId': 'fixture-review',
                'reason': 'Synthetic semantic review with a retained unique detail.'}
    sibling = similarity.evaluate(root, staged['amber'], gid, [{
        'path': f'topical-v2/{gid}/silver.md', 'card': staged['silver'],
        'pageSha256': hashlib.sha256(topical.read_generation_bytes(root, gid,
            'cards/silver.md')).hexdigest()}])['pairs'][0]
    decisions[sibling['pairSha256']] = {'pairSha256': sibling['pairSha256'],
        'verdict': 'distinct', 'reviewerId': 'fixture-review',
        'reason': 'Correction and inspection are independent rules.'}
    review = root / similarity.REVIEW_FILE
    review.write_text(json.dumps({'schema': 'qmd-topical-similarity-review-v1',
        'decisions': list(decisions.values())}))
    review.chmod(0o600)
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        with patch.object(reconcile, 'park_created_batch', side_effect=RuntimeError('crash')):
            try:
                creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
                    160, generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'crash'
            else:
                raise AssertionError('crash point did not interrupt create')
        assert (root / creator.JOURNAL).is_file()
        result = creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
            160, generation_runner=generate, verification_runner=verify)
    assert result['status'] == 'created_source_held', result
    assert result['held'] == ['amber'] and result['published'] == 1, result
    assert not (wiki / 'topical-v2' / gid / 'amber.md').exists()
    assert (wiki / 'topical-v2' / gid / 'silver.md').is_file()
    assert (root / 'topical-generations' / gid / 'cards/amber.md').is_file()
    state = reconcile._read(root)
    assert state['queue'] and not state['inFlight']
    assert state['heldSources']['sources/new.md'] == revision['sha256']
    _refs, published = refresh._baseline_cards(root)
    projection = reconcile.safe_projection(root, ['sources'], published,
        trusted_card_ids=[card['cardId'] for card in published])
    assert projection['silver']['state'] == 'eligible_existing_attestation', projection
    idle = hook_queue._drain_auto(root, {'maxEstimatedCents': 160}, ())
    assert idle['reason'] == 'held_source_similarity' and idle['completedInHandoff'] == 0
    held = json.loads((root / 'topical-held-create' / (journal['batchId'] + '.json')).read_text())
    assert held['heldCards']['amber']['reason'] == (
        'similarity_conflict' if CASE == 'conflict' else 'similarity_duplicate')
    assert not (root / creator.JOURNAL).exists()
    # Restart and duplicate invocation do not call the teacher or settle source.
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call):
        again = creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
            160, generation_runner=generate, verification_runner=verify)
    assert again['status'] == 'no_net_changes', again
    assert calls == ['generate', 'verify', 'verify']
    other = root / 'sources/other.md'
    other.write_text('Independent source fact.\n')
    reconcile.reconcile(root, ['sources'], [staged['silver']], trusted_card_ids=['silver'])
    claim = reconcile.start_batch(root, ['sources'], [staged['silver']], trusted_card_ids=['silver'])
    assert claim['started'] and set(claim['batch']['sources']) == {'sources/other.md'}, claim
    assert 'sources/new.md' in reconcile._read(root)['queue']
    reconcile.finish_mock_batch(root, ['sources'], claim['batch']['batchId'], success=False)
    source.write_text(source.read_text() + 'A new revision is available.\n')
    reconcile.reconcile(root, ['sources'], [staged['silver']], trusted_card_ids=['silver'])
    assert 'sources/new.md' not in reconcile._read(root)['heldSources']
    resumed = reconcile.start_batch(root, ['sources'], [staged['silver']],
        trusted_card_ids=['silver'])
    assert resumed['started'] and 'sources/new.md' in resumed['batch']['sources']
    print(json.dumps({'case': CASE, 'k42Held': True, 'independentCardPublished': True,
        'retrySafe': True, 'otherSourceClaimed': True, 'newRevisionRequeued': True}))
