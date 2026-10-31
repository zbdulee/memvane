"""Prediction keeps every candidate: an over-budget body is cut to its leading prefix.

A codepoint tokenizer and a stub ``laya.common`` reproduce the real sequence
layout ([CLS] head [SEP] [MASK] options [SEP] state [SEP]); no ML runs.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

PAD, CLS, SEP, MASK = 0, 1, 2, 3


class CodepointTokenizer:
    """One token per codepoint: lengths are exact and decoding is lossless."""
    mask_token = '[MASK]'
    mask_token_id, cls_token_id, sep_token_id, pad_token_id = MASK, CLS, SEP, PAD
    model_max_length = 8192
    is_fast = True

    def spans(self, text):
        return [(i, i + 1) for i in range(len(text))]

    def __call__(self, text, add_special_tokens=False, truncation=False, max_length=None,
                 return_offsets_mapping=False):
        spans = self.spans(text)
        if truncation: spans = spans[:max_length]
        result = {'input_ids': [self.token(text[a:b]) for a, b in spans]}
        if return_offsets_mapping: result['offset_mapping'] = spans
        return result

    @staticmethod
    def token(piece):
        return ord(piece) + 16 if len(piece) == 1 else 0x110000 + hash(piece) % 1000

    @staticmethod
    def decode(ids):
        return ''.join(chr(value - 16) for value in ids)


class MergingTokenizer(CodepointTokenizer):
    """Whole vocabulary words are one token; any partial word is one per char.

    Token count is not monotonic in the character prefix: ``synthetic`` costs
    one token where ``syntheti`` costs eight, like a real BPE merge.
    """
    LONG_WORD = 'm' * 300
    VOCAB = (LONG_WORD, 'synthetic', 'overflow', 'marker')

    def spans(self, text):
        spans, i = [], 0
        while i < len(text):
            word = next((w for w in self.VOCAB if text.startswith(w, i)), None)
            step = len(word) if word else 1
            spans.append((i, i + step)); i += step
        return spans


def _stub_laya():
    common = types.ModuleType('laya.common')
    common.QTYPES = {'choice': 0, 'score': 1, 'noul': 2}

    def render_options(q):
        return ['%s: %s' % (key, value) for key, value in q['crit'].items()]

    def build_sequence(tok, state, q, max_len=512, head_max_len=192, state_ids=None):
        head_ids = tok('%s question: %s' % (q['t'], q['ins']))['input_ids']
        opt_ids = [[tok.mask_token_id] + tok(' ' + option, truncation=True, max_length=48)['input_ids']
                   for option in render_options(q)]
        head_ids = head_ids[:max(8, head_max_len - sum(len(o) for o in opt_ids))]
        ids = [tok.cls_token_id] + head_ids + [tok.sep_token_id]
        markers = []
        for option in opt_ids:
            markers.append(len(ids))
            ids.extend(option)
        ids.append(tok.sep_token_id)
        room = max(0, max_len - len(ids) - 1)
        if state_ids is None:
            state_ids = tok(state)['input_ids']
        ids = ids + state_ids[:room] + [tok.sep_token_id]
        return ids[:max_len], [m for m in markers if m < max_len]

    common.render_options, common.build_sequence = render_options, build_sequence
    package = types.ModuleType('laya')
    package.common = common
    return {'laya': package, 'laya.common': common}


with patch.dict(sys.modules, _stub_laya()):
    from context_learning import laya_adapter as adapter
    from context_learning import compact_input

TOK = CodepointTokenizer()
CFG = {'max_len': 256, 'head_max_len': 128}
PROMPT = 'ESP32 모터 배선은 어떻게 해?'
TITLE = '# 18650 배터리와 보조배터리 비교\n'
LONG_BODY = TITLE + ''.join('전원 %03d번 문단: 모터 드라이버 전류가 커서 전압 강하가 생긴다. ' % i
                            for i in range(60))
SHORT_BODY = TITLE + '짧은 카드 본문.\n'


def card(card_id, body):
    return {'id': card_id, 'revision_sha256': '0' * 64, 'input_text': body,
            'input_kind': 'full_compact_wiki_body'}


def case(prompt=PROMPT, bodies=(LONG_BODY, SHORT_BODY), **extra):
    return {'request_id': 'r', 'prompt': prompt,
            'candidates': [card('wiki/%d.md' % i, body) for i, body in enumerate(bodies)], **extra}


def kept_body(item, prompt=PROMPT):
    """The body text the model actually sees, recovered from the state tokens."""
    first = item['ids'].index(SEP)
    state = TOK.decode(item['ids'][item['ids'].index(SEP, first + 1) + 1:-1])
    prefix = 'User request:\n' + prompt + '\nWiki card body:\n'
    assert state.startswith(prefix), state[:80]
    return state[len(prefix):]


class PredictionPairingTests(unittest.TestCase):
    def pairs(self, cases, *, with_gold=False):
        with patch.dict(sys.modules, _stub_laya()):
            return adapter._pairs(cases, TOK, CFG, with_gold=with_gold)

    def test_over_budget_body_is_scored_from_its_leading_prefix(self):
        self.assertGreater(len(TOK('User request:\n' + PROMPT + '\nWiki card body:\n'
                                   + LONG_BODY)['input_ids']), CFG['max_len'])
        long_item, short_item = self.pairs([case()])
        self.assertLessEqual(len(long_item['ids']), CFG['max_len'])
        self.assertTrue(long_item['truncated'])
        kept = kept_body(long_item)
        self.assertTrue(LONG_BODY.startswith(kept) and kept.startswith(TITLE))
        self.assertLess(len(kept), len(LONG_BODY))
        # Maximal: one more codepoint no longer fits the budget.
        with patch.dict(sys.modules, _stub_laya()):
            with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
                adapter._pair(PROMPT, card('x', LONG_BODY[:len(kept) + 1]), TOK, CFG)
        # Deterministic, and a fitting card is untouched.
        self.assertEqual(self.pairs([case()])[0], long_item)
        self.assertFalse(short_item['truncated'])
        self.assertEqual(kept_body(short_item), SHORT_BODY)

    def test_prefix_never_splits_a_multibyte_character(self):
        long_item, _ = self.pairs([case()])
        kept = kept_body(long_item)
        self.assertEqual(kept.encode('utf-8').decode('utf-8'), kept)
        self.assertTrue(LONG_BODY.encode('utf-8').startswith(kept.encode('utf-8')))

    def test_training_pairs_stay_untruncated(self):
        with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
            self.pairs([case(selected_ids=['wiki/0.md'])], with_gold=True)
        with patch.dict(sys.modules, _stub_laya()):
            with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
                adapter._pair(PROMPT, card('x', LONG_BODY), TOK, CFG)

    def test_request_that_cannot_fit_still_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
            self.pairs([case(prompt='질문 ' * 200)])

    def test_merging_tokenizer_keeps_a_budget_sized_token_boundary_prefix(self):
        # One 300-character word is a single token; any part of it costs one
        # token per character. A codepoint-prefix probe inside the word looks
        # over budget although the whole word fits: the cut must still take
        # the whole word and fill the room after it.
        tok = MergingTokenizer()
        word = tok.LONG_WORD
        body = '# Merge card\n' + 'z ' * 20 + word + ' z' * 500
        with patch.dict(sys.modules, _stub_laya()):
            item = adapter._pairs([case(bodies=(body,))], tok, CFG, with_gold=False)[0]
            skeleton, _ = sys.modules['laya.common'].build_sequence(
                tok, '', adapter.QUESTION, max_len=CFG['max_len'],
                head_max_len=CFG['head_max_len'], state_ids=[])
        room = CFG['max_len'] - len(skeleton)
        state_tokens = len(item['ids']) - len(skeleton)
        self.assertTrue(item['truncated'])
        self.assertIn(tok.token(word), item['ids'])
        self.assertLessEqual(state_tokens, room)
        self.assertGreaterEqual(state_tokens, room - 1)

    def test_heading_line_is_kept_whole_or_the_pair_fails_closed(self):
        title = '# ' + '긴제목' * 20 + '\n'
        body = title + '본문 ' * 400
        # Room for the request and the heading: the heading is kept whole.
        item = self.pairs([case(bodies=(body,))])[0]
        self.assertTrue(kept_body(item).startswith(title.rstrip('\n')))
        # A request that leaves room for only part of the heading fails closed.
        with patch.dict(sys.modules, _stub_laya()):
            skeleton, _ = sys.modules['laya.common'].build_sequence(
                TOK, '', adapter.QUESTION, max_len=CFG['max_len'],
                head_max_len=CFG['head_max_len'], state_ids=[])
        lead = len('User request:\n' + '\nWiki card body:\n')
        prompt = '질' * (CFG['max_len'] - len(skeleton) - lead - 10)
        self.assertLess(10, len(title) - 1)
        with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
            self.pairs([case(prompt=prompt, bodies=(body,))])
        with patch.dict(sys.modules, _stub_laya()):
            short = adapter._pair(prompt, card('x', '# 짧은\n' + '본문 ' * 400), TOK, CFG, fit_body=True)
        self.assertTrue(short['truncated'])

    def test_cancellation_is_checked_while_fitting(self):
        with patch.dict(sys.modules, _stub_laya()):
            with self.assertRaisesRegex(ValueError, 'prediction_cancelled'):
                adapter._pair(PROMPT, card('x', LONG_BODY), TOK, CFG, fit_body=True,
                              cancelled=lambda: True)

    def test_truncation_probe_overflows_both_supported_budgets(self):
        for max_len, head in ((1024, 256), (8192, 256)):
            cfg = {'max_len': max_len, 'head_max_len': head}
            with patch.dict(sys.modules, _stub_laya()):
                probe = adapter.truncation_probe((None, TOK, cfg))
                items = adapter._pairs([probe], TOK, cfg, with_gold=False)
                with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
                    adapter._pairs([{**probe, 'selected_ids': []}], TOK, cfg, with_gold=True)
            self.assertEqual([item['truncated'] for item in items], [True], max_len)
            self.assertLessEqual(len(items[0]['ids']), max_len)
            self.assertLessEqual(len(probe['candidates'][0]['input_text'].encode()), adapter.MAX_INPUT_BYTES)

    def test_content_checks_run_on_the_whole_body(self):
        with self.assertRaisesRegex(ValueError, 'mask_token_in_source'):
            self.pairs([case(bodies=(LONG_BODY + '[MASK]',))])


class SelectionBodyTests(unittest.TestCase):
    def test_oversize_body_is_capped_on_a_codepoint_boundary(self):
        body = '# 큰 카드\n' + '가' * 7000
        source = '---\ntitle: 큰 카드\n---\n' + body
        sha = hashlib.sha256(source.encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'compact_body_limit_or_empty'):
            compact_input.derive(source, sha)
        selected = compact_input.derive_for_selection(source, sha)
        kept = selected['body_text']
        self.assertTrue(selected['truncated'] and body.startswith(kept))
        self.assertLessEqual(len(kept.encode('utf-8')), compact_input.MAX_COMPACT_BODY_BYTES)
        self.assertGreater(len(kept.encode('utf-8')), compact_input.MAX_COMPACT_BODY_BYTES - 3)
        small = '---\ntitle: s\n---\nbody\n'
        self.assertEqual(compact_input.derive_for_selection(small, hashlib.sha256(small.encode()).hexdigest()),
                         compact_input.derive(small, hashlib.sha256(small.encode()).hexdigest()))

    def test_oversize_card_keeps_the_pool_selectable(self):
        from context_learning import live_select, inference_service
        from context_learning.contracts import digest
        with tempfile.TemporaryDirectory(prefix='qmd-pairing-') as temporary:
            state = Path(temporary).resolve(); state.chmod(0o700)
            artifact = state / 'head.artifact'; artifact.write_text('synthetic head'); artifact.chmod(0o600)
            def private(path, value):
                path.write_text(json.dumps(value)); path.chmod(0o600)
            private(state / 'active-checkpoint.json', {'schema_version': 1, 'artifact_path': str(artifact),
                'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()})
            adapter_path = Path(adapter.__file__).resolve()
            private(state / 'live-selector.json', {'schema': 'qmd-live-selector-v1',
                'trainerArgv': [sys.executable, str(adapter_path), str(state)],
                'timeoutSeconds': 5, 'maxRssMib': 512})
            big = '---\ntitle: big\n---\n# Big\n' + '본문 ' * 6000
            small = '---\ntitle: small\n---\nSmall body.\n'
            rows = [{'id': 'wiki/big.md', 'revision_sha256': digest(big), 'source_text': big},
                    {'id': 'wiki/small.md', 'revision_sha256': digest(small), 'source_text': small}]
            seen = []
            class Trainer:
                def __init__(self, *_a, **_k): pass
                def validate_prediction(self, *, cwd): return {}
            def service(_state, _policy, request, _sha, **_kwargs):
                one = request['cases'][0]; seen.append(one['candidates'])
                return {'schema_version': 1, 'predictions': [{'request_id': one['request_id'],
                    'input_sha256': one['input_sha256'], 'selected_ids': ['wiki/big.md']}]}, {}
            with patch.object(live_select, 'VerifiedLayaTrainer', Trainer), \
                    patch.object(inference_service, 'predict', service):
                ids, reason, _ = live_select.choose(state, 'Synthetic request', rows,
                                                    corpus_fingerprint='a' * 64)
            self.assertEqual((ids, reason), (['wiki/big.md'], 'selected'))
            self.assertEqual([c['id'] for c in seen[0]], ['wiki/big.md', 'wiki/small.md'])
            self.assertLessEqual(len(seen[0][0]['input_text'].encode()), adapter.MAX_INPUT_BYTES)


class ReceiptVersionTests(unittest.TestCase):
    def test_pairing_version_is_part_of_the_inference_receipt_checks(self):
        from context_learning import inference_identity
        self.assertEqual(inference_identity.CHECKS[-1],
                         'prediction_' + adapter.PREDICTION_PAIRING.replace('-', '_'))


if __name__ == '__main__':
    unittest.main()
