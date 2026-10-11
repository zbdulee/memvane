"""Completed scoped verification receipt replays after an attestation crash."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import topical_story_scope as scope
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_create as creator
import wiki_topical_experiment as experiment
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_similarity as similarity
import wiki_compile_worker as worker

with tempfile.TemporaryDirectory(prefix='qmd-transport-replay-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    (root / '01_Settings').mkdir()
    quote = 'The amber bell marks dawn.'
    (root / '01_Settings/scene.md').write_text(quote + '\n')
    with patch.object(scope, 'exact_story', return_value=True), \
         patch.object(scope, 'read', return_value={}):
        revision = topical.source_snapshot(root, '01_Settings/scene.md', {})[0]
        card = {'cardId': 'amber-bell', 'title': 'Amber bell', 'category': 'world-rule',
                'details': '', 'claims': [{'claimId': 'dawn', 'statement': quote,
                'state': 'rule', 'timeScope': 'current', 'condition': '',
                'evidence': [{'sourcePath': '01_Settings/scene.md',
                'sourceRevisionSha256': revision['sha256'], 'startLine': 1,
                'endLine': 1, 'quoteAnchor': quote,
                'quoteSha256': topical.digest(quote.encode())}]}]}
        staged = backend.stage_generation_response(root,
            {'schema': topical.SCHEMA, 'cards': [card]})
        gid = staged['generationId']
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    (root / scope.AUTO_POLICY).write_text(json.dumps({'compileCfg': cfg}))
    calls = []
    def runner(_argv, payload, _timeout, _cwd):
        calls.append('verify')
        checks = [{'claimId': claim['claimId'], 'sourcePath': evidence['sourcePath'],
            'quoteSha256': evidence['quoteSha256'],
            'quoteAnchor': evidence['quoteAnchor'], 'supported': True}
            for claim in payload['card']['claims']
            for evidence in claim['evidence']]
        return {'verdict': 'pass', 'checks': checks, 'reasons': [],
                '_qmd': {'reasoningEffort': 'medium'}}, None, 0
    def no_call(*_args, **_kwargs):
        raise AssertionError('completed_verification_repeated')
    old_fixture = os.environ.get('QMD_SETUP_GUARD_FIXTURE')
    os.environ['QMD_SETUP_GUARD_FIXTURE'] = '1'
    try:
        with patch.object(scope, 'exact_story', return_value=True), \
             patch.object(scope, 'read', return_value={}), \
             patch.object(backend, 'publish_pass_attestation',
                          side_effect=RuntimeError('synthetic_after_receipt_crash')):
            try:
                backend.run_verification_backend(root, gid, 'amber-bell', cfg, 'codex',
                    allow_backend_execution=True, runner=runner)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_after_receipt_crash'
            else:
                raise AssertionError('crash_not_reproduced')
        audit = next((root / 'topical-backend-audit').glob('verification.*.attempt.json'))
        saved = audit.read_bytes()
        assert json.loads(saved)['state'] == 'completed' and calls == ['verify']
        altered = json.loads(saved)
        altered['transportSha256'] = '0' * 64
        audit.write_text(json.dumps(altered) + '\n')
        for recovery_only in (False, True):
            with patch.object(scope, 'exact_story', return_value=True), \
                 patch.object(scope, 'read', return_value={}):
                outcome = refresh._verification(root, gid, 'amber-bell', cfg, 'codex',
                    no_call, recovery_only=recovery_only)
            assert outcome == (None, 'verification_policy_changed') and calls == ['verify']
        audit.write_bytes(saved)
        with patch.object(scope, 'exact_story', return_value=True), \
             patch.object(scope, 'read', return_value={}):
            outcome = refresh._verification(root, gid, 'amber-bell', cfg, 'codex',
                no_call, recovery_only=True)
        assert outcome == ('backend_pass', None) and calls == ['verify']
        backend_record = root / 'topical-attestations/backend' / gid / 'amber-bell.json'
        assert backend_record.is_file()

        next_source = root / '01_Settings/next.md'
        next_source.write_text('The silver bell marks dusk.\n')
        next_card = {'cardId': 'silver-bell', 'title': 'Silver bell',
            'category': 'world-rule', 'details': '', 'claims': [{
            'claimId': 'dusk', 'statement': 'The silver bell marks dusk.',
            'state': 'rule', 'timeScope': 'current', 'condition': '',
            'evidence': [{'sourcePath': '01_Settings/next.md',
                'startLine': 1, 'endLine': 1,
                'quoteAnchor': 'The silver bell marks dusk.'}]}]}
        def generation_runner(*_args):
            calls.append('generate')
            return {'schema': topical.SCHEMA, 'cards': [next_card],
                    '_qmd': {'reasoningEffort': 'medium'}}, None, 0
        with patch.object(scope, 'exact_story', return_value=True), \
             patch.object(scope, 'read', return_value={}), \
             patch.object(scope, 'validate_wiki_candidates', return_value=None), \
             patch.object(backend, 'stage_generation_response',
                          side_effect=RuntimeError('synthetic_after_generation_receipt')):
            try:
                backend.run_generation_backend(root, ['01_Settings/next.md'], cfg,
                    'codex', allow_backend_execution=True,
                    existing_wiki_candidates=[], runner=generation_runner)
            except RuntimeError as exc:
                assert str(exc) == 'synthetic_after_generation_receipt'
            else:
                raise AssertionError('generation_crash_not_reproduced')
        generation_audit = next((root / 'topical-backend-audit').glob('generation.*.attempt.json'))
        generation_saved = generation_audit.read_bytes()
        altered = json.loads(generation_saved)
        altered['transportSha256'] = '0' * 64
        generation_audit.write_text(json.dumps(altered) + '\n')
        with patch.object(scope, 'exact_story', return_value=True), \
             patch.object(scope, 'read', return_value={}):
            generated, pending = refresh._generation(root, ['01_Settings/next.md'],
                cfg, 'codex', [], no_call, recovery_only=True)
        assert generated is None and pending == 'generation_policy_changed'
        generation_audit.write_bytes(generation_saved)
        with patch.object(scope, 'exact_story', return_value=True), \
             patch.object(scope, 'read', return_value={}):
            generated, pending = refresh._generation(root, ['01_Settings/next.md'],
                cfg, 'codex', [], no_call, recovery_only=True)
        assert pending is None and generated['generationId'] and calls == ['verify', 'generate']
    finally:
        if old_fixture is None: os.environ.pop('QMD_SETUP_GUARD_FIXTURE', None)
        else: os.environ['QMD_SETUP_GUARD_FIXTURE'] = old_fixture

with tempfile.TemporaryDirectory(prefix='qmd-retry-transport-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    (root / 'sources').mkdir()
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    (root / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['synthetic-wiki'],
        'collectionPaths': {'synthetic-wiki': '.auto-context/wiki'},
        'collectionRoles': {'synthetic-wiki': 'wiki'}, 'recallStrategy': 'wikiOnly'}))
    reconcile.reconcile(root, ['sources'], [], trusted_card_ids=[])
    (root / 'sources/retry.md').write_text('The copper bell marks noon.\n')
    reconcile.reconcile(root, ['sources'], [], trusted_card_ids=[])
    claimed = reconcile.start_batch(root, ['sources'], [], trusted_card_ids=[])
    assert claimed['started'] and set(claimed['batch']['sources']) == {'sources/retry.md'}
    batch = claimed['batch']
    cfg = {'extractor': {'builtins': ['codex']},
           'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}
    journal = {'schema': 'qmd-topical-create-v1', 'batchId': batch['batchId'],
        'sourcePath': 'sources/retry.md',
        'sourceSha256': batch['snapshot']['sources/retry.md']['sha256'],
        'sourceRoots': ['sources'], 'engine': 'codex', 'skipPaths': [],
        'compilePolicySha256': topical.digest(topical.encoded(cfg)),
        'phase': 'generation_failed_wait', 'generationId': None,
        'cardIds': [], 'preCandidates': []}
    reconcile._atomic(root / creator.JOURNAL, journal)
    payload = experiment.generation_contract(root, ['sources/retry.md'])
    payload.update(existingWikiCandidates=[], timeout=420)
    response = {'schema': topical.SCHEMA, 'cards': [{
        'cardId': 'copper-bell', 'title': 'Copper bell', 'category': 'world-rule',
        'details': '', 'claims': [{'claimId': 'noon',
        'statement': 'The copper bell marks noon.', 'state': 'rule',
        'timeScope': 'current', 'condition': '', 'evidence': [{
        'sourcePath': 'sources/retry.md', 'startLine': 1, 'endLine': 1,
        'quoteAnchor': 'The copper bell marks noon.'}]}]}]}
    argv = worker.resolve_extractor_argv(cfg, 'codex')
    returned, replay = backend._attempt(root, 'generation', payload, argv, 420,
        lambda *_: (response, None, 0), 'retry1', 2, 10, 20)
    assert returned == response and replay is False
    audit = next((root / 'topical-backend-audit').glob('generation.*.retry1.attempt.json'))
    saved = audit.read_bytes()
    changed = json.loads(saved)
    changed['transportSha256'] = '0' * 64
    audit.write_text(json.dumps(changed) + '\n')
    for recovery_only in (False, True):
        with patch.object(backend, 'stage_generation_response',
                          side_effect=AssertionError('tampered_retry_staged')):
            result = creator.create_one(root, ['sources'], 'sources/retry.md', cfg,
                'codex', 40, generation_runner=no_call,
                verification_runner=no_call, recovery_only=recovery_only)
        assert result == {'status': 'pending_review',
                          'reason': 'generation_policy_changed'}, result
        assert creator._read(root)['phase'] == 'generation_failed_wait'
    audit.write_bytes(saved)
    result = creator.create_one(root, ['sources'], 'sources/retry.md', cfg,
        'codex', 40, generation_runner=no_call,
        verification_runner=no_call, recovery_only=True)
    assert result == {'status': 'pending_review',
                      'reason': 'verification_requires_explicit_backend'}, result
    assert creator._read(root)['phase'] == 'generated'
    retry_receipt_rejected = True

rows = [{'path': f'legacy-{i}.md', 'lead': f'Legacy page {i}'}
        for i in range(33)]
rows.insert(7, {'path': 'topical-v2/' + 'a' * 24 + '/current.md',
                'lead': 'Scoped current card'})
with patch.object(scope, 'exact_story', return_value=True):
    selected = similarity.generation_context(Path('/synthetic'), rows)
assert selected == [{'path': 'topical-v2/' + 'a' * 24 + '/current.md',
                     'lead': 'Scoped current card'}]
assert len(rows) == 34 and similarity.generation_context(Path('/synthetic'), rows) == rows[:8]

print(json.dumps({'completedReceiptReplayed': True, 'tamperedTransportRejected': True,
                  'providerCalls': len(calls), 'generationRecovered': True,
                  'retryReceiptRejected': retry_receipt_rejected,
                  'scopedContextBounded': True}))
