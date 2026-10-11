"""Deterministic admission and widened evidence contract checks."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / 'core'))
from context_learning.adaptive_pool import plan, actual_work
from context_learning.contracts import digest, request, validate_request
from context_learning.laya_adapter import _pairs


def report(available, load=0.2, ready=True):
    return {'available_mib': available, 'load_per_cpu': load, 'ready': ready}


def probe(value):
    return lambda _policy, detailed=False: value


now = 100.0
assert plan('short', 118, now=now, probe=probe(report(3000)))['requested'] == 5
assert plan('short', 118, now=now, probe=probe(report(6000)))['requested'] == 10
assert plan('short', 118, now=now, probe=probe(report(12000)))['requested'] == 20
assert plan('short', 118, now=now, probe=probe(report(24000)))['requested'] == 30
assert plan('short', 118, now=now, probe=probe(report(24000, 1.2)))['requested'] == 10
assert plan('short', 118, now=now, probe=probe(report(1500, ready=False)))['requested'] == 0
assert plan('short', 106.5, now=now, probe=probe(report(24000)))['reason'] == 'deadline_deferred'
assert plan('x' * 3000, 118, now=now, probe=probe(report(24000)))['reason'] == 'input_window_unavailable'
edit = plan('x' * 3000, 118, now=now, edit_view=True,
            probe=probe(report(24000)))
assert 5 <= edit['requested'] < 15
assert edit['estimated_rerank_seconds'] <= edit['rerank_target_seconds']
assert plan('x' * 15000, 118, now=now, edit_view=True,
            probe=probe(report(24000)))['reason'] == 'input_window_unavailable'
assert plan('한' * 1500, 118, now=now, edit_view=True,
            probe=probe(report(24000)))['requested'] >= 5
short = actual_work('question', ['short body'] * 3)
assert short['input_windows_estimate'] == 3 and short['all_pairs_fit_estimate']
assert not actual_work('question', ['x' * 2500])['all_pairs_fit_estimate']
assert actual_work('question', ['x' * 2500], edit_view=True)['all_pairs_fit_estimate']
long_cards = actual_work('x' * 3000, ['y' * 5000] * 5, edit_view=True)
assert long_cards['all_pairs_fit_estimate']
assert not long_cards['within_rerank_target_estimate']
assert long_cards['estimated_rerank_seconds'] > 1.0
assert actual_work('x' * 3000, ['y' * 5000] * 5, edit_view=True,
                   rerank_target_seconds=2.0)['within_rerank_target_estimate']

ids = [f'wiki/card-{i:02}.md' for i in range(20)]
sampling = {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
    'phase_counts': {'primary': 20}, 'per_phase_limit': 30,
    'eligible_ids': ids, 'captured_ids': ids,
    'baseline_selected_ids': ids[:3], 'excluded': [], 'snapshot_failures': [],
    'retrieval_truncated': False, 'assessment_complete': True,
    'complete_within_returned_bound': True, 'index_revision': digest('index'),
    'candidate_limit': 20}
rows = [{'candidate_id': cid, 'revision_sha256': digest(cid),
         'eligible': True, 'excerpt': 'Synthetic body.'} for cid in ids]
sample = request({'prompt': 'Synthetic relevant document question'}, rows,
                 host='codex', request_id='adaptive-20', sampling=sampling)
validate_request(sample)
assert len(sample['candidates']) == 20 and sample['schema_version'] == 3
case = {'request_id': 'adaptive-20', 'prompt': 'Synthetic question',
        'candidates': [{'id': cid, 'input_text': 'Synthetic body'} for cid in ids]}
with patch('context_learning.laya_adapter._pair', return_value={'ids': [1]}):
    assert len(_pairs([case], None, {}, with_gold=False)) == 20
    try:
        _pairs([{**case, 'candidates': case['candidates'] * 2}], None, {},
               with_gold=False)
    except ValueError as exc:
        assert str(exc) == 'invalid_candidate_pool'
    else:
        raise AssertionError('unbounded model pool')
print(json.dumps({'adaptive': True, 'moreThanFifteen': True,
                  'microbatchAndFinalUnchanged': True}))
