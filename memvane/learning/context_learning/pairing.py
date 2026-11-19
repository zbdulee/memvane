"""Edit-view pair plan of the Laya selection adapter (``adapter.PREDICTION_PAIRING``).

Training and prediction call ``pair_plan`` for every edit-view case: a query
over ``Q_MAX`` tokens becomes at most ``Q_CHUNKS`` query chunks, a card body
over ``B_MAX`` tokens at most ``B_CHUNKS`` card chunks that each keep the
card's heading line, and pairs past the request's pair budget are dropped in
a fixed order and counted. The limits live in ``adapter.py``; a change to
this plan changes ``PREDICTION_PAIRING`` with them. The legacy and
experiment views keep the adapter's own leading-prefix pairing
(``truncation_probe`` proves it).
"""
import hashlib
import json

from . import adapter as A

_SENTENCE_ENDS = '.!?。！？\n'


def token_spans(tok, text):
    """Contiguous codepoint spans, one per token, covering all of ``text``.

    A zero-width token joins the next one; text after the last token end
    joins the last span. Fails closed when the tokenizer gives no offsets.
    """
    if not getattr(tok, 'is_fast', False):
        raise ValueError('model_token_budget_exceeded')
    spans, start = [], 0
    for end in A._body_token_ends(tok, text):
        if end > start:
            spans.append((start, end))
            start = end
    if start < len(text):
        if spans:
            spans[-1] = (spans[-1][0], len(text))
        else:
            spans.append((0, len(text)))
    return spans


def windows(text, spans, target, limit, cursor=0):
    """Up to ``limit`` consecutive token windows of at most ``target`` tokens.

    The boundary rule of ``edit_segments.split_with_offsets``: a window ends
    at a sentence or paragraph end within its last quarter (at most 160
    tokens back) when there is one, otherwise at the token budget.
    """
    found = []
    while cursor < len(spans) and len(found) < limit:
        ceiling = min(len(spans), cursor + target)
        end = ceiling
        if ceiling < len(spans):
            floor = max(cursor + target * 3 // 4, ceiling - 160)
            for candidate in range(ceiling, floor, -1):
                if text[spans[candidate - 1][1] - 1] in _SENTENCE_ENDS:
                    end = candidate
                    break
        found.append((cursor, end))
        cursor = end
    return found


def query_chunks(prompt, tok, lane):
    """(chunks, dropped tokens). The edit lane keeps the first windows; the
    prompt lane keeps the first three and the tail, where a request typically
    follows a pasted text."""
    spans = token_spans(tok, prompt)
    kept = windows(prompt, spans, A.Q_MAX, A.Q_CHUNKS)
    if kept[-1][1] < len(spans) and lane == 'prompt':
        kept = kept[:A.Q_CHUNKS - 1]
        kept.append((max(kept[-1][1], len(spans) - A.Q_MAX), len(spans)))
    used = sum(end - start for start, end in kept)
    return [prompt[spans[a][0]:spans[b - 1][1]] for a, b in kept], len(spans) - used


def card_chunks(body, tok):
    """(chunks, dropped tokens): the body, or up to B_CHUNKS windows; every
    window after the first starts with the card's heading line."""
    spans = token_spans(tok, body)
    if len(spans) <= A.B_MAX:
        return [body], 0
    heading = ''
    start = len(body) - len(body.lstrip())
    if body.startswith('#', start):
        line = body[:A._title_end(body)].strip()
        if len(tok(line, add_special_tokens=False)['input_ids']) <= A.TITLE_PREFIX_TOKENS:
            heading = line + '\n'
    room = A.B_MAX - (len(tok(heading, add_special_tokens=False)['input_ids']) if heading else 0)
    kept = windows(body, spans, A.B_MAX, 1)
    kept += windows(body, spans, room, A.B_CHUNKS - 1, cursor=kept[0][1])
    chunks = [body[spans[a][0]:spans[b - 1][1]] for a, b in kept]
    chunks[1:] = [heading + chunk for chunk in chunks[1:]]
    return chunks, len(spans) - kept[-1][1]


def plan_order(ranked, lane='edit'):
    """Pair order over ``(card, query chunks, card chunks)`` in rank order:
    every card with the first query and card chunks, then the remaining query
    chunks (each over every card in rank order), then the remaining card
    chunks. The prompt lane takes its last query chunk first among the
    remaining ones, so a request written after a long paste is scored before
    the middle of the paste; the edit lane keeps chunk order."""
    order = [(card, 0, 0) for card, _queries, _chunks in ranked]
    queries = max((count for _card, count, _chunks in ranked), default=0)
    rest = list(range(1, queries))
    if lane == 'prompt' and len(rest) > 1:
        rest = rest[-1:] + rest[:-1]
    for query in rest:
        order += [(card, query, 0) for card, count, _chunks in ranked if query < count]
    for card, count, chunks in ranked:
        order += [(card, query, chunk) for chunk in range(1, chunks) for query in range(count)]
    return order


def pair_plan(case, tok, cfg, *, lane='edit', pair_budget=None, cancelled=None, stats=None):
    """The edit-view pairs of one case, in budget order, tokenized.

    Each item names its ``candidate_id`` and chunk indices. Pairs past
    ``min(pair_budget, P_HARD)`` are not built; ``stats`` (optional, mutated)
    counts chunks, dropped pairs and the tokens left outside the chunk limits.
    """
    from laya.common import QTYPES
    if lane not in A.LANES:
        raise ValueError('invalid_predict_request')
    budget = A.P_HARD if pair_budget is None else min(pair_budget, A.P_HARD)
    question = A.EDIT_QUESTION
    queries, dropped_query = query_chunks(case.get('prompt'), tok, lane)
    ranked = []
    dropped_card = chunked = card_max = 0
    for candidate in case['candidates']:
        if cancelled is not None and cancelled():
            raise ValueError('prediction_cancelled')
        chunks, dropped = card_chunks(candidate['input_text'], tok)
        dropped_card += dropped
        chunked += len(chunks) > 1
        card_max = max(card_max, len(chunks))
        ranked.append((candidate['id'], chunks))
    order = plan_order([(card, len(queries), len(chunks)) for card, chunks in ranked], lane)
    texts = dict(ranked)
    items = []
    for card, query, chunk in order[:budget]:
        if cancelled is not None and cancelled():
            raise ValueError('prediction_cancelled')
        body = texts[card][chunk]
        if tok.mask_token in queries[query] + body:
            raise ValueError('mask_token_in_source')
        try:
            ids, markers = A._sequence(queries[query], body, tok, cfg, question)
            truncated = False
        except ValueError as exc:
            if str(exc) not in A._BUDGET_ERRORS:
                raise
            ids, markers, kept = A._fitting(queries[query], body, tok, cfg, question,
                                            cancelled=cancelled)
            truncated = kept < len(body)
        items.append({'ids': ids, 'markers': markers, 'qtype': QTYPES[question['t']],
                      'truncated': truncated, 'candidate_id': card,
                      'query_chunk': query, 'card_chunk': chunk})
    if stats is not None:
        stats['query_chunks'] = max(stats.get('query_chunks', 0), len(queries))
        stats['card_chunks_max'] = max(stats.get('card_chunks_max', 0), card_max)
        stats['chunked_cards'] = stats.get('chunked_cards', 0) + chunked
        stats['dropped_pairs'] = stats.get('dropped_pairs', 0) + max(0, len(order) - budget)
        stats['dropped_query'] = stats.get('dropped_query', 0) + dropped_query
        stats['dropped_card'] = stats.get('dropped_card', 0) + dropped_card
    return items


def case_pool(case, with_gold):
    """(candidates, gold ids or None) of one case: count, unique ids, gold exposure."""
    if not isinstance(case, dict) or not isinstance(case.get('candidates'), list):
        raise ValueError('invalid_case')
    candidates = case['candidates']
    if not 1 <= len(candidates) <= 30 or len({c.get('id') for c in candidates}) != len(candidates):
        raise ValueError('invalid_candidate_pool')
    selected = None
    if with_gold:
        selected = case.get('selected_ids')
        if not isinstance(selected, list) or not set(selected) <= {c['id'] for c in candidates}:
            raise ValueError('invalid_train_selection')
    elif 'selected_ids' in case:
        raise ValueError('heldout_gold_exposed')
    return candidates, selected


def complete_input(prompt, candidate, tok):
    """The whole-input checks of one edit-view pair, before any chunking."""
    from laya.common import render_options
    body = candidate.get('input_text')
    if (not isinstance(prompt, str) or not prompt.strip() or
            not isinstance(body, str) or not body.strip() or
            len(body.encode('utf8')) > A.MAX_INPUT_BYTES or
            candidate.get('input_kind') not in ('full_compact_wiki_body', 'legacy_complete_excerpt')):
        raise ValueError('invalid_complete_input')
    if tok.mask_token in 'User request:\n' + prompt + '\nWiki card body:\n' + body:
        raise ValueError('mask_token_in_source')
    for option in render_options(A.EDIT_QUESTION):
        if len(tok(' ' + option, add_special_tokens=False)['input_ids']) > 48:
            raise ValueError('option_token_truncation')


def planned_pairs(cases, tok, cfg, *, with_gold, cancelled, lane, pair_budget, stats):
    """Every case's ``pair_plan`` items with request ids (and gold labels)."""
    result = []
    for case in cases:
        if cancelled is not None and cancelled():
            raise ValueError('prediction_cancelled')
        candidates, selected = case_pool(case, with_gold)
        for candidate in candidates:
            complete_input(case.get('prompt'), candidate, tok)
        for item in pair_plan(case, tok, cfg, lane=lane, pair_budget=pair_budget,
                              cancelled=cancelled, stats=stats):
            item['request_id'] = case['request_id']
            if with_gold:
                item['gold_label'] = int(item['candidate_id'] in selected)
            result.append(item)
    return result


def single_positive(items, score):
    """Training pairs under the max rule: a gold card keeps one positive pair.

    ``score(pairs)`` gives each pair's probability under the head training
    starts from. A gold card with several pairs keeps its highest-scoring
    pair (the first on a tie) and drops the rest; other pairs keep their
    label. Order is otherwise unchanged.
    """
    groups = {}
    for index, item in enumerate(items):
        if item.get('gold_label') == 1:
            groups.setdefault((item.get('request_id'), item['candidate_id']), []).append(index)
    drop = set()
    for indices in groups.values():
        if len(indices) < 2:
            continue
        probabilities = score([items[i] for i in indices])
        if len(probabilities) != len(indices):
            raise ValueError('unexpected_laya_logit_shape')
        best = max(range(len(indices)), key=lambda i: (probabilities[i], -i))
        drop.update(index for i, index in enumerate(indices) if i != best)
    return [item for index, item in enumerate(items) if index not in drop]


def head_scorer(torch, model, tok, *, guard=None, limits=None):
    """``score(pairs)`` with the loaded head, in no-grad batches of four."""
    from contextlib import nullcontext

    def score(pairs):
        scored = []
        for group in A.prediction_batches(pairs, max_padded_tokens=8192, max_size=4):
            with guard.step() if guard is not None else nullcontext():
                A._check_mps_budget(torch, limits)
                with torch.no_grad():
                    logits, _ = model(**A._batch(group, tok, next(model.parameters()).device))
                scored += torch.softmax(logits.float(), -1)[:, 1].tolist()
        return scored
    return score


def request_lane(request):
    """(lane, pair budget) of a prediction request; both keys are optional."""
    lane = request.get('query_lane', 'edit')
    budget = request.get('pair_budget')
    if lane not in A.LANES or (budget is not None and (type(budget) is not int
                                                       or not 1 <= budget <= A.P_HARD)):
        raise ValueError('invalid_predict_request')
    return lane, budget


def pair_lengths(items):
    """p50, p95 and max model input length of the planned pairs."""
    ordered = sorted(len(item['ids']) for item in items)
    def at(fraction):
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]
    return {'pair_len_p50': at(.5), 'pair_len_p95': at(.95), 'pair_len_max': ordered[-1]}


def _digest_case(case):
    case['input_sha256'] = hashlib.sha256(json.dumps(case, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return case


def chunking_probe(loaded):
    """A synthetic case whose query and card both split into two or more chunks."""
    tok = loaded[1]

    def sized(unit, tokens):
        per = len(tok(unit * 64, add_special_tokens=False)['input_ids'])
        return (unit * max(1, tokens * 64 // per)).strip()
    prompt = sized('synthetic request marker sentence. ', A.Q_MAX * 2 + A.Q_MAX // 2)
    body = '# Synthetic chunked card\n' + sized('synthetic card body sentence. ',
                                                A.B_MAX * 2 + A.B_MAX // 2)
    return _digest_case({'request_id': 'inference-prepare-chunking', 'prompt': prompt,
                         'candidates': [{'id': 'synthetic/chunked', 'revision_sha256': '1' * 64,
                                         'input_text': body,
                                         'input_kind': 'full_compact_wiki_body'}]})


def truncation_probe(loaded):
    """A synthetic case whose single card must be cut under this model budget.

    Sized from the loaded model's own budget (1024 or 8192 tokens): the
    request fills all but a small room, the card's heading fits that room and
    its body does not. The legacy and experiment views' pairing proof.
    """
    from laya.common import build_sequence
    _, tok, cfg = loaded
    question = (A.EDIT_QUESTION if cfg.get('model_input_view') == A.EDIT_INPUT_VIEW
                else A.QUESTION)
    skeleton, _ = build_sequence(tok, '', question, max_len=cfg['max_len'],
        head_max_len=cfg.get('head_max_len', 192), state_ids=[])
    room = min(cfg['max_len'], tok.model_max_length) - len(skeleton)

    def tokens(text):
        return len(tok(text, add_special_tokens=False)['input_ids'])
    unit = 'synthetic request marker '
    prompt = unit * max(1, (room - 192) * 64 // tokens(unit * 64))
    while tokens(prompt) > room - 128 and len(prompt) > len(unit):
        prompt = prompt[:-len(unit)]
    filler = 'synthetic overflow body '
    body = '# Synthetic overflow\n' + filler * (512 * 64 // tokens(filler * 64) + 1)
    return _digest_case({'request_id': 'inference-prepare-truncation', 'prompt': prompt.strip(),
                         'candidates': [{'id': 'synthetic/overflow', 'revision_sha256': '1' * 64,
                                         'input_text': body,
                                         'input_kind': 'full_compact_wiki_body'}]})


def pairing_probe(loaded):
    """(case, proof) of this model view's versioned prediction pairing.

    ``proof(timing)`` holds when the case's prediction timing shows the
    pairing ran: every planned chunk pair scored (edit view), or the one cut
    card (legacy and experiment views).
    """
    if loaded[2].get('model_input_view') == A.EDIT_INPUT_VIEW:
        def chunked(timing):
            return (timing.get('query_chunks', 0) >= 2 and timing.get('card_chunks_max', 0) >= 2
                    and timing.get('dropped_pairs') == 0
                    and timing.get('pairs') == timing.get('planned_pairs')
                    == timing['query_chunks'] * timing['card_chunks_max'])
        return chunking_probe(loaded), chunked
    return truncation_probe(loaded), lambda timing: timing.get('truncated_pairs') == 1


def smoke_chunk_plan(tokenizer, checkpoint, encoder):
    """The edit-view pair plan on this tokenizer: over-budget input becomes
    chunks, and the length left outside the chunk limits is counted.

    Tokenizer only (no forward). A tokenizer without the edit view's window
    or offsets cannot run the edit view and is not checked here.
    """
    if (not getattr(tokenizer, 'is_fast', False)
            or tokenizer.model_max_length < A.EDIT_INPUT_TOKENS
            or encoder.get('max_position_embeddings', 0) < A.EDIT_INPUT_TOKENS):
        return
    cfg = {**checkpoint, 'max_len': A.EDIT_INPUT_TOKENS, 'model_input_view': A.EDIT_INPUT_VIEW}
    prompt = 'synthetic request sentence. ' * (A.Q_MAX * A.Q_CHUNKS)
    body = '# Synthetic smoke card\n' + 'synthetic smoke body sentence. ' * (A.B_MAX * A.B_CHUNKS)
    stats = {}
    items = pair_plan({'request_id': 'synthetic-chunks', 'prompt': prompt,
                       'candidates': [{'id': 'card-long', 'input_text': body,
                                       'input_kind': 'full_compact_wiki_body'}]},
                      tokenizer, cfg, stats=stats)
    if (stats['query_chunks'] != A.Q_CHUNKS or stats['card_chunks_max'] != A.B_CHUNKS
            or len(items) != A.Q_CHUNKS * A.B_CHUNKS or not stats['dropped_query']
            or not stats['dropped_card'] or any(item['truncated'] for item in items)
            or any(len(item['ids']) > A.EDIT_INPUT_TOKENS for item in items)):
        raise ValueError('silent_model_token_truncation')
