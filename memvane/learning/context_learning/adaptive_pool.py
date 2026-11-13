"""Bounded admission for the opt-in shared experimental reranker.

This decides the retrieval bound before QMD is called. It is deliberately
separate from resident microbatch size (1/2/4) and final injected cards (0-3).
The 30-row ceiling is the versioned capture contract's per-phase evidence
bound, not a fixed model candidate count.
"""
from __future__ import annotations

import math
import time

MIN_USEFUL = 5
MAX_EVIDENCE = 30
PAIR_TOKEN_CEILING = 900  # original short prompt path
EDIT_PAIR_TOKEN_CEILING = 7000  # 8192 edit view, with instruction/headroom


def token_upper(text):
    """Conservative byte-based estimate when the tokenizer is not loaded."""
    return max(1, math.ceil(len(text.encode('utf8')) / 2))


def _estimated_rerank_seconds(pair_tokens):
    """Provisional warm-MPS estimate; the resident still measures real time."""
    return .12 + sum(.03 + .00004 * count for count in pair_tokens)


def plan(prompt, deadline, *, probe=None, now=None, edit_view=False,
         rerank_target_seconds=1.0):
    """Return a serializable decision; zero means retain ordinary QMD topN.

    The deadline is only an admission safety bound. CPU and available memory
    determine the normal tier, then estimated work must fit the time left.
    No unavailable candidate is invented to reach five.
    """
    from .shared import resource_report
    now = time.monotonic() if now is None else now
    remaining = max(0.0, deadline - now)
    prompt_tokens = token_upper(prompt)
    ceiling = EDIT_PAIR_TOKEN_CEILING if edit_view else PAIR_TOKEN_CEILING
    if (type(rerank_target_seconds) not in (int, float)
            or not math.isfinite(rerank_target_seconds)
            or not .2 <= rerank_target_seconds <= 10):
        rerank_target_seconds = 1.0
    report = (probe or resource_report)(
        {'maxLoadPerCpu': 1.5, 'minAvailableMib': 2048}, detailed=False)
    available = report.get('available_mib')
    load = report.get('load_per_cpu')
    base = {'requested': 0, 'prompt_tokens_estimate': prompt_tokens,
            'pair_tokens_estimate': prompt_tokens + 400,
            'pair_windows_estimate': math.ceil((prompt_tokens + 400) / 1024),
            'available_mib': available, 'load_per_cpu': load,
            'deadline_remaining_seconds': round(remaining, 3),
            'pair_token_ceiling': ceiling,
            'rerank_target_seconds': rerank_target_seconds}
    if (not report.get('ready') or not isinstance(available, (int, float))
            or not isinstance(load, (int, float))):
        return {**base, 'reason': 'resource_deferred'}
    if prompt_tokens + 400 > ceiling:
        return {**base, 'reason': 'input_window_unavailable'}
    tier = (5 if available < 4096 else 10 if available < 8192 else
            20 if available < 16384 else MAX_EVIDENCE)
    if load > 1.0:
        tier = min(tier, 10)
    if edit_view:
        # A long 1536-token source window can fit the multilingual model but
        # needs fewer candidates to approach the one-second rerank target.
        # These conservative local estimates are admission hints, not a
        # latency guarantee on another machine.
        pair_seconds = .03 + .00004 * (prompt_tokens + 400)
        responsiveness_capacity = max(0, math.floor(
            (rerank_target_seconds - .12) / pair_seconds))
        if responsiveness_capacity < MIN_USEFUL:
            return {**base, 'reason': 'responsiveness_deferred',
                    'estimated_pair_seconds': round(pair_seconds, 4)}
        tier = min(tier, responsiveness_capacity)
    # 0.20 s/pair is intentionally slower than the measured warm Mini MPS
    # run; 2 s allows retrieval/snapshots and 4 s allows a cold resident.
    capacity = math.floor((remaining - 6.0) / 0.20)
    if capacity < MIN_USEFUL:
        return {**base, 'reason': 'deadline_deferred'}
    count = min(tier, MAX_EVIDENCE, capacity)
    return {**base, 'requested': count, 'reason': 'admitted',
            'estimated_rerank_seconds': round(
                _estimated_rerank_seconds([prompt_tokens + 400] * count), 4)
            if edit_view else None}


def actual_work(prompt, bodies, *, edit_view=False,
                rerank_target_seconds=1.0):
    """Estimate serialized model work from the exact captured bodies."""
    tokens = [token_upper(prompt) + token_upper(body) for body in bodies]
    ceiling = EDIT_PAIR_TOKEN_CEILING if edit_view else PAIR_TOKEN_CEILING
    estimate = _estimated_rerank_seconds(tokens) if edit_view else None
    return {'input_tokens_estimate': sum(tokens),
            'input_windows_estimate': sum(math.ceil(n / 1024) for n in tokens),
            'largest_pair_tokens_estimate': max(tokens, default=0),
            'all_pairs_fit_estimate': all(n <= ceiling for n in tokens),
            'estimated_rerank_seconds': round(estimate, 4) if estimate is not None else None,
            'within_rerank_target_estimate': (estimate <= rerank_target_seconds
                if estimate is not None else None)}
