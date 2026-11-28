"""Query Playground DTO: one ``recall.api.dry_run_recall`` of a proved checkout.

The target id is proved through ``read_adapters.resolve_target`` before the
dry run reads its view. The dry run itself writes no event, wakes nothing and
starts no provider, inference service or trainer; this adapter only checks
the request body and reshapes the result into ``DryRunRecallResponse`` of
``ui/src/api/types.ts`` (``hash`` becomes ``blob``, a selector decision
becomes ``picked`` and ``reason``). Rows carry card paths and blob hashes,
never a card body.
"""
from __future__ import annotations

import math
import time

from memvane.ui.read_adapters import TargetError, resolve_target

MAX_PROMPT_CHARS = 32768
TIMEOUT_MS_DEFAULT = 5000
TIMEOUT_MS_MIN = 200
TIMEOUT_MS_MAX = 15000
BODY_KEYS = frozenset({'prompt', 'useSemantic', 'useSelector', 'timeoutMs'})
# Decisions of a card the dry run would inject: chosen by the selector, or ranked into the answer without it.
PICKED_DECISIONS = frozenset({'selected', 'ranked_top'})


def _flag(body, key, default):
    value = body.get(key, default)
    if type(value) is not bool:
        raise TargetError(key + '_invalid', 400)
    return value


def parse_body(body):
    """``(prompt, use_semantic, use_selector, timeout_ms)`` of a playground request, or ``TargetError`` 400."""
    if not isinstance(body, dict):
        raise TargetError('body_invalid', 400)
    if not set(body) <= BODY_KEYS:
        raise TargetError('body_unknown_field', 400)
    prompt = body.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise TargetError('prompt_invalid', 400)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise TargetError('prompt_too_long', 400)
    use_semantic = _flag(body, 'useSemantic', True)
    use_selector = _flag(body, 'useSelector', False)
    timeout_ms = body.get('timeoutMs', TIMEOUT_MS_DEFAULT)
    if type(timeout_ms) is not int or not TIMEOUT_MS_MIN <= timeout_ms <= TIMEOUT_MS_MAX:
        raise TargetError('timeoutMs_invalid', 400)
    return prompt, use_semantic, use_selector, timeout_ms


def _number(value):
    """A finite JSON number (``bool`` as 0/1), or None."""
    if type(value) is bool:
        return int(value)
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def _count(value):
    return value if type(value) is int else None


def _text(value):
    return value if isinstance(value, str) else None


def _rows(rows, score_key, out_key):
    return [{'path': str(row.get('path') or ''), 'blob': str(row.get('hash') or ''),
             out_key: _number(row.get(score_key)), 'rank': _count(row.get('rank'))}
            for row in rows or () if isinstance(row, dict)]


def _selector(raw):
    raw = raw if isinstance(raw, dict) else {}
    decisions = []
    for row in raw.get('decisions') or ():
        if isinstance(row, dict):
            decision = str(row.get('decision') or '')
            decisions.append({'path': str(row.get('path') or ''),
                              'picked': decision in PICKED_DECISIONS, 'reason': decision})
    return {'ran': raw.get('ran') is True, 'reason': _text(raw.get('reason')),
            'checkpoint': _text(raw.get('checkpoint')),
            'final': [{'path': str(row.get('path') or ''), 'sha': str(row.get('hash') or '')}
                      for row in raw.get('final') or () if isinstance(row, dict)],
            'decisions': decisions}


def playground_response(view):
    """``DryRunRecallResponse`` of a ``DryRunRecallResult.as_dict()`` view."""
    semantic = view.get('semanticList')
    generation = view.get('generation')
    timings = {}
    for key, value in (view.get('timings') or {}).items():
        number = _number(value)
        if number is not None:
            timings[str(key)] = number
    return {
        'route': '' if view.get('route') is None else str(view['route']),
        'linked': view.get('linked') is True,
        'outcome': str(view.get('outcome') or ''),
        'reason': str(view.get('reason') or ''),
        'generation': None if generation is None else str(generation),
        'lexicalLists': [{'query': str(lane.get('query') or ''),
                          'rows': _rows(lane.get('rows'), 'score', 'score')}
                         for lane in view.get('lexicalLists') or () if isinstance(lane, dict)],
        'semanticList': None if semantic is None else _rows(semantic, 'cosine', 'cosine'),
        'fusedList': _rows(view.get('fusedList'), 'rrfScore', 'rrfScore'),
        'selectorResult': _selector(view.get('selectorResult')),
        'semanticStatus': _text(view.get('semanticStatus')),
        'vectorCoveredMembers': _count(view.get('vectorCoveredMembers')),
        'vectorEligibleMembers': _count(view.get('vectorEligibleMembers')),
        'fallbackReason': _text(view.get('fallbackReason')),
        'notes': [str(note) for note in view.get('notes') or ()],
        'timings': timings,
    }


def query_playground(target_id, body, *, home=None):
    """The dry run of ``body['prompt']`` in the proved checkout of ``target_id`` (``DryRunRecallResponse``)."""
    from memvane.recall import api as recall
    prompt, use_semantic, use_selector, timeout_ms = parse_body(body)
    resolved = resolve_target(target_id, home=home)
    result = recall.dry_run_recall(resolved['targetRoot'], prompt,
                                   deadline=time.monotonic() + timeout_ms / 1000.0,
                                   use_semantic=use_semantic, use_selector=use_selector)
    return playground_response(result.as_dict())
