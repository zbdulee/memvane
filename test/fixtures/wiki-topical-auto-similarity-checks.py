"""Synthetic adapter receipts for automatic semantic comparison; no provider."""
import hashlib
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
import wiki_topical_similarity as similarity
import wiki_topical_publish as publisher

os.environ['QMD_SETUP_GUARD_FIXTURE'] = '1'
with tempfile.TemporaryDirectory(prefix='qmd-auto-similarity-') as temporary:
    root = Path(temporary).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    wiki = root / '.auto-context/wiki'
    wiki.mkdir(parents=True)
    gid = 'a' * 24
    staged = root / 'topical-generations' / gid / 'cards'
    staged.mkdir(parents=True, mode=0o700)
    proposed_bytes = b'Lantern fuel requires a signed ledger.\n'
    (staged / 'new-rule.md').write_bytes(proposed_bytes)
    (staged / 'new-rule.md').chmod(0o600)
    legacy = wiki / 'legacy.md'
    legacy.write_text('Silver owls guard the autumn gate.\n')
    cfg = {'extractor': {'builtins': ['codex']}}
    policy = root / scope.AUTO_POLICY
    policy.write_text(json.dumps({'compileCfg': cfg}))
    policy.chmod(0o600)
    argv = backend.worker.resolve_extractor_argv(cfg, 'codex')
    proposed = {'cardId': 'new-rule', 'claims': [{'statement': 'Signed ledger',
        'state': 'rule', 'timeScope': 'synthetic', 'condition': ''}]}
    def match(page):
        body = page.read_bytes()
        return {'path': page.relative_to(wiki).as_posix(),
                'pageSha256': hashlib.sha256(body).hexdigest(),
                'lead': body.decode(), 'legacy': True}
    calls = []
    def distinct(_argv, payload, _timeout, _cwd):
        calls.append(payload['pairSha256'])
        assert payload['pageA']['content'] == proposed_bytes.decode()
        return {'verdict': 'distinct', 'reason': 'Different explicit rules.',
                'sharedFacts': [], 'uniqueToA': ['Signed ledger'],
                'uniqueToB': ['Autumn gate']}, None, 0
    with patch.object(scope, 'exact_story', return_value=True), patch.object(
            scope, 'read', return_value={'wiki': wiki, 'collection': 'w'}):
        first = similarity.auto_review_pending(root, proposed, gid, [match(legacy)],
            cfg, 'codex', distinct, recovery_only=False)
        assert first['status'] == 'distinct_or_no_candidates', first
        second = similarity.auto_review_pending(root, proposed, gid, [match(legacy)],
            cfg, 'codex', distinct, recovery_only=True)
        assert second['status'] == 'distinct_or_no_candidates' and len(calls) == 1
        receipts = list((root / 'topical-backend-audit').glob('similarity.*.attempt.json'))
        assert len(receipts) == 1
        row = json.loads(receipts[0].read_text())
        assert row['state'] == 'completed' and row['requestContext']['pageB']['content'] == legacy.read_text()
        assert row['runtimeMetrics']['wallMs'] >= 0
        attestation = {'synthetic': 'reviewed-pass'}
        with patch.object(publisher, '_attested', return_value=attestation):
            proof_sha = publisher.save_create_review_proof(root, gid, 'new-rule',
                first['pairs'])
            assert isinstance(proof_sha, str) and len(proof_sha) == 64
            publisher._save_publication_proof(root, gid, 'new-rule',
                hashlib.sha256(proposed_bytes).hexdigest(), attestation,
                {'pairs': first['pairs']})
            assert publisher._publication_proof(root, gid, 'new-rule',
                hashlib.sha256(proposed_bytes).hexdigest(), attestation)
        legacy.write_text('The gate now follows a different rule.\n')
        assert similarity._review(root) == {}
        assert similarity.evaluate(root, proposed, gid, [match(legacy)])['pending'] == 1

        failing = wiki / 'failed.md'
        failing.write_text('Copper cranes count the winter stars.\n')
        def failure(*_args):
            calls.append('failed')
            return None, 'synthetic_timeout', 124
        pending = similarity.auto_review_pending(root, proposed, gid,
            [match(failing)], cfg, 'codex', failure, recovery_only=False)
        assert pending['status'] == 'pending_review' and pending['reason'] == 'synthetic_timeout'
        retried = similarity.auto_review_pending(root, proposed, gid,
            [match(failing)], cfg, 'codex', distinct, recovery_only=False)
        assert retried['status'] == 'distinct_or_no_candidates'
        attempts = [json.loads(p.read_text()) for p in
            (root / 'topical-backend-audit').glob('similarity.*.attempt.json')]
        assert sorted(x['state'] for x in attempts) == ['completed', 'completed', 'failed']
        assert len(calls) == 3
        duplicate = wiki / 'duplicate.md'
        duplicate.write_text('Lantern fuel also requires a signed ledger.\n')
        def same(_argv, _payload, _timeout, _cwd):
            calls.append('duplicate')
            return {'verdict': 'duplicate', 'reason': 'Same ledger requirement.',
                    'sharedFacts': ['Signed ledger'], 'uniqueToA': [],
                    'uniqueToB': []}, None, 0
        held = similarity.auto_review_pending(root, proposed, gid,
            [match(duplicate)], cfg, 'codex', same, recovery_only=False)
        assert held['status'] == 'pending_review' and held['pending'] == 1
        assert held['reason'] == 'similarity_duplicate'
        held_again = similarity.auto_review_pending(root, proposed, gid,
            [match(duplicate)], cfg, 'codex', same, recovery_only=False)
        assert held_again['status'] == 'pending_review' and calls.count('duplicate') == 1
        uncertain = wiki / 'uncertain.md'
        uncertain.write_text('Golden moths trace the midnight wind.\n')
        def lost(*_args):
            calls.append('uncertain')
            raise RuntimeError('synthetic_lost_result')
        try:
            similarity.auto_review_pending(root, proposed, gid,
                [match(uncertain)], cfg, 'codex', lost, recovery_only=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError('uncertain transport was accepted')
        pending_uncertain = similarity.auto_review_pending(root, proposed, gid,
            [match(uncertain)], cfg, 'codex', lost, recovery_only=False)
        assert pending_uncertain['reason'] == 'similarity_attempt_uncertain'
        assert calls.count('uncertain') == 1
        link = wiki / 'unsafe.md'
        link.symlink_to(legacy)
        try:
            similarity._page_bytes(root, 'unsafe.md')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_candidate_missing'
        else:
            raise AssertionError('symlinked wiki page was read')
        fifo = wiki / 'blocked.md'
        os.mkfifo(fifo)
        try:
            similarity._page_bytes(root, 'blocked.md')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_candidate_missing'
        else:
            raise AssertionError('nonregular wiki page was read')
        nested = wiki / 'nested'
        nested.mkdir()
        (nested / 'safe.md').write_text('Nested safe content.\n')
        assert similarity._page_bytes(root, 'nested/safe.md') == b'Nested safe content.\n'
        nested.rename(wiki / 'nested-real')
        nested.symlink_to(wiki / 'nested-real', target_is_directory=True)
        try:
            similarity._page_bytes(root, 'nested/safe.md')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_candidate_missing'
        else:
            raise AssertionError('symlinked nested wiki directory was read')
        private = root / 'private.md'
        private.write_text('Outside the wiki.\n')
        os.link(private, wiki / 'linked.md')
        try:
            similarity._page_bytes(root, 'linked.md')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_candidate_missing'
        else:
            raise AssertionError('hard-linked external content was read')
        many = []
        for index in range(10):
            page = wiki / f'unrelated-{index}.md'
            page.write_text(f'Independent archive rule {index}.\n')
            many.append(match(page))
        before = len(calls)
        round_one = similarity.auto_review_pending(root, proposed, gid, many,
            cfg, 'codex', distinct, recovery_only=False)
        assert round_one['reason'] == 'similarity_review_deferred'
        assert round_one['reviewedThisPass'] == 8 and len(calls) - before == 8
        round_two = similarity.auto_review_pending(root, proposed, gid, many,
            cfg, 'codex', distinct, recovery_only=False)
        assert round_two['status'] == 'distinct_or_no_candidates'
        assert len(calls) - before == 10
        replay = similarity.auto_review_pending(root, proposed, gid, many,
            cfg, 'codex', distinct, recovery_only=True)
        assert replay['status'] == 'distinct_or_no_candidates'
        assert len(calls) - before == 10
        target_pair = similarity._dedup_payload(root, gid, 'new-rule',
                                                many[0])['pairSha256']
        with patch.object(similarity, '_dedup_payload',
                          wraps=similarity._dedup_payload) as checked:
            limited = similarity._review(root, required_pairs={target_pair})
        assert set(limited) == {target_pair}
        assert checked.call_count == 1, checked.call_count
        technical = {'cardId': 'keeper-k42-corrections', 'sourceRevisions': [
            {'path': '02_Characters/qmd-prospective/tr-character-keeper/'
                     'keeper-consent-k42-20261012.md'}]}
        assert similarity._record_anchor(technical) == 'k42'
        candidate_census = [{'path': f'legacy-{i:02}.md',
            '_body': (b'k42 exact source cross-reference' if i == 11
                      else f'unrelated archive {i}'.encode())}
            for i in range(12)]
        selected = similarity._selected_legacy(candidate_census,
            [row['path'] for row in candidate_census], 'k42')
        assert selected == [f'legacy-{i:02}.md' for i in range(8)] + [
            'legacy-11.md']
        assert len(similarity._selected_legacy(candidate_census, [], None)) == 12
        auto_dir = root / '.auto-context'
        auto_dir.rename(root / 'saved-auto-context')
        auto_dir.symlink_to(root / 'saved-auto-context', target_is_directory=True)
        try:
            similarity._page_bytes(root, 'legacy.md')
        except topical.TopicalError as exc:
            assert exc.code == 'similarity_candidate_missing'
        else:
            raise AssertionError('symlinked wiki ancestor was read')
print(json.dumps({'completedReceiptReused': True, 'changedPageInvalidated': True,
                  'failedAttemptRetriedOnce': True, 'duplicateHeld': True,
                  'uncertainNotRepeated': True, 'providerCalls': 0}))
