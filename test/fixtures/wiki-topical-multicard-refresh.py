"""One changed source refreshes two old cards and promotes one new card."""
import json
import hashlib
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
import wiki_topical_similarity as similarity


with tempfile.TemporaryDirectory(prefix='qmd-multicard-refresh-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    source = root / 'sources/scene.md'
    source.parent.mkdir()
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['fixture-wiki'],
        'collectionPaths': {'fixture-wiki': '.auto-context/wiki'},
        'collectionRoles': {'fixture-wiki': 'wiki'},
        'recallStrategy': 'wikiOnly'}))
    source.write_text('The amber bell marks dawn.\nThe silver bell marks dusk.\n')
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    def card(cid, line, quote):
        revision = topical.source_snapshot(root, 'sources/scene.md', {})[0]
        return {'cardId': cid, 'title': cid, 'category': 'world-rule',
            'details': '', 'claims': [{'claimId': cid + '-claim',
            'statement': quote, 'state': 'rule', 'timeScope': 'current',
            'condition': '', 'evidence': [{'sourcePath': 'sources/scene.md',
            'sourceRevisionSha256': revision['sha256'],
            'startLine': line, 'endLine': line, 'quoteAnchor': quote,
            'quoteSha256': hashlib.sha256(quote.encode()).hexdigest()}]}]}
    old_response = {'schema': topical.SCHEMA, 'cards': [
        card('amber-bell', 1, 'The amber bell marks dawn.'),
        card('silver-bell', 2, 'The silver bell marks dusk.')]}
    old = backend.stage_generation_response(root, old_response)
    old_gid = old['generationId']
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
    for cid in ('amber-bell', 'silver-bell'):
        backend.run_verification_backend(root, old_gid, cid, cfg, 'codex',
            allow_backend_execution=True, runner=verify)
        publisher.save_create_review_proof(root, old_gid, cid, [])
        publisher.publish(root, old_gid, cid)
    old_cards = [experiment.load_staged_card(root, old_gid, cid)
                 for cid in ('amber-bell', 'silver-bell')]
    reconcile.reconcile(root, ['sources'], old_cards,
        trusted_card_ids=['amber-bell', 'silver-bell'])
    source.write_text('The amber bell marks dawn.\nThe silver bell marks dusk.\n'
                      'Both prior cards retire only after verification.\n')
    response = {'schema': topical.SCHEMA, 'cards': [
        card('amber-bell', 1, 'The amber bell marks dawn.'),
        card('silver-bell', 2, 'The silver bell marks dusk.'),
        card('verified-handoff', 3,
             'Both prior cards retire only after verification.')]}
    def generate(_argv, _payload, _timeout, _cwd):
        calls.append('generate')
        return response, None, 0
    with patch.object(similarity, 'retrieve', return_value=[]), \
         patch.object(similarity, 'evaluate', side_effect=lambda _r,c,_g,_m: {
             'status': 'distinct_or_no_candidates', 'pairs': [],
             'proposedSha256': c['cardId'], 'pending': 0}), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        with patch.object(backend, 'stage_generation_response',
                          side_effect=RuntimeError('synthetic_completed_receipt_crash')):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_completed_receipt_crash'
            else:
                raise AssertionError('completed_receipt_crash_missing')
        assert json.loads((root / refresh.JOURNAL).read_text())['phase'] == 'pre_retrieved'
        with patch.object(refresh, '_verification',
                          side_effect=RuntimeError('synthetic_staged_crash')):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_staged_crash'
            else:
                raise AssertionError('staged_crash_missing')
        journal_path = root / refresh.JOURNAL
        original_journal = json.loads(journal_path.read_text())
        assert original_journal['phase'] == 'generated'
        assert len(original_journal['cardIds']) == 3
        for field in ('cardIds', 'retireCardIds'):
            changed = dict(original_journal)
            changed[field] = changed[field][:-1]
            refresh._save(root, changed)
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except topical.TopicalError as exc:
                assert exc.code == 'refresh_card_set_changed', exc.code
            else:
                raise AssertionError('tampered_' + field + '_accepted')
        refresh._save(root, original_journal)
        publish_once = publisher.publish
        publish_calls = []
        def crash_after_first_publish(*args, **kwargs):
            published = publish_once(*args, **kwargs)
            publish_calls.append(published)
            if len(publish_calls) == 1:
                raise RuntimeError('synthetic_first_publish_crash')
            return published
        with patch.object(publisher, 'publish', side_effect=crash_after_first_publish):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_first_publish_crash'
            else:
                raise AssertionError('first_publish_crash_missing')
        assert len(list((wiki / 'topical-v2' / original_journal['generationId']).glob('*.md'))) == 1
        # An index failure before replacement ACK must leave both old pages
        # physically published. No retirement may start yet.
        with patch.object(publisher, 'sync',
                          side_effect=topical.TopicalError('synthetic_index_not_ready')):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except topical.TopicalError as exc:
                assert exc.code == 'synthetic_index_not_ready'
            else:
                raise AssertionError('pre_retirement_ack_was_skipped')
        assert len(list((wiki / 'topical-v2' / old_gid).glob('*.md'))) == 2
        assert not (root / 'topical-retired').exists()
        retire_once = publisher.retire_stale
        retire_calls = []
        def crash_after_first_retirement(*args, **kwargs):
            retired = retire_once(*args, **kwargs)
            retire_calls.append(retired)
            if len(retire_calls) == 1:
                raise RuntimeError('synthetic_first_retirement_crash')
            return retired
        with patch.object(publisher, 'retire_stale',
                          side_effect=crash_after_first_retirement):
            try:
                refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
                    cfg, 'codex', allow_backend_execution=True,
                    generation_runner=generate, verification_runner=verify)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_first_retirement_crash'
            else:
                raise AssertionError('first_retirement_crash_missing')
        assert len(list((root / 'topical-retired' / old_gid).glob('*.md'))) == 1
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
        assert not list((wiki / 'topical-v2' / old_gid).glob('*.md'))
        assert json.loads(journal_path.read_text())['phase'] == 'qmd_synced'
        new_cards = [experiment.load_staged_card(root,
            original_journal['generationId'], cid) for cid in original_journal['cardIds']]
        reconcile.safe_projection(root, ['sources'], new_cards,
            trusted_card_ids=original_journal['cardIds'])
        carried = reconcile.safe_projection(root, ['sources'], old_cards,
            trusted_card_ids=['amber-bell', 'silver-bell'])
        assert carried['verified-handoff']['state'] == 'excluded_pending_refresh'
        state_path = root / reconcile.STATE
        current_state = reconcile._read(root)
        tampered = json.loads(json.dumps(current_state))
        tampered['projection']['verified-handoff']['claims'][0]['claimId'] = 'foreign'
        reconcile._atomic(state_path, tampered)
        try:
            reconcile.finish_backend_batch(root, ['sources'],
                current_state['inFlight']['batchId'], original_journal['generationId'],
                new_cards, old_card_ids=['amber-bell', 'silver-bell'],
                settled_paths=['sources/scene.md'])
        except ValueError as exc:
            assert str(exc) == 'replacement_card_id_conflict'
        else:
            raise AssertionError('tampered_interleaving_accepted')
        reconcile._atomic(state_path, current_state)
        foreign = wiki / 'topical-v2' / '000000000000000000000001'
        foreign.mkdir()
        os.link(wiki / 'topical-v2' / original_journal['generationId'] /
                'verified-handoff.md', foreign / 'verified-handoff.md')
        try:
            reconcile.finish_backend_batch(root, ['sources'],
                current_state['inFlight']['batchId'], original_journal['generationId'],
                new_cards, old_card_ids=['amber-bell', 'silver-bell'],
                settled_paths=['sources/scene.md'])
        except ValueError as exc:
            assert str(exc) == 'replacement_card_id_conflict'
        else:
            raise AssertionError('foreign_generation_collision_accepted')
        (foreign / 'verified-handoff.md').unlink()
        foreign.rmdir()
        result = refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
            cfg, 'codex', allow_backend_execution=True,
            generation_runner=generate, verification_runner=verify)
    assert result['status'] == 'backend_verified_synced', result
    assert result['replacementCards'] == 3
    assert calls.count('generate') == 1 and calls.count('verify') == 5
    assert not (root / refresh.JOURNAL).exists()
    assert len(list((root / 'topical-retired' / old_gid).glob('*.md'))) == 2
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 3
    old_page = wiki / 'topical-v2' / old_gid / 'amber-bell.md'
    archive_page = root / 'topical-retired' / old_gid / 'amber-bell.md'
    old_page.parent.mkdir(exist_ok=True)
    os.link(archive_page, old_page)
    replay = publisher.retire_stale(root, old_gid, 'amber-bell',
                                    ['sources'], [old_cards[0]])
    assert replay['status'] == 'retired_stale' and not old_page.exists()
    state = reconcile._read(root)
    assert not state['queue'] and not state['inFlight']
    assert set(state['projection']) == {'amber-bell', 'silver-bell', 'verified-handoff'}

# A response may reuse another stale ID. Its old source must be covered by
# the selected refresh contract even if the model rewrites the new evidence
# to point at the selected source.
with tempfile.TemporaryDirectory(prefix='qmd-cross-source-refresh-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    (root / 'sources').mkdir()
    (root / 'sources/one.md').write_text('The amber bell marks dawn.\n')
    (root / 'sources/two.md').write_text('The silver bell marks dusk.\n')
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['fixture-wiki'],
        'collectionPaths': {'fixture-wiki': '.auto-context/wiki'},
        'collectionRoles': {'fixture-wiki': 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    def source_card(cid, source_path, quote):
        revision = topical.source_snapshot(root, source_path, {})[0]
        return {'cardId': cid, 'title': cid, 'category': 'world-rule',
            'details': '', 'claims': [{'claimId': cid + '-claim',
            'statement': quote, 'state': 'rule', 'timeScope': 'current',
            'condition': '', 'evidence': [{'sourcePath': source_path,
            'sourceRevisionSha256': revision['sha256'], 'startLine': 1,
            'endLine': 1, 'quoteAnchor': quote,
            'quoteSha256': hashlib.sha256(quote.encode()).hexdigest()}]}]}
    old = backend.stage_generation_response(root, {'schema': topical.SCHEMA,
        'cards': [source_card('amber-bell', 'sources/one.md',
                    'The amber bell marks dawn.'),
                  source_card('silver-bell', 'sources/two.md',
                    'The silver bell marks dusk.')]})
    old_gid = old['generationId']
    for cid in ('amber-bell', 'silver-bell'):
        backend.run_verification_backend(root, old_gid, cid, cfg, 'codex',
            allow_backend_execution=True, runner=verify)
        publisher.save_create_review_proof(root, old_gid, cid, [])
        publisher.publish(root, old_gid, cid)
    old_cards = [experiment.load_staged_card(root, old_gid, cid)
                 for cid in ('amber-bell', 'silver-bell')]
    reconcile.reconcile(root, ['sources'], old_cards,
        trusted_card_ids=['amber-bell', 'silver-bell'])
    (root / 'sources/one.md').write_text('The amber bell marks dawn.\nOne changed.\n')
    (root / 'sources/two.md').write_text('The silver bell marks dusk.\nTwo changed.\n')
    response = {'schema': topical.SCHEMA, 'cards': [
        source_card('amber-bell', 'sources/one.md', 'The amber bell marks dawn.'),
        source_card('silver-bell', 'sources/one.md', 'The amber bell marks dawn.')]}
    with patch.object(similarity, 'retrieve', return_value=[]):
        result = refresh.refresh_one(root, ['sources'], old_cards[0], old_gid,
            cfg, 'codex', allow_backend_execution=True,
            generation_runner=lambda *_: (response, None, 0),
            verification_runner=verify)
    assert result == {'status': 'pending_review',
                      'reason': 'replacement_source_mismatch'}, result
    assert not (root / 'topical-retired').exists()
    assert len(list(wiki.glob('topical-v2/*/*.md'))) == 2

# A primary old card may lose one of its sources. The verified replacement
# must cover its surviving source while the deleted path is settled as gone.
with tempfile.TemporaryDirectory(prefix='qmd-partial-delete-refresh-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    (root / 'sources').mkdir()
    (root / 'sources/one.md').write_text('The amber bell marks dawn.\n')
    (root / 'sources/two.md').write_text('The silver bell marks dusk.\n')
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['fixture-wiki'],
        'collectionPaths': {'fixture-wiki': '.auto-context/wiki'},
        'collectionRoles': {'fixture-wiki': 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    first = source_card('two-source-rule', 'sources/one.md',
                        'The amber bell marks dawn.')
    second = source_card('two-source-rule', 'sources/two.md',
                         'The silver bell marks dusk.')
    second['claims'][0]['claimId'] = 'two-source-rule-second-claim'
    first['claims'].extend(second['claims'])
    old = backend.stage_generation_response(root,
        {'schema': topical.SCHEMA, 'cards': [first]})
    old_gid = old['generationId']
    backend.run_verification_backend(root, old_gid, 'two-source-rule', cfg,
        'codex', allow_backend_execution=True, runner=verify)
    publisher.save_create_review_proof(root, old_gid, 'two-source-rule', [])
    publisher.publish(root, old_gid, 'two-source-rule')
    old_card = experiment.load_staged_card(root, old_gid, 'two-source-rule')
    reconcile.reconcile(root, ['sources'], [old_card],
        trusted_card_ids=['two-source-rule'])
    (root / 'sources/one.md').write_text('The amber bell marks dawn.\nOne changed.\n')
    (root / 'sources/two.md').unlink()
    response = {'schema': topical.SCHEMA, 'cards': [
        source_card('two-source-rule', 'sources/one.md',
                    'The amber bell marks dawn.')]}
    with patch.object(similarity, 'retrieve', return_value=[]), \
         patch.object(similarity, 'evaluate', side_effect=lambda _r,c,_g,_m: {
             'status': 'distinct_or_no_candidates', 'pairs': [],
             'proposedSha256': c['cardId'], 'pending': 0}), \
         patch.object(publisher, 'sync', return_value={'status': 'ready'}):
        result = refresh.refresh_one(root, ['sources'], old_card, old_gid,
            cfg, 'codex', allow_backend_execution=True,
            generation_runner=lambda *_: (response, None, 0),
            verification_runner=verify)
    assert result['status'] == 'backend_verified_synced', result
    assert not reconcile._read(root)['queue']
    assert (root / 'topical-retired' / old_gid / 'two-source-rule.md').is_file()

print(json.dumps({'multicardRefresh': True, 'oldCardsRetired': 2,
                  'newCardsPublished': 3, 'generationCalls': 1,
                  'verificationCalls': 3, 'queueSettled': True,
                  'crossSourceRetirementBlocked': True,
                  'journalTamperBlocked': True,
                  'retirementHardlinkReplay': True,
                  'partialSourceDeletionRecovered': True}))
