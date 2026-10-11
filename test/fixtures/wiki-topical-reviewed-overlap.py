"""A reviewed complementary sibling keeps both immutable, verified cards."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_similarity as similarity

with tempfile.TemporaryDirectory(prefix='qmd-reviewed-overlap-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    source = root / 'sources/rules.md'
    source.parent.mkdir()
    lines = [
        'An old revision is not current.',
        'Deleting a source removes its card.',
        'A topic is valid only with its current source.',
        'Old cards retire after the replacement is verified.',
        'The path and hash identify the precise source.',
    ]
    source.write_text('\n'.join(lines) + '\n')
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['fixture-wiki'],
        'collectionPaths': {'fixture-wiki': '.auto-context/wiki'},
        'collectionRoles': {'fixture-wiki': 'wiki'},
        'recallStrategy': 'wikiOnly'}))
    revision = topical.source_snapshot(root, 'sources/rules.md', {})[0]
    def claim(cid, line):
        quote = lines[line - 1]
        return {'claimId': cid, 'statement': quote, 'state': 'rule',
            'timeScope': 'current', 'condition': '',
            'evidence': [{'sourcePath': 'sources/rules.md',
                'sourceRevisionSha256': revision['sha256'],
                'startLine': line, 'endLine': line, 'quoteAnchor': quote,
                'quoteSha256': topical.digest(quote.encode())}]}
    response = {'schema': topical.SCHEMA, 'cards': [
        {'cardId': 'source-lifecycle', 'title': 'Source lifecycle',
         'category': 'world-rule', 'details': '',
         'claims': [claim('shared-a', 1), claim('unique-a', 2),
                    claim('unique-a-second', 5)]},
        {'cardId': 'revision-validity', 'title': 'Revision validity',
         'category': 'world-rule', 'details': '',
         'claims': [claim('shared-b', 3), claim('unique-b', 4)]}]}
    stage = backend.stage_generation_response(root, response)
    gid = stage['generationId']
    cards = {cid: experiment.load_staged_card(root, gid, cid)
             for cid in ('source-lifecycle', 'revision-validity')}
    pages = {cid: topical.read_generation_bytes(root, gid, f'cards/{cid}.md')
             for cid in cards}
    def compare(left, right, *, path=None):
        item = {'path': path or f'topical-v2/{gid}/{right}.md',
                'pageSha256': hashlib.sha256(pages[right]).hexdigest(),
                'card': cards[right]}
        return similarity.evaluate(root, cards[left], gid, [item])
    before = compare('source-lifecycle', 'revision-validity')
    assert before['status'] == 'pending_review'
    pair = before['pairs'][0]['pairSha256']
    decision = {'pairSha256': pair, 'verdict': 'complementary_overlap',
        'reviewerId': 'synthetic-independent-reviewer',
        'reason': 'Both address source currency; deletion and verified retirement '
                  'are separately evidenced and both remain visible.',
        'sharedClaims': {'source-lifecycle': ['shared-a'],
                         'revision-validity': ['shared-b']},
        'uniqueClaims': {'source-lifecycle': ['unique-a', 'unique-a-second'],
                         'revision-validity': ['unique-b']}}
    review = root / similarity.REVIEW_FILE
    def save(row):
        review.write_text(json.dumps({'schema': 'qmd-topical-similarity-review-v1',
                                      'decisions': [row]}))
        review.chmod(0o600)
    save(decision)
    for left, right in (('source-lifecycle', 'revision-validity'),
                        ('revision-validity', 'source-lifecycle')):
        result = compare(left, right)
        assert result['status'] == 'distinct_or_no_candidates'
        assert result['pairs'][0]['verdict'] == 'complementary_overlap'
    assert compare('source-lifecycle', 'revision-validity',
                   path='topical-v2/foreign-generation/revision-validity.md')['status'] == 'pending_review'
    assert similarity.evaluate(root, cards['source-lifecycle'], gid, [{
        'path': 'legacy.md', 'pageSha256': hashlib.sha256(pages['revision-validity']).hexdigest(),
        'lead': 'legacy', 'legacy': True}])['status'] == 'pending_review'
    bad = dict(decision)
    bad['uniqueClaims'] = {**decision['uniqueClaims'],
                           'revision-validity': ['missing-claim']}
    save(bad)
    assert compare('source-lifecycle', 'revision-validity')['status'] == 'pending_review'
    omitted = dict(decision)
    omitted['uniqueClaims'] = {**decision['uniqueClaims'],
                               'source-lifecycle': ['unique-a']}
    save(omitted)
    assert compare('source-lifecycle', 'revision-validity')['status'] == 'pending_review'
    save(decision)
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    def verify(_argv, payload, _timeout, _cwd):
        checks = [{'claimId': c['claimId'], 'sourcePath': e['sourcePath'],
                   'quoteSha256': e['quoteSha256'], 'quoteAnchor': e['quoteAnchor'],
                   'supported': True} for c in payload['card']['claims']
                  for e in c['evidence']]
        return {'verdict': 'pass', 'checks': checks, 'reasons': []}, None, 0
    for cid in cards:
        assert backend.run_verification_backend(root, gid, cid, cfg, 'codex',
            allow_backend_execution=True, runner=verify)['status'] == 'backend_pass'
        publisher.save_create_review_proof(root, gid, cid,
            compare(cid, next(other for other in cards if other != cid))['pairs'])
    for cid in cards:
        publisher.publish(root, gid, cid)
        assert topical.read_generation_bytes(root, gid, f'cards/{cid}.md') == pages[cid]
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 2
    assert publisher.publish(root, gid, 'revision-validity')['status'] == 'published_verified'
    # Generic projects may publish without a create proof. The publication
    # proof itself must still bind the complete complementary decision.
    (root / 'topical-create-review-proofs' / gid /
     'revision-validity.json').unlink()
    assert publisher.publish(root, gid, 'revision-validity')['status'] == 'published_verified'
    altered = {**decision, 'reason': 'Changed rationale after publication.'}
    save(altered)
    try:
        publisher.publish(root, gid, 'revision-validity')
    except topical.TopicalError as exc:
        assert exc.code == 'publication_review_revoked', exc.code
    else:
        raise AssertionError('same_verdict_review_change_was_accepted')
    save(decision)
    assert publisher.publish(root, gid, 'revision-validity')['status'] == 'published_verified'
    save({'pairSha256': pair, 'verdict': 'unclear',
          'reviewerId': decision['reviewerId'], 'reason': 'synthetic revocation'})
    try:
        publisher.publish(root, gid, 'revision-validity')
    except topical.TopicalError as exc:
        assert exc.code == 'publication_review_revoked', exc.code
    else:
        raise AssertionError('review_revocation_was_accepted')
    print(json.dumps({'reviewedSiblingOverlap': True, 'unmodifiedCards': True,
                      'legacyAndForeignRejected': True, 'revocationRejected': True}))
