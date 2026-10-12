"""Legacy similarity review and interrupted two-card create recover without new calls."""
import hashlib
import json
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
import wiki_topical_similarity as similarity

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
    legacy.write_text('# A separate old wiki entry\nUnrelated archive label.\n')
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
    assert rows[0]['lead'].startswith('# A separate old wiki entry')
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
    source.write_text('Amber bell marks dawn.\nSilver bell marks dusk.\n')
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
    cards = [make_card('amber', 1, 'Amber bell marks dawn.'),
             make_card('silver', 2, 'Silver bell marks dusk.')]
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
        result = similarity.evaluate(root, card, gid, rows)
        assert result['status'] == 'pending_review'
        for row in result['pairs']:
            assert row['relation'] == 'legacy_wiki_review_required', row
            decisions[row['pairSha256']] = {'pairSha256': row['pairSha256'],
                'verdict': 'distinct', 'reviewerId': 'synthetic-reviewer',
                'reason': 'The legacy archive label is unrelated to bell times.'}
    pair = similarity.evaluate(root, staged['amber'], gid, [{
        'path': f'topical-v2/{gid}/silver.md', 'card': staged['silver'],
        'pageSha256': hashlib.sha256(topical.read_generation_bytes(root, gid,
            'cards/silver.md')).hexdigest()}])['pairs'][0]
    decisions[pair['pairSha256']] = {'pairSha256': pair['pairSha256'],
        'verdict': 'distinct', 'reviewerId': 'synthetic-reviewer',
        'reason': 'Dawn and dusk are separate synthetic observations.'}
    review = root / similarity.REVIEW_FILE
    review.write_text(json.dumps({'schema': 'qmd-topical-similarity-review-v1',
        'decisions': list(decisions.values())}))
    review.chmod(0o600)
    # Simulate the old crash point: one card has a durable publication proof.
    first = publisher.publish(root, gid, 'amber')
    first_sha = hashlib.sha256(Path(first['path']).read_bytes()).hexdigest()
    first_rel = Path(first['path']).relative_to(wiki).as_posix()
    assert similarity._page_bytes(root, first_rel) == topical.read_generation_bytes(
        root, gid, 'cards/amber.md')
    published_page = Path(first['path'])
    stamped = published_page.read_bytes()
    published_page.write_bytes(stamped.replace(b'status: verified',
                                              b'status: altered'))
    try:
        similarity._page_bytes(root, first_rel)
    except topical.TopicalError as exc:
        assert exc.code == 'published_card_changed'
    else:
        raise AssertionError('modified stamped card entered the model payload')
    finally:
        published_page.write_bytes(stamped)
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO documents VALUES(?,?,?,1)',
            (collection, first_rel, first_sha))
    def no_call(*args, **kwargs):
        raise AssertionError('completed provider request was repeated')
    original_retrieve = similarity.retrieve
    seen_exclusions = []
    def checked_retrieve(*args, **kwargs):
        seen_exclusions.append(set(kwargs.get('exclude', ())))
        result = original_retrieve(*args, **kwargs)
        assert all(row['path'] != first_rel for row in result)
        return result
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call), \
         patch.object(similarity, 'retrieve', side_effect=checked_retrieve), \
         patch.object(publisher, 'sync', side_effect=topical.TopicalError('synthetic_qmd_pause')):
        try:
            creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
                160, generation_runner=no_call, verification_runner=no_call)
        except topical.TopicalError as exc:
            assert exc.code == 'synthetic_qmd_pause'
        else:
            raise AssertionError('synthetic_post_publication_pause_missing')
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 2
    assert len(list((root / 'topical-create-review-proofs' / gid).glob('*.json'))) == 2
    reconcile.reconcile(root, ['sources'], list(staged.values()),
        trusted_card_ids=list(staged))
    interleaved = reconcile._read(root)
    assert interleaved['inFlight'] and set(interleaved['projection']) == set(staged)
    foreign = wiki / 'topical-v2' / ('f' * 24) / 'amber.md'
    foreign.parent.mkdir()
    foreign.write_text('# Conflicting card ID\n')
    try:
        reconcile.finish_created_batch(root, ['sources'],
            interleaved['inFlight']['batchId'], gid, list(staged.values()),
            'sources/new.md')
    except ValueError as exc:
        assert str(exc) == 'created_card_id_conflict'
    else:
        raise AssertionError('foreign_generation_id_collision_accepted')
    finally:
        foreign.unlink()
        foreign.parent.rmdir()
    late = wiki / 'late-legacy.md'
    late.write_text('# Late unrelated wiki page\nThis appeared after publication.\n')
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO documents VALUES(?,?,?,1)',
            (collection, late.name, hashlib.sha256(late.read_bytes()).hexdigest()))
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call), \
         patch.object(similarity, 'retrieve', side_effect=AssertionError('review_set_requeried')), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        done = creator.create_one(root, ['sources'], 'sources/new.md', cfg, 'codex',
            160, generation_runner=no_call, verification_runner=no_call)
    assert done['status'] == 'backend_created_synced', done
    assert seen_exclusions and all(excluded == {(gid, 'amber'), (gid, 'silver')}
                                   for excluded in seen_exclusions)
    assert calls == ['generate', 'verify', 'verify']
    assert hashlib.sha256(Path(first['path']).read_bytes()).hexdigest() == first_sha
    first_main = json.loads((root / 'topical-publication-proofs' / gid /
                            'amber.json').read_text())
    assert first_main['reviewPairs'] == []  # old partially completed publication
    additive = root / 'topical-create-review-proofs' / gid / 'amber.json'
    additive_data = json.loads(additive.read_text())
    selection_file = root / similarity.SELECTION_DIR / gid / 'amber.json'
    selection_bytes = selection_file.read_bytes()
    selection = json.loads(selection_bytes)
    assert selection['selected'] == ['legacy-card.md', nested_rel]
    assert additive_data['legacySelectionSha256'] == hashlib.sha256(
        selection_bytes).hexdigest()
    assert len(additive_data['pairs']) == 3
    assert {row['relation'] for row in additive_data['pairs']} == {
        'legacy_wiki_review_required', 'semantic_review_required'}
    import topical_story_scope
    record = publisher._attested(root, gid, 'amber')
    staged_sha = hashlib.sha256(topical.read_generation_bytes(
        root, gid, 'cards/amber.md')).hexdigest()
    changed_selection = dict(selection)
    changed_selection['selected'] = []
    selection_file.write_text(json.dumps(changed_selection)); selection_file.chmod(0o600)
    try:
        publisher.publish(root, gid, 'amber')
    except topical.TopicalError as exc:
        assert exc.code == 'similarity_selection_changed'
    else:
        raise AssertionError('reviewed legacy candidate was silently omitted')
    finally:
        selection_file.write_bytes(selection_bytes); selection_file.chmod(0o600)
    missing = additive.with_suffix('.held')
    additive.rename(missing)
    try:
        with patch.object(topical_story_scope, 'exact_story', return_value=True):
            try:
                publisher._create_review_proof(root, gid, 'amber', staged_sha, record)
            except topical.TopicalError as exc:
                assert exc.code == 'create_review_proof_required'
            else:
                raise AssertionError('scoped card lost its additive review proof')
    finally:
        missing.rename(additive)
    review_bytes = review.read_bytes()
    changed = json.loads(review_bytes)
    changed['decisions'] = [row for row in changed['decisions']
                            if row['pairSha256'] != additive_data['pairs'][0]['pairSha256']]
    review.write_text(json.dumps(changed)); review.chmod(0o600)
    try:
        publisher.publish(root, gid, 'amber')
    except topical.TopicalError as exc:
        assert exc.code == 'create_review_proof_changed'
    else:
        raise AssertionError('revoked legacy review was ignored')
    review.write_bytes(review_bytes); review.chmod(0o600)
    assert publisher.publish(root, gid, 'amber')['sha256'] == first_sha
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 2
    assert not (root / creator.JOURNAL).exists()
    state = reconcile._read(root)
    assert not state['queue'] and not state['inFlight']
    assert set(state['projection']) == {'amber', 'silver'}
    print(json.dumps({'legacyReviewRequired': True, 'staleHashRejected': True,
        'stableSiblingPair': True, 'partialPublishRecovered': True,
        'providerCallsNotRepeated': True, 'reviewProofRevocation': True}))
