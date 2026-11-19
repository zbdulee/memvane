"""Opt-in Laya selector throughput measurement on synthetic pairs (M-2).

Run with the Laya runtime interpreter under the development guard's model-test
kind, with the repository root on ``PYTHONPATH`` and ``QMD_LAYA_MODEL``::

    npm run dev:guard -- --kind model-test -- env PYTHONPATH=. QMD_LAYA_MODEL=<model> \\
      <laya python> -s -B memvane/learning/tests/model/measure_selector_throughput.py

Synthetic text only (Korean-heavy and English/code-heavy). Prints one JSON
report: per (pair tokens, batch size, text kind) the first-shape and warm
forward milliseconds, pairs per second, RSS and MPS driver memory; the cost of
new padded shapes against 128-token buckets; padded totals with and without a
length sort; and the longest single batch (the cancellation latency bound).
"""
import json
import os
import random
import statistics
import sys
import time

from memvane.learning.context_learning import adapter

def _ints(name, default):
    value = os.environ.get(name)
    return tuple(int(x) for x in value.split(',')) if value else default


# A guard stop keeps every line printed before it; the order below measures
# shapes first, then the grid cells named by these variables.
LENGTHS = _ints('QMD_MEASURE_LENGTHS', (256, 512, 768, 1024, 1350, 2048, 4096))
BATCHES = _ints('QMD_MEASURE_BATCHES', (1, 2, 4, 8, 16))
KINDS = tuple(os.environ.get('QMD_MEASURE_KINDS', 'ko,en').split(','))
MAX_TOKENS_IN_FLIGHT = int(os.environ.get('QMD_MEASURE_MAX_TOKENS', '16384'))
WARM_REPEATS = 3
UNITS = {
    'ko': ('%d번 합성 측정: 배터리 전압이 모터 기동 전류로 잠깐 떨어진다. ',
           '%d번 합성 카드: 위키 카드는 편집 중인 문서와 관련된 결정을 적는다. '),
    'en': ('def synthetic_%d(value):\n    return value * %d  # route the enable pin\n',
           'Step %d: keep grounds common and log the synthetic marker %d. '),
}


def _text(unit, count, seed):
    return ''.join(unit % ((i + seed),) if unit.count('%d') == 1 else unit % (i + seed, i)
                   for i in range(count))


def _pair(tok, cfg, kind, target, seed):
    """Synthetic (ids, markers) whose full model input is close to ``target`` tokens."""
    prompt_unit, body_unit = UNITS[kind]
    prompt = _text(prompt_unit, 3, seed)
    low, high = 1, 4096
    best = None
    while low <= high:
        middle = (low + high) // 2
        body = '# Synthetic card %d\n' % seed + _text(body_unit, middle, seed)
        try:
            ids, markers = adapter._sequence(prompt, body, tok, cfg, adapter.EDIT_QUESTION)
        except ValueError:
            high = middle - 1
            continue
        if len(ids) <= target:
            best = (ids, markers)
            low = middle + 1
        else:
            high = middle - 1
    return {'ids': best[0], 'markers': best[1], 'qtype': 0}


def _forward(torch, model, tok, items):
    device = next(model.parameters()).device
    batch = adapter._batch(items, tok, device)
    started = time.perf_counter()
    with torch.no_grad():
        logits, _ = model(**batch)
        probabilities = torch.softmax(logits.float(), -1)[:, 1].tolist()
    elapsed = time.perf_counter() - started
    if len(probabilities) != len(items):
        raise ValueError('unexpected_laya_logit_shape')
    return elapsed


def _memory(torch):
    rss, driver = adapter._allocation(torch)
    return round(rss, 1), round(driver, 1)


def main():
    model_dir = os.environ['QMD_LAYA_MODEL']
    model, tok, cfg = adapter._load(model_dir, input_view=adapter.EDIT_INPUT_VIEW)
    torch = adapter._torch()
    from laya.common import QTYPES
    qtype = QTYPES[adapter.EDIT_QUESTION['t']]
    # Cost of a new padded shape: batches of four pairs cut to widths never
    # used before (700..1400 tokens, distinct); the first call against an
    # immediate repeat of the same batch. A pair is cut inside its state
    # tokens; the question and marker positions lead it.
    def width(item, size):
        size = min(size, len(item['ids']))
        return {'ids': item['ids'][:size - 1] + item['ids'][-1:], 'markers': item['markers'],
                'qtype': qtype}
    rng = random.Random(19)
    base = [_pair(tok, cfg, 'ko', 1400, seed) for seed in range(4)]
    overheads, warm_batches = [], []
    for size in rng.sample(range(701, 1400), 12):
        items = [width(item, size) for item in base]
        first = _forward(torch, model, tok, items)
        again = _forward(torch, model, tok, items)
        overheads.append(first - again)
        warm_batches.append(again)
    print(json.dumps({'new_shape_overhead_ms': {
        'median': round(statistics.median(overheads) * 1000, 1),
        'max': round(max(overheads) * 1000, 1),
        'warm_batch4_median': round(statistics.median(warm_batches) * 1000, 1)}}), flush=True)
    rows = []
    longest_batch = {}
    peak = [0.0, 0.0]
    for kind in KINDS:
        for target in LENGTHS:
            pool = []
            for seed in range(max(BATCHES)):
                item = _pair(tok, cfg, kind, target, seed)
                item['qtype'] = qtype
                pool.append(item)
            for size in BATCHES:
                if target * size > MAX_TOKENS_IN_FLIGHT:
                    continue
                items = pool[:size]
                first = _forward(torch, model, tok, items)
                warm = [_forward(torch, model, tok, items) for _ in range(WARM_REPEATS)]
                rss, driver = _memory(torch)
                peak = [max(peak[0], rss), max(peak[1], driver)]
                warm_ms = statistics.median(warm) * 1000
                row = ({'kind': kind, 'pair_tokens': max(len(i['ids']) for i in items),
                             'target': target, 'batch': size,
                             'first_ms': round(first * 1000, 1),
                             'warm_ms': round(warm_ms, 1),
                             'warm_max_ms': round(max(warm) * 1000, 1),
                             'ms_per_pair': round(warm_ms / size, 1),
                             'pairs_per_second': round(size / (warm_ms / 1000), 2),
                             'rss_mib': rss, 'mps_driver_mib': driver})
                rows.append(row)
                # One line per cell, so a guard stop keeps the cells measured so far.
                print(json.dumps({'row': row}), flush=True)
                key = (target, size)
                longest_batch[key] = max(longest_batch.get(key, 0.0), max([first, *warm]))
    # Padded totals for a realistic plan: 8 cards x (1 query chunk) x up to 3
    # card chunks with mixed lengths, batch 8, sorted against unsorted.
    lengths = [rng.choice((300, 450, 700, 900, 1100, 1300)) for _ in range(48)]
    def padded(values, size):
        return sum(max(values[i:i + size]) * len(values[i:i + size])
                   for i in range(0, len(values), size))
    report = {
        'device': str(next(model.parameters()).device),
        'max_len': cfg['max_len'],
        'rows': rows,
        'padded_unsorted': padded(lengths, 8), 'padded_sorted': padded(sorted(lengths), 8),
        'longest_batch_ms': {'%d x %d' % key: round(value * 1000, 1)
                             for key, value in sorted(longest_batch.items())},
        'peak_rss_mib': peak[0], 'peak_mps_driver_mib': peak[1],
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    sys.exit(main())
