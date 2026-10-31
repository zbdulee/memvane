"""Opt-in actual Laya tokenizer and model: over-budget fixture cards are scored.

Run with the Laya runtime interpreter (``QMD_LAYA_PYTHON -I``) and
``QMD_LAYA_MODEL``. Synthetic fixture cards only; prints one JSON report.
"""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import laya_adapter as adapter
from context_learning.contracts import digest
from context_learning.store import canonical

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
print(json.dumps({'max_len': cfg['max_len'], 'long_card_tokens': [
                      len(tok(body, add_special_tokens=False)['input_ids']) for body in long_cards],
                  'strict_budget_refused': budget_refused,
                  'pairs': timing['pairs'], 'truncated_pairs': timing['truncated_pairs'],
                  'max_pair_tokens': max(len(item['ids']) for item in items),
                  'selected_ids': result['predictions'][0]['selected_ids'],
                  'deterministic': result == again,
                  'tokenize_seconds': timing['tokenize_seconds']}))
