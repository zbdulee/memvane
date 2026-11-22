"""Child process that embeds one query: JSON request on stdin, JSON result on stdout.

A request with ``chunks`` asks a long prompt as several windows (``embed_chunks``).
"""
from __future__ import annotations

import json
import sys
import time

from memvane.kernel.bundle_paths import plugin_root

# The child starts with ``-I`` and puts the plugin root (argv[1]) on sys.path itself.
_BOOTSTRAP = '''
import sys
sys.path.insert(0,sys.argv[1])
from memvane.provider.query_embed_child import main
main()
'''
# A prompt up to WHOLE_CHARS is asked whole. A longer one, or one over
# RETRY_CHARS whose whole request failed (the worker refuses a text past its
# context), is asked as windows of WINDOW_CHARS: the last three and the first.
WHOLE_CHARS = 6000
RETRY_CHARS = 2000
WINDOW_CHARS = 1200
MAX_WINDOWS = 4
# The parent ends the child at the request's ``timeout``; the child stops
# asking this long before, so the vectors it has still reach the parent.
ANSWER_RESERVE_SECONDS = 0.3


def child_argv():
    return [sys.executable, '-B', '-I', '-c', _BOOTSTRAP, str(plugin_root())]


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
    ``left()`` is the seconds this child may still spend. A failed window is
    asked once more as its second half. Windows left when the time ends are
    dropped; the vectors already answered stand.
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
            error = error or str(exc)
            return None
    if len(prompt) <= WHOLE_CHARS:
        if ask(prompt) or len(prompt) <= RETRY_CHARS:
            return vectors, requested, error
    for window in windows(prompt):
        answered = ask(window)
        if answered is False:
            break
        if answered is None and len(window) > 1 and ask(window[len(window) // 2:]) is False:
            break
    return vectors, requested, error


def main():
    from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider
    from memvane.kernel.errors import IndexContractError
    started = time.monotonic()
    request = json.load(sys.stdin)
    # A test runtime root's fixture spec answers instead (``fixture_port.active``).
    from memvane.provider.fixture_port import active
    port = active()

    def answer(code):
        return {'error': code if code in request['allowed'] else 'provider_result_invalid'}
    if not request.get('chunks'):
        if port is not None:
            print(json.dumps(port.query_result(request), allow_nan=False, separators=(',', ':')))
            return
        try:
            value = ReviewedLocalQmdProvider(request['manifest'])._embed('query', request['prompt'],
                                                                         timeout=request['timeout'])
            print(json.dumps({'vector': value}, allow_nan=False, separators=(',', ':')))
        except IndexContractError as exc:
            print(json.dumps(answer(str(exc))))
        return
    try:
        handle = None if port is not None else ReviewedLocalQmdProvider(request['manifest'])
    except IndexContractError as exc:
        print(json.dumps(answer(str(exc))))
        return

    def embed(text, seconds):
        if port is not None:
            result = port.query_result({**request, 'prompt': text})
            if 'vector' not in result:
                raise ValueError(result['error'])
            return result['vector']
        try:
            return handle._embed('query', text, timeout=seconds)
        except IndexContractError as exc:
            raise ValueError(str(exc)) from exc
    vectors, requested, error = embed_chunks(
        request['prompt'], embed,
        lambda: request['timeout'] - ANSWER_RESERVE_SECONDS - (time.monotonic() - started))
    result = ({'vectors': vectors, 'requested': requested} if vectors
              else answer(error or 'provider_unavailable'))
    print(json.dumps(result, allow_nan=False, separators=(',', ':')))
