"""The query vectors of one recall, asked of the warm embedding service from the hook process.

A prompt up to ``WHOLE_CHARS`` is asked whole. A longer one, or one over
``RETRY_CHARS`` whose whole request failed (the worker refuses a text past
its context), is asked as windows of ``WINDOW_CHARS``: the last three and the
first (``embed_chunks``).

Each request is the reviewed provider's own socket round trip (the attested
adapter: its owner and replacement checks and its envelope validation). That
round trip bounds each socket operation; the bound on the whole request is
kept here: the call runs on a daemon thread that is joined until the recall's
deadline. A request still running then is given up as ``provider_unavailable``;
its thread ends at the adapter's own socket timeout or with the process.

No child process is started. A test runtime root's fixture spec answers
instead of the service (``fixture_port.active``).
"""
from __future__ import annotations

import threading
import time

from memvane.provider.types import QUERY_IDENTITY_ERRORS

WHOLE_CHARS = 6000
RETRY_CHARS = 2000
WINDOW_CHARS = 1200
MAX_WINDOWS = 4
# The adapter's bound on one query request.
REQUEST_SECONDS = 2.0


def windows(prompt):
    """Up to ``MAX_WINDOWS`` windows of ``prompt``: from its end backwards, then its first."""
    chosen, end = [], len(prompt)
    while end > 0 and len(chosen) < MAX_WINDOWS - 1:
        chosen.append(prompt[max(0, end - WINDOW_CHARS):end])
        end = max(0, end - WINDOW_CHARS)
    if end > 0:
        chosen.append(prompt[:min(WINDOW_CHARS, end)])
    return chosen


def embed_chunks(prompt, embed, left):
    """``(vectors, requested, first error)`` of one prompt.

    ``embed(text, seconds)`` returns a vector or raises ``ValueError(code)``;
    ``left()`` is the seconds the recall may still spend here. A failed window is
    asked once more as its second half. Windows left when the time ends are
    dropped. An identity change discards every vector returned during the request.
    """
    vectors, requested, error = [], 0, None

    def ask(text):
        nonlocal requested, error
        seconds = left()
        if seconds <= 0:
            return False
        requested += 1
        try:
            vectors.append(embed(text, seconds))
            return True
        except ValueError as exc:
            if str(exc) in QUERY_IDENTITY_ERRORS:
                # An observed identity change invalidates every window of this request.
                vectors.clear()
                error = str(exc)
                return False
            error = error or str(exc)
            return None
    if len(prompt) <= WHOLE_CHARS:
        if ask(prompt) is not None or len(prompt) <= RETRY_CHARS:
            return vectors, requested, error
    for window in windows(prompt):
        answered = ask(window)
        if answered is False:
            break
        if answered is None and len(window) > 1 and ask(window[len(window) // 2:]) is False:
            break
    return vectors, requested, error


def _bounded(call, seconds):
    """``call()`` finished within ``seconds``, else ``IndexContractError('provider_unavailable')``."""
    from memvane.kernel.errors import IndexContractError
    box = {}

    def run():
        try:
            box['value'] = call()
        except BaseException as exc:  # noqa: BLE001 - carried to the caller below
            box['error'] = exc
    thread = threading.Thread(target=run, name='memvane-query-embed', daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise IndexContractError('provider_unavailable')
    if 'error' in box:
        raise box['error']
    return box['value']


def query_vectors(provider, prompt, budget, *, chunks, allowed):
    """``(vectors, requested)`` of ``prompt`` from ``provider`` within ``budget`` seconds.

    ``chunks`` asks a long prompt as windows; without it the prompt is asked
    once, whole. Raises ``IndexContractError``: the provider's refusal when it
    is one of ``allowed``, ``provider_result_invalid`` for any other, and
    ``provider_unavailable`` when the time ended before any vector.
    """
    from memvane.kernel.errors import IndexContractError
    from memvane.provider.fixture_port import active
    deadline, port = time.monotonic() + budget, active()

    def left():
        return min(REQUEST_SECONDS, deadline - time.monotonic())

    def embed(text, seconds):
        if port is not None:
            result = port.query_result({'manifest': provider.manifest, 'prompt': text, 'timeout': seconds,
                                        'allowed': sorted(allowed)})
            if 'vector' not in result:
                raise ValueError(result['error'])
            return result['vector']
        try:
            return _bounded(lambda: provider._embed('query', text, timeout=seconds), seconds)
        except IndexContractError as exc:
            raise ValueError(str(exc)) from exc
    if chunks:
        vectors, requested, error = embed_chunks(prompt, embed, left)
    else:
        vectors, requested, error, seconds = [], 1, None, left()
        try:
            if seconds > 0:
                vectors.append(embed(prompt, seconds))
        except ValueError as exc:
            error = str(exc)
    if not vectors:
        code = error or 'provider_unavailable'
        raise IndexContractError(code if code in allowed else 'provider_result_invalid')
    return vectors, requested
