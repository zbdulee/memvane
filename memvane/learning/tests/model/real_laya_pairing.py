"""Opt-in actual Laya tokenizer and model: over-budget fixture cards are scored.

Run with the Laya runtime interpreter (``QMD_LAYA_PYTHON -s``, repository root on ``PYTHONPATH``) and
``QMD_LAYA_MODEL``. Synthetic fixture cards only; prints one JSON report.
"""
import json
import os
from pathlib import Path
import sys

from memvane.learning.context_learning import adapter, pairing
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.store import canonical

model, tok, cfg = adapter._load(os.environ['QMD_LAYA_MODEL'])
prompt = 'ESP32 모터 드라이버 배선은 어떻게 해?'
long_cards = [
    '# 18650 배터리와 보조배터리 전원 비교\n' + ''.join(
        '%d번 측정: 모터 기동 전류 때문에 전압이 순간적으로 떨어지고 ESP32가 리셋된다. ' % i
        for i in range(120)),
    '# Motor driver wiring notes\n' + ''.join(
        'Step %d: route the L298N enable pin to GPIO and keep grounds common. ' % i
        for i in range(200)),
]
short_cards = ['# 카드 %d\n짧은 위키 카드 본문 %d.\n' % (i, i) for i in range(13)]
budget_refused = 0
for body in long_cards:
    try:
        adapter._pair(prompt, {'input_text': body, 'input_kind': 'full_compact_wiki_body'}, tok, cfg)
    except ValueError as exc:
        budget_refused += str(exc) == 'model_token_budget_exceeded'
candidates = [{'id': 'wiki/%02d.md' % i, 'revision_sha256': '0' * 64, 'input_text': body,
               'input_kind': 'full_compact_wiki_body'}
              for i, body in enumerate(long_cards + short_cards)]
case = {'request_id': 'real-pairing', 'prompt': prompt, 'candidates': candidates}
case['input_sha256'] = digest(canonical(case))
timing = {}
result = adapter.predict_loaded(model, tok, cfg, {'schema_version': 1, 'artifact_path': '',
                                                  'cases': [case]}, timing=timing)
again = adapter.predict_loaded(model, tok, cfg, {'schema_version': 1, 'artifact_path': '',
                                                 'cases': [case]})
items = adapter._pairs([case], tok, cfg, with_gold=False)
# Worst recall pool: fifteen cards at the 16 KiB selection cap.
import time
heavy = []
for i in range(15):
    body, unit = '# 카드 %d 전원 설계\n' % i, '%d번 측정: synthetic motor driver 전류로 전압이 떨어진다. ' % i
    while len((body + unit).encode()) <= adapter.MAX_INPUT_BYTES: body += unit
    heavy.append({'id': 'w/%02d.md' % i, 'revision_sha256': '0' * 64, 'input_text': body,
                  'input_kind': 'full_compact_wiki_body'})
started = time.perf_counter()
adapter._pairs([{'request_id': 'heavy', 'prompt': prompt, 'candidates': heavy}], tok, cfg, with_gold=False)
heavy_seconds = time.perf_counter() - started
# The prepare probes: a legacy-view card is cut; an edit-view case scores every
# planned chunk pair.
probes = {}
legacy_probe, legacy_proves = pairing.pairing_probe((model, tok, cfg))
fitted = adapter._pairs([legacy_probe], tok, cfg, with_gold=False)
probes['1024'] = [item['truncated'] for item in fitted]
edit = adapter._load(os.environ['QMD_LAYA_MODEL'], input_view=adapter.EDIT_INPUT_VIEW)
edit_probe, edit_proves = pairing.pairing_probe(edit)
edit_timing = {}
adapter.predict_loaded(*edit, {'schema_version': 1, 'artifact_path': '',
                               'model_input_view': adapter.EDIT_INPUT_VIEW,
                               'cases': [edit_probe]}, timing=edit_timing)
probes['8192'] = [edit_proves(edit_timing)]
# Edit view on the long fixture pool: chunked pairs, max aggregation, a pair
# budget, and one training update whose gold card keeps a single positive.
long_prompt = ''.join('%d번 합성 질문: 모터 드라이버 배선과 전원 설계를 함께 묻는다. ' % i
                      for i in range(120))
edit_case = {'request_id': 'real-edit', 'prompt': long_prompt, 'candidates': candidates}
edit_case['input_sha256'] = digest(canonical(edit_case))
chunk_timing = {}
edit_result = adapter.predict_loaded(*edit, {
    'schema_version': 1, 'artifact_path': '', 'model_input_view': adapter.EDIT_INPUT_VIEW,
    'query_lane': 'prompt', 'pair_budget': 24, 'cases': [edit_case]}, timing=chunk_timing)
edit_again = adapter.predict_loaded(*edit, {
    'schema_version': 1, 'artifact_path': '', 'model_input_view': adapter.EDIT_INPUT_VIEW,
    'query_lane': 'prompt', 'pair_budget': 24, 'cases': [edit_case]})
import tempfile
with tempfile.TemporaryDirectory(prefix='qmd-real-edit-') as temporary:
    gold = {**edit_case, 'selected_ids': [candidates[0]['id']]}
    trained = adapter.train(os.environ['QMD_LAYA_MODEL'], {
        'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
        'artifact_path': str(Path(temporary) / 'head.artifact'), 'train': [gold]},
        max_updates=1)
print(json.dumps({'heavy_pairing_seconds': round(heavy_seconds, 3), 'probes': probes,'max_len': cfg['max_len'], 'long_card_tokens': [
                      len(tok(body, add_special_tokens=False)['input_ids']) for body in long_cards],
                  'strict_budget_refused': budget_refused,
                  'pairs': timing['pairs'], 'truncated_pairs': timing['truncated_pairs'],
                  'max_pair_tokens': max(len(item['ids']) for item in items),
                  'selected_ids': result['predictions'][0]['selected_ids'],
                  'deterministic': result == again,
                  'tokenize_seconds': timing['tokenize_seconds'],
                  'edit': {key: chunk_timing.get(key) for key in (
                      'query_chunks', 'card_chunks_max', 'chunked_cards', 'pairs',
                      'planned_pairs', 'dropped_pairs', 'truncated_pairs', 'pair_len_max',
                      'forward_seconds')},
                  'edit_selected_ids': edit_result['predictions'][0]['selected_ids'],
                  'edit_deterministic': edit_result == edit_again,
                  'edit_trained': trained}))
