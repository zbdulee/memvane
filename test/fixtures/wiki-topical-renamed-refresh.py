"""A completed three-card response with renamed IDs settles without regeneration."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_replacement as replacement
import wiki_topical_similarity as similarity


with tempfile.TemporaryDirectory(prefix='qmd-renamed-refresh-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    source = root / 'sources/ledger.md'
    source.parent.mkdir()
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['fixture-wiki'],
        'collectionPaths': {'fixture-wiki': '.auto-context/wiki'},
        'collectionRoles': {'fixture-wiki': 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    source.write_text('A lantern record names a keeper.\n'
                      'Missing records remain unverified.\n'
                      'Relighting requires a signed check.\n')
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}

    def card(cid, line, quote):
        revision = topical.source_snapshot(root, 'sources/ledger.md', {})[0]
        return {'cardId': cid, 'title': cid, 'category': 'world-rule',
            'details': '', 'claims': [{'claimId': cid + '-claim',
            'statement': quote, 'state': 'rule', 'timeScope': 'current',
            'condition': '', 'evidence': [{'sourcePath': 'sources/ledger.md',
            'sourceRevisionSha256': revision['sha256'], 'startLine': line,
            'endLine': line, 'quoteAnchor': quote,
            'quoteSha256': hashlib.sha256(quote.encode()).hexdigest()}]}]}

    old_response = {'schema': topical.SCHEMA, 'cards': [
        card('ledger-records', 1, 'A lantern record names a keeper.'),
        card('missing-status', 2, 'Missing records remain unverified.'),
        card('reignition', 3, 'Relighting requires a signed check.')]}
    old = backend.stage_generation_response(root, old_response)
    old_gid = old['generationId']
    old_ids = [row['cardId'] for row in old_response['cards']]
    publisher._publication_similarity = lambda *_: {
        'status': 'distinct_or_no_candidates', 'pairs': []}
    calls = []

    def verify(_argv, payload, _timeout, _cwd):
        calls.append('verify')
        checks = [{'claimId': claim['claimId'], 'sourcePath': span['sourcePath'],
            'quoteSha256': span['quoteSha256'], 'quoteAnchor': span['quoteAnchor'],
            'supported': True} for claim in payload['card']['claims']
            for span in claim['evidence']]
        return {'verdict': 'pass', 'checks': checks, 'reasons': []}, None, 0

    for cid in old_ids:
        backend.run_verification_backend(root, old_gid, cid, cfg, 'codex',
            allow_backend_execution=True, runner=verify)
        publisher.save_create_review_proof(root, old_gid, cid, [])
        publisher.publish(root, old_gid, cid)
    old_cards = [experiment.load_staged_card(root, old_gid, cid)
                 for cid in old_ids]
    reconcile.reconcile(root, ['sources'], old_cards, trusted_card_ids=old_ids)
    source.write_text('A lantern record names a keeper.\n'
                      'Missing records remain unverified.\n'
                      'Relighting requires a signed check.\n'
                      'The keeper must record three checks.\n')
    new_response = {'schema': topical.SCHEMA, 'cards': [
        card('ledger-recording', 1, 'A lantern record names a keeper.'),
        card('missing-status', 2, 'Missing records remain unverified.'),
        card('relighting-status', 3, 'Relighting requires a signed check.')]}
    new_ids = [row['cardId'] for row in new_response['cards']]
    # Reusing an ID cannot drop an unchanged old quote.
    forged_cards = json.loads(json.dumps(old_cards))
    forged_cards[0]['cardId'] = 'ledger-recording'
    forged_cards[1]['claims'][0]['evidence'][0]['quoteAnchor'] = (
        'The keeper must record three checks.')
    forged_cards[2]['cardId'] = 'relighting-status'
    try:
        replacement.derive(root, old_cards, forged_cards,
            {cid: {'state': 'excluded_stale'} for cid in old_ids},
            ['sources/ledger.md'], reconcile._scan(root, ['sources'], ()),
            'ledger-records')
    except topical.TopicalError as exc:
        assert exc.code == 'replacement_lineage_unresolved', exc.code
    else:
        raise AssertionError('same_id_dropped_anchor_accepted')

    def generate(_argv, _payload, _timeout, _cwd):
        calls.append('generate')
        return new_response, None, 0

    with patch.object(similarity, 'retrieve', return_value=[]), \
         patch.object(similarity, 'evaluate', side_effect=lambda _r,c,_g,_m: {
             'status': 'distinct_or_no_candidates', 'pairs': [],
             'proposedSha256': c['cardId'], 'pending': 0}), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        with patch.object(refresh, '_verification',
                          side_effect=RuntimeError('synthetic_after_receipt_crash')):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_after_receipt_crash'
            else:
                raise AssertionError('crash_missing')
        journal = json.loads((root / refresh.JOURNAL).read_text())
        assert journal['cardIds'] == new_ids
        assert [(link['oldCardId'], link['newCardId']) for link in
                journal['replacementLineage']['links']] == [
            ('ledger-records', 'ledger-recording'),
            ('missing-status', 'missing-status'),
            ('reignition', 'relighting-status')]
        forged = json.loads(json.dumps(journal))
        forged['replacementLineage']['links'][0]['newCardId'] = 'missing-status'
        refresh._save(root, forged)
        try:
            refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                cfg, 'codex', allow_backend_execution=True,
                generation_runner=generate, verification_runner=verify)
        except topical.TopicalError as exc:
            assert exc.code == 'replacement_lineage_changed', exc.code
        else:
            raise AssertionError('forged_lineage_accepted')
        refresh._save(root, journal)
        with patch.object(reconcile, 'finish_backend_batch',
                          side_effect=RuntimeError('synthetic_settlement_crash')):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_settlement_crash'
            else:
                raise AssertionError('settlement_crash_missing')
        generation_id = journal['generationId']
        new_cards = [experiment.load_staged_card(root, generation_id, cid)
                     for cid in new_ids]
        original_finish = reconcile.finish_backend_batch
        def finish_after_session(*args, **kwargs):
            # Reconcile after old retirement but immediately before settlement.
            reconcile.safe_projection(root, ['sources'], new_cards,
                trusted_card_ids=new_ids)
            return original_finish(*args, **kwargs)
        foreign = wiki / 'topical-v2' / '000000000000000000000001'
        foreign.mkdir()
        os.link(root / 'topical-retired' / old_gid / 'ledger-records.md',
                foreign / 'ledger-records.md')
        with patch.object(reconcile, 'finish_backend_batch',
                          side_effect=finish_after_session):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except ValueError as exc:
                assert str(exc) == 'replacement_card_id_conflict', exc
            else:
                raise AssertionError('foreign_old_id_page_accepted')
        (foreign / 'ledger-records.md').unlink()
        foreign.rmdir()
        with patch.object(reconcile, 'finish_backend_batch',
                          side_effect=finish_after_session):
            result = refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                cfg, 'codex', allow_backend_execution=True,
                generation_runner=generate, verification_runner=verify)
    assert result['status'] == 'backend_verified_synced', result
    assert calls.count('generate') == 1 and calls.count('verify') == 6
    state = reconcile._read(root)
    assert not state['queue'] and not state['inFlight']
    assert set(state['projection']) == set(new_ids)
    assert len(list((root / 'topical-retired' / old_gid).glob('*.md'))) == 3
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 3
