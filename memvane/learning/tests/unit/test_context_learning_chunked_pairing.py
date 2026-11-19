"""Edit-view pair plan: query and card chunks, max aggregation and the pair budget.

The codepoint tokenizer and stub ``laya.common`` of the prediction pairing
tests reproduce the real sequence layout; a fake model scores pairs from a
table, so no ML runs.
"""
from contextlib import nullcontext
import sys
import types
import unittest
from unittest.mock import patch

from memvane.learning.tests.unit.test_context_learning_prediction_pairing import (
    TOK, MergingTokenizer, _stub_laya, card)

# Neither module imports laya at load time; importing them outside the stub
# keeps them in sys.modules for later tests.
from memvane.learning.context_learning import adapter, pairing  # noqa: E402

EDIT = {'max_len': adapter.EDIT_INPUT_TOKENS, 'head_max_len': 256,
        'model_input_view': adapter.EDIT_INPUT_VIEW}
TITLE = '# 전원 설계 카드'


def sentences(word, count):
    return ''.join('%s %03d번 문장이다. ' % (word, i) for i in range(count))


def long_card(index, count=300):
    return TITLE + ' %d\n' % index + sentences('본문', count)


def make_case(prompt, bodies, request_id='r', **extra):
    return {'request_id': request_id, 'prompt': prompt, 'input_sha256': 'a' * 64,
            'candidates': [card('wiki/%d.md' % i, body) for i, body in enumerate(bodies)],
            **extra}


def plan(case, *, tok=TOK, cfg=EDIT, with_gold=False, **kw):
    stats = {}
    with patch.dict(sys.modules, _stub_laya()):
        items = adapter._pairs([case], tok, cfg, with_gold=with_gold, stats=stats, **kw)
    return items, stats


def state_parts(item):
    """(query chunk, card chunk) the model sees, decoded from the state tokens."""
    sep = 2
    first = item['ids'].index(sep)
    state = TOK.decode(item['ids'][item['ids'].index(sep, first + 1) + 1:-1])
    head, _, body = state.partition('\nWiki card body:\n')
    assert head.startswith('User request:\n')
    return head[len('User request:\n'):], body


class FakeLogits:
    def __init__(self, values):
        self.values = values
        self.shape = (len(values), 2)

    def float(self):
        return self


class Scores:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, _index):
        return self

    def tolist(self):
        return list(self.values)


class TableModel:
    """Scores each pair from ``table[(candidate, query chunk, card chunk)]``."""

    def __init__(self, table, default=.1):
        self.table, self.default, self.calls = table, default, []

    def parameters(self):
        return iter([types.SimpleNamespace(device='cpu')])

    def __call__(self, items):
        self.calls.append([len(item['ids']) for item in items])
        return FakeLogits([self.table.get((i['candidate_id'], i['query_chunk'], i['card_chunk']),
                                          self.default) for i in items]), None


FAKE_TORCH = types.SimpleNamespace(
    no_grad=nullcontext, set_num_threads=lambda n: None, set_num_interop_threads=lambda n: None,
    get_num_threads=lambda: 1, get_num_interop_threads=lambda: 1,
    softmax=lambda logits, *_: Scores(logits.values))


def predict(model, case, *, timing=None, **request):
    with patch.dict(sys.modules, {**_stub_laya(), 'torch': FAKE_TORCH}), \
            patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}):
        return adapter.predict_loaded(model, TOK, EDIT, {
            'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
            'cases': [case], **request}, timing=timing)


class QueryChunks(unittest.TestCase):
    def test_a_long_query_is_at_most_four_chunks_and_the_rest_is_counted(self):
        prompt = sentences('질문', 300)
        self.assertGreater(len(prompt), adapter.Q_MAX * adapter.Q_CHUNKS)
        for lane in adapter.LANES:
            items, stats = plan(make_case(prompt, ['# 짧은 카드\n본문.']), lane=lane)
            self.assertEqual(stats['query_chunks'], adapter.Q_CHUNKS)
            self.assertEqual(len(items), adapter.Q_CHUNKS)
            self.assertGreater(stats['dropped_query'], 0)
            for item in items:
                query, _ = state_parts(item)
                self.assertLessEqual(len(query), adapter.Q_MAX)
                self.assertIn(query, prompt)
            covered = sum(len(state_parts(i)[0]) for i in items)
            self.assertEqual(covered + stats['dropped_query'], len(prompt), lane)

    def test_the_prompt_lane_keeps_the_tail_and_the_edit_lane_the_head(self):
        prompt = sentences('질문', 300) + ' 마지막 요청: 배선을 알려줘'
        edit, _ = plan(make_case(prompt, ['# c\nb.']), lane='edit')
        last, _ = plan(make_case(prompt, ['# c\nb.']), lane='prompt')
        edit = {i['query_chunk']: state_parts(i)[0] for i in edit}
        last = {i['query_chunk']: state_parts(i)[0] for i in last}
        self.assertFalse(edit[3].endswith('배선을 알려줘'))
        self.assertTrue(last[3].endswith('배선을 알려줘'))
        self.assertEqual([edit[i] for i in range(3)], [last[i] for i in range(3)])

    def test_a_short_query_is_one_chunk(self):
        items, stats = plan(make_case('ESP32 배선?', ['# c\nb.']))
        self.assertEqual((len(items), stats['query_chunks'], stats['dropped_query']), (1, 1, 0))
        self.assertEqual(state_parts(items[0])[0], 'ESP32 배선?')


class CardChunks(unittest.TestCase):
    def test_a_long_card_is_at_most_three_chunks_each_led_by_its_heading(self):
        body = long_card(0)
        chunks, dropped = pairing.card_chunks(body, TOK)
        self.assertEqual(len(chunks), adapter.B_CHUNKS)
        self.assertGreater(dropped, 0)
        heading = TITLE + ' 0'
        for chunk in chunks:
            self.assertTrue(chunk.startswith(heading), chunk[:30])
            self.assertLessEqual(len(chunk), adapter.B_MAX + len(heading) + 1)
        self.assertEqual(chunks[0], body[:len(chunks[0])])
        for chunk in chunks[1:]:
            self.assertIn(chunk[len(heading) + 1:], body)

    def test_a_card_within_the_body_budget_is_one_chunk(self):
        body = TITLE + '\n' + '가' * (adapter.B_MAX - len(TITLE) - 1)
        self.assertEqual(len(body), adapter.B_MAX)
        self.assertEqual(pairing.card_chunks(body, TOK), ([body], 0))

    def test_chunks_never_split_a_multibyte_character(self):
        tok = MergingTokenizer()
        body = TITLE + '\n' + ('한글과 synthetic overflow marker 문장. ' * 200)
        chunks, _ = pairing.card_chunks(body, tok)
        self.assertGreater(len(chunks), 1)
        raw = body.encode('utf-8')
        for chunk in chunks:
            piece = chunk.split('\n', 1)[1] if chunk is not chunks[0] else chunk
            self.assertIn(piece.encode('utf-8'), raw)


class MaxAggregation(unittest.TestCase):
    def test_a_card_scores_the_highest_probability_of_its_pairs(self):
        prompt = sentences('질문', 120)
        case = make_case(prompt, [long_card(0), long_card(1), '# 짧은\n본문.'])
        # Card 0 is needed only through its second card chunk; card 1 nowhere.
        model = TableModel({('wiki/0.md', 1, 2): .91, ('wiki/1.md', 0, 0): .49,
                            ('wiki/2.md', 0, 0): .6})
        result = predict(model, case)
        self.assertEqual(result['predictions'][0]['selected_ids'], ['wiki/0.md', 'wiki/2.md'])

    def test_ties_keep_pool_order_and_the_policy_keeps_three(self):
        case = make_case('질문', ['# %d\n본문.' % i for i in range(5)])
        model = TableModel({}, default=.7)
        result = predict(model, case)
        self.assertEqual(result['predictions'][0]['selected_ids'],
                         ['wiki/0.md', 'wiki/1.md', 'wiki/2.md'])


class PairBudget(unittest.TestCase):
    def test_pairs_follow_the_fixed_order_and_the_rest_is_dropped(self):
        prompt = sentences('질문', 40)       # two query chunks
        bodies = [long_card(0, 80), '# 1\n짧다.', long_card(2, 80)]
        full, stats = plan(make_case(prompt, bodies))
        order = [(i['candidate_id'], i['query_chunk'], i['card_chunk']) for i in full]
        self.assertEqual(stats['query_chunks'], 2)
        self.assertEqual(order[:3], [('wiki/0.md', 0, 0), ('wiki/1.md', 0, 0), ('wiki/2.md', 0, 0)])
        self.assertEqual(order[3:6], [('wiki/0.md', 1, 0), ('wiki/1.md', 1, 0), ('wiki/2.md', 1, 0)])
        self.assertEqual(order[6:], [('wiki/0.md', 0, 1), ('wiki/0.md', 1, 1),
                                     ('wiki/2.md', 0, 1), ('wiki/2.md', 1, 1)])
        self.assertEqual(stats['dropped_pairs'], 0)
        for budget in (1, 2, 5):
            kept, counted = plan(make_case(prompt, bodies), pair_budget=budget)
            self.assertEqual([(i['candidate_id'], i['query_chunk'], i['card_chunk'])
                              for i in kept], order[:budget])
            self.assertEqual(counted['dropped_pairs'], len(order) - budget)

    def test_the_prompt_tail_is_scored_within_the_shipped_budget(self):
        # Ten candidates, four query chunks and the prompt lane's 24 pairs:
        # after the ten first-chunk pairs the tail chunk goes to every card.
        prompt = sentences('질문', 300) + ' 마지막 요청: 배선을 알려줘'
        bodies = ['# 카드 %d\n짧은 본문.' % i for i in range(10)]
        items, stats = plan(make_case(prompt, bodies), lane='prompt', pair_budget=24)
        self.assertEqual(stats['query_chunks'], 4)
        order = [(i['candidate_id'], i['query_chunk']) for i in items]
        self.assertEqual(order[:10], [('wiki/%d.md' % i, 0) for i in range(10)])
        self.assertEqual(order[10:20], [('wiki/%d.md' % i, 3) for i in range(10)])
        self.assertEqual(order[20:], [('wiki/%d.md' % i, 1) for i in range(4)])
        tail = [i for i in items if i['query_chunk'] == 3]
        self.assertTrue(all(state_parts(i)[0].endswith('배선을 알려줘') for i in tail))
        # The edit lane keeps chunk order: its last chunk is not reached.
        edit, _ = plan(make_case(prompt, bodies), lane='edit', pair_budget=24)
        self.assertEqual(sorted({i['query_chunk'] for i in edit}), [0, 1, 2])

    def test_a_three_chunk_prompt_takes_its_last_chunk_first(self):
        self.assertEqual(pairing.plan_order([('a', 3, 1), ('b', 3, 1)], 'prompt'),
                         [('a', 0, 0), ('b', 0, 0), ('a', 2, 0), ('b', 2, 0),
                          ('a', 1, 0), ('b', 1, 0)])
        self.assertEqual(pairing.plan_order([('a', 3, 1), ('b', 3, 1)], 'edit'),
                         [('a', 0, 0), ('b', 0, 0), ('a', 1, 0), ('b', 1, 0),
                          ('a', 2, 0), ('b', 2, 0)])

    def test_the_hard_limit_caps_every_request(self):
        prompt = sentences('질문', 300)
        bodies = [long_card(i) for i in range(30)]
        items, stats = plan(make_case(prompt, bodies), pair_budget=adapter.P_HARD)
        self.assertEqual(len(items), adapter.P_HARD)
        self.assertEqual(stats['dropped_pairs'], 30 * 4 * 3 - adapter.P_HARD)

    def test_a_card_left_without_a_pair_is_not_selected(self):
        case = make_case('질문', ['# %d\n본문.' % i for i in range(3)])
        model = TableModel({('wiki/2.md', 0, 0): .99})
        result = predict(model, case, pair_budget=2)
        self.assertEqual(result['predictions'][0]['selected_ids'], [])
        self.assertEqual(sum(len(c) for c in model.calls), 2)

    def test_request_keys_are_checked(self):
        case = make_case('질문', ['# c\nb.'])
        for bad in ({'pair_budget': 0}, {'pair_budget': adapter.P_HARD + 1},
                    {'pair_budget': True}, {'query_lane': 'other'}):
            with self.assertRaisesRegex(ValueError, 'invalid_predict_request'):
                predict(TableModel({}), case, **bad)


class SharedInput(unittest.TestCase):
    def test_training_and_prediction_plan_the_same_pairs(self):
        prompt = sentences('질문', 100)
        bodies = [long_card(0), '# 1\n짧다.']
        heldout, _ = plan(make_case(prompt, bodies))
        trained, _ = plan(make_case(prompt, bodies, selected_ids=['wiki/0.md']), with_gold=True)
        key = lambda i: (i['candidate_id'], i['query_chunk'], i['card_chunk'], i['ids'])
        self.assertEqual([key(i) for i in trained], [key(i) for i in heldout])

    def test_a_gold_card_keeps_one_positive_pair_and_other_cards_stay_negative(self):
        prompt = sentences('질문', 100)
        trained, _ = plan(make_case(prompt, [long_card(0), long_card(1)],
                                    selected_ids=['wiki/0.md']), with_gold=True)
        table = {('wiki/0.md', 1, 1): .8}
        def score(pairs):
            return [table.get((p['candidate_id'], p['query_chunk'], p['card_chunk']), .2)
                    for p in pairs]
        kept = pairing.single_positive(trained, score)
        gold = [i for i in kept if i['candidate_id'] == 'wiki/0.md']
        other = [i for i in kept if i['candidate_id'] == 'wiki/1.md']
        self.assertEqual([(i['query_chunk'], i['card_chunk'], i['gold_label']) for i in gold],
                         [(1, 1, 1)])
        self.assertEqual(len(other), len([i for i in trained if i['candidate_id'] == 'wiki/1.md']))
        self.assertTrue(all(i['gold_label'] == 0 for i in other))

    def test_a_tie_keeps_the_first_planned_pair(self):
        items = [{'request_id': 'r', 'candidate_id': 'c', 'gold_label': 1, 'n': n}
                 for n in range(3)]
        kept = pairing.single_positive(items, lambda pairs: [.5] * len(pairs))
        self.assertEqual([i['n'] for i in kept], [0])


class Budgets(unittest.TestCase):
    def test_a_long_prompt_fits_the_pressure_padded_limit(self):
        prompt = sentences('질문', 2000)
        case = make_case(prompt, [long_card(i) for i in range(10)])
        model = TableModel({}, default=.2)
        timing = {}
        with patch.dict(sys.modules, {**_stub_laya(), 'torch': FAKE_TORCH}), \
                patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}):
            adapter.predict_loaded(model, TOK, EDIT, {
                'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
                'cases': [case], 'pair_budget': 24, 'query_lane': 'prompt'},
                max_batch_size=4, max_padded_tokens=4096, timing=timing)
        self.assertEqual(timing['pairs'], 24)
        self.assertEqual(timing['query_chunks'], 4)
        self.assertLessEqual(timing['pair_len_max'], 4096)
        self.assertTrue(all(len(c) <= 4 and max(c) * len(c) <= 4096 for c in model.calls))

    def test_batches_run_in_length_order_and_results_join_by_card(self):
        bodies = ['# %d\n' % i + '본문 ' * (40 * (5 - i)) for i in range(5)]
        case = make_case('질문', bodies)
        model = TableModel({('wiki/4.md', 0, 0): .9, ('wiki/0.md', 0, 0): .8})
        timing = {}
        result = predict(model, case, timing=timing)
        lengths = [n for call in model.calls for n in call]
        self.assertEqual(lengths, sorted(lengths))
        self.assertEqual(result['predictions'][0]['selected_ids'], ['wiki/4.md', 'wiki/0.md'])
        self.assertEqual(timing['padded_total'], sum(max(c) * len(c) for c in model.calls))
        self.assertEqual((timing['planned_pairs'], timing['pairs'], timing['dropped_pairs']),
                         (5, 5, 0))

    def test_cancellation_between_batches_reports_the_batches_run(self):
        case = make_case('질문', ['# %d\n본문.' % i for i in range(8)])
        model = TableModel({})
        timing, calls = {}, []
        def cancelled():
            calls.append(1)
            return len(model.calls) >= 1
        with patch.dict(sys.modules, {**_stub_laya(), 'torch': FAKE_TORCH}), \
                patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}):
            with self.assertRaisesRegex(ValueError, 'prediction_cancelled'):
                adapter.predict_loaded(model, TOK, EDIT, {
                    'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
                    'cases': [case]}, max_batch_size=2, timing=timing, cancelled=cancelled)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(timing['cancelled_after_batches'], 1)


class WholeInputChecks(unittest.TestCase):
    def test_content_and_pool_checks_run_on_the_whole_input(self):
        long = long_card(0)
        with self.assertRaisesRegex(ValueError, 'mask_token_in_source'):
            plan(make_case('질문', [long + '[MASK]']))
        with self.assertRaisesRegex(ValueError, 'invalid_complete_input'):
            plan(make_case('질문', ['가' * (adapter.MAX_INPUT_BYTES)]))
        with self.assertRaisesRegex(ValueError, 'invalid_candidate_pool'):
            plan(make_case('질문', ['# c\nb.'] * 31))
        duplicated = make_case('질문', ['# a\nb.', '# c\nd.'])
        duplicated['candidates'][1]['id'] = duplicated['candidates'][0]['id']
        with self.assertRaisesRegex(ValueError, 'invalid_candidate_pool'):
            plan(duplicated)

    def test_legacy_and_experiment_views_keep_the_leading_prefix(self):
        for view, size in ((adapter.LEGACY_INPUT_VIEW, 1024),
                           (adapter.EXPERIMENT_INPUT_VIEW, 1024)):
            cfg = {'max_len': size, 'head_max_len': 256, 'model_input_view': view}
            items, stats = plan(make_case('질문', [long_card(0)]), cfg=cfg)
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0]['truncated'])
            self.assertNotIn('query_chunk', items[0])
            self.assertEqual(stats, {})


class Proofs(unittest.TestCase):
    def test_the_chunking_probe_needs_two_query_and_two_card_chunks(self):
        with patch.dict(sys.modules, _stub_laya()):
            case, proves = pairing.pairing_probe((None, TOK, EDIT))
            items, stats = plan(case)
        self.assertGreaterEqual(stats['query_chunks'], 2)
        self.assertGreaterEqual(stats['card_chunks_max'], 2)
        timing = {**stats, 'pairs': len(items), 'planned_pairs': len(items)}
        self.assertTrue(proves(timing))
        self.assertFalse(proves({**timing, 'pairs': len(items) - 1}))
        self.assertFalse(proves({**timing, 'query_chunks': 1}))
        with patch.dict(sys.modules, _stub_laya()):
            legacy, legacy_proof = pairing.pairing_probe(
                (None, TOK, {'max_len': 1024, 'head_max_len': 256}))
        self.assertEqual(legacy['request_id'], 'inference-prepare-truncation')
        self.assertTrue(legacy_proof({'truncated_pairs': 1}))

    def test_the_prepare_probe_fits_the_pressure_padded_limit(self):
        # Under memory pressure the service admits 4,096 padded tokens. The
        # earlier edit-view probe filled the 8,192-token window and was refused
        # there (inference_prepare_model_token_budget_exceeded); every pair of
        # the chunking probe fits and all planned pairs are scored.
        with patch.dict(sys.modules, _stub_laya()):
            old = pairing.truncation_probe((None, TOK, EDIT))
            old_items = adapter._pairs([old], TOK, {**EDIT, 'model_input_view': None},
                                       with_gold=False)
        self.assertGreater(max(len(i['ids']) for i in old_items), 4096)
        with self.assertRaisesRegex(ValueError, 'model_token_budget_exceeded'):
            adapter.prediction_batches(old_items, max_padded_tokens=4096, max_size=1)
        with patch.dict(sys.modules, _stub_laya()):
            case, proves = pairing.pairing_probe((None, TOK, EDIT))
        timing = {}
        model = TableModel({})
        with patch.dict(sys.modules, {**_stub_laya(), 'torch': FAKE_TORCH}), \
                patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}):
            adapter.predict_loaded(model, TOK, EDIT, {
                'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
                'cases': [case]}, max_batch_size=1, max_padded_tokens=4096, timing=timing)
        self.assertLessEqual(timing['pair_len_max'], 4096)
        self.assertTrue(proves(timing))

    def test_the_smoke_plan_check_counts_what_it_drops(self):
        with patch.dict(sys.modules, _stub_laya()):
            pairing.smoke_chunk_plan(TOK, {'max_len': 1024, 'head_max_len': 256},
                                     {'max_position_embeddings': 8192})
            short = types.SimpleNamespace(is_fast=True, model_max_length=1024)
            pairing.smoke_chunk_plan(short, {}, {'max_position_embeddings': 8192})
            with patch.object(adapter, 'Q_CHUNKS', 1), \
                    self.assertRaisesRegex(ValueError, 'silent_model_token_truncation'):
                with patch.object(pairing, 'query_chunks', lambda *a: (['q'], 0)):
                    pairing.smoke_chunk_plan(TOK, {'max_len': 1024, 'head_max_len': 256},
                                             {'max_position_embeddings': 8192})


if __name__ == '__main__':
    unittest.main()
