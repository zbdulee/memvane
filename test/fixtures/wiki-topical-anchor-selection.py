"""An anchored technical card binds a narrowed legacy set through publication replay."""
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
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_similarity as similarity
import topical_story_scope

with tempfile.TemporaryDirectory(prefix='qmd-anchor-selection-') as name:
    root = Path(name).resolve()
    patch.object(topical_story_scope, 'exact_story', return_value=True).start()
    patch.object(topical_story_scope, 'read', return_value={
        'wiki': root / '.auto-context/wiki',
        'collection': 'fixture-anchor-wiki'}).start()
    patch.object(similarity, '_automatic_decisions', return_value={}).start()
    (root / topical.MARKER).write_text('synthetic only\n')
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    collection = 'fixture-anchor-wiki'
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': [collection],
        'collectionPaths': {collection: '.auto-context/wiki'},
        'collectionRoles': {collection: 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    db = root / 'index.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE documents(collection TEXT, path TEXT, hash TEXT, active INTEGER)')
        for index in range(12):
            page = wiki / f'archive-{index:02}.md'
            page.write_text(f'Unrelated archive record {index}.\n' +
                            ('K42 has a separate archived identity note.\n' if index == 11 else ''))
            conn.execute('INSERT INTO documents VALUES(?,?,?,1)',
                (collection, page.name, hashlib.sha256(page.read_bytes()).hexdigest()))
    source_path = ('02_Characters/qmd-prospective/tr-character-keeper/'
                   'keeper-consent-k42-20261012.md')
    source = root / source_path
    source.parent.mkdir(parents=True)
    statement = 'Keeper K42 records corrections only with consent.'
    source.write_text(statement + '\n')
    response = {'schema': topical.SCHEMA, 'cards': [{
        'cardId': 'keeper-k42-corrections', 'title': 'K42 corrections',
        'category': 'world-rule', 'details': '', 'claims': [{
            'claimId': 'main', 'statement': statement, 'state': 'rule',
            'timeScope': 'synthetic', 'condition': '', 'evidence': [{
                'sourcePath': source_path, 'startLine': 1, 'endLine': 1,
                'quoteAnchor': statement}]}]}]}
    staged = backend.stage_generation_response(root, response,
        requested_sources=[source_path])
    gid = staged['generationId']
    card_id = 'keeper-k42-corrections'
    card = experiment.load_staged_card(root, gid, card_id)
    assert similarity._record_anchor(card) == 'k42'
    def qmd_call(_root, _runtime, argv):
        if argv[0] != 'vsearch':
            raise AssertionError(argv)
        return SimpleNamespace(stdout=json.dumps([
            {'file': f'qmd://{collection}/archive-{index:02}.md'}
            for index in range(12)]))
    with patch.object(publisher, '_qmd_runtime', return_value={'INDEX_PATH': str(db)}), \
         patch.object(publisher, '_call', side_effect=qmd_call):
        matches = similarity.review_candidates(root, card, gid)
    assert [row['path'] for row in matches] == [
        f'archive-{index:02}.md' for index in range(8)] + ['archive-11.md']
    selection = root / similarity.SELECTION_DIR / gid / (card_id + '.json')
    plan = json.loads(selection.read_text())
    assert len(plan['census']) == 12 and len(plan['selected']) == 9
    unresolved = similarity.evaluate(root, card, gid, matches)
    assert unresolved['status'] == 'pending_review' and len(unresolved['pairs']) == 9
    decisions = [{'pairSha256': row['pairSha256'], 'verdict': 'distinct',
                  'reviewerId': 'synthetic-reviewer',
                  'reason': 'The synthetic archive is a separate record.'}
                 for row in unresolved['pairs']]
    review = root / similarity.REVIEW_FILE
    review.write_text(json.dumps({'schema': 'qmd-topical-similarity-review-v1',
                                  'decisions': decisions}))
    review.chmod(0o600)
    resolved = similarity.evaluate(root, card, gid, matches)
    assert resolved['status'] == 'distinct_or_no_candidates'
    staged_sha = hashlib.sha256(topical.read_generation_bytes(
        root, gid, f'cards/{card_id}.md')).hexdigest()
    record = {'cardMarkdownSha256': staged_sha, 'engine': 'codex',
              'generationId': gid, 'cardId': card_id}
    with patch.object(publisher, '_attested', return_value=record):
        publisher.save_create_review_proof(root, gid, card_id, resolved['pairs'])
        first = publisher.publish(root, gid, card_id)
        second = publisher.publish(root, gid, card_id)
    assert first == second
    proof_path = root / 'topical-create-review-proofs' / gid / (card_id + '.json')
    proof = json.loads(proof_path.read_text())
    assert proof['legacySelectionSha256'] == hashlib.sha256(
        selection.read_bytes()).hexdigest()
    assert len(proof['pairs']) == 9
    # A later unrelated legacy page cannot change an already published batch.
    late = wiki / 'archive-late.md'
    late.write_text('A later independent archive.\n')
    with patch.object(publisher, '_attested', return_value=record):
        assert publisher.publish(root, gid, card_id) == first
    # Losing the frozen proof fails closed even when the page still exists.
    selection.unlink()
    with patch.object(publisher, '_attested', return_value=record):
        try:
            publisher.publish(root, gid, card_id)
        except topical.TopicalError as exc:
            assert exc.code == 'create_review_proof_changed'
        else:
            raise AssertionError('missing anchored selection was accepted')
print(json.dumps({'selectedLegacy': 9, 'census': 12, 'publishedReplay': True}))
