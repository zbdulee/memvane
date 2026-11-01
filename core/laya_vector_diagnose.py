"""Name why the reviewed provider refused one member's embedding.

``ReviewedLocalQmdProvider`` raises ``provider_result_invalid`` without the
answer it rejected. ``diagnose`` repeats that one embedding in a separate,
short-lived ``python3 -B`` process (``main``). Only that process observes the
JSON the provider module decodes, so no other provider caller in any process
ever sees an instrumented module. The child builds the provider from the same
reviewed manifest (its identity and artifact checks run unchanged), uses the
caller's request timeout, and prints one cause code. The repeated result is
never stored.

``result_rule_code`` re-applies the provider's own ``document_chunks`` answer
rules, in its order, to name the first one the answer breaks.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys

UNKNOWN = 'unknown'
MAX_CHUNKS = 1024
IDENTITY = ('approvedProviderId', 'providerArtifactSha256', 'embeddingFingerprint')
CHILD_MARGIN_SECONDS = 30


def result_rule_code(envelope, manifest):
    """First provider rule a decoded ``document_chunks`` answer breaks.

    Mirrors ``ReviewedLocalQmdProvider._embed`` and ``laya_index_vector._blob``;
    ``unknown`` when the answer passes every rule.
    """
    import struct
    from laya_index_vector import _sum_squares
    dimension = manifest['dimension']
    if not isinstance(envelope, dict):
        return 'envelope_invalid'
    if (all(isinstance(envelope.get(key), str) for key in IDENTITY)
            and any(envelope[key] != manifest[key] for key in IDENTITY)):
        return 'identity_mismatch'
    if envelope.get('ok') is False:
        return 'worker_refused' if 'refused' in envelope else 'worker_failure'
    if (any(envelope.get(key) != manifest[key] for key in IDENTITY)
            or envelope.get('ok') is not True
            or set(envelope) != {*IDENTITY, 'ok', 'result'}):
        return 'envelope_invalid'
    response = envelope['result']
    if (not isinstance(response, dict) or set(response) != {'chunks'}
            or not isinstance(response['chunks'], list)):
        return 'result_invalid'
    if not 1 <= len(response['chunks']) <= MAX_CHUNKS:
        return 'chunk_count_invalid'
    last_pos = -1
    for seq, chunk in enumerate(response['chunks']):
        if not isinstance(chunk, dict) or set(chunk) != {'seq', 'pos', 'vector'}:
            return 'chunk_shape_invalid'
        if type(chunk['seq']) is not int or chunk['seq'] != seq:
            return 'chunk_seq_invalid'
        if type(chunk['pos']) is not int:
            return 'chunk_shape_invalid'
        if chunk['pos'] <= last_pos:
            return 'chunk_pos_not_increasing'
        vector = chunk['vector']
        if not isinstance(vector, list) or len(vector) != dimension:
            return 'dimension_mismatch'
        if any(type(value) not in (int, float) for value in vector):
            return 'vector_value_invalid'
        if not all(math.isfinite(value) for value in vector):
            return 'non_finite_vector'
        try:
            values = struct.unpack('<' + 'f' * dimension,
                                   struct.pack('<' + 'f' * dimension, *vector))
        except (OverflowError, struct.error):
            return 'vector_out_of_float32_range'
        total = _sum_squares(values)
        if not math.isfinite(total):
            return 'vector_norm_overflow'
        if total <= 0:
            return 'zero_norm_vector'
        last_pos = chunk['pos']
    return UNKNOWN


def diagnose(manifest, body, path, *, timeout, python=None, runner=subprocess.run):
    """Cause code for one refused member, from a separate process; never raises."""
    try:
        request = json.dumps({'manifest': manifest, 'body': body, 'path': path,
                              'timeout': timeout}, ensure_ascii=False).encode('utf-8')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        done = runner([python or sys.executable, '-B', str(Path(__file__).resolve())],
                      input=request, capture_output=True, env=env,
                      timeout=timeout + CHILD_MARGIN_SECONDS)
        if done.returncode != 0:
            return UNKNOWN
        code = json.loads(done.stdout.decode('utf-8')).get('cause')
    except Exception:
        return UNKNOWN
    if (not isinstance(code, str) or not code or len(code) > 64
            or any(not (ch.isascii() and (ch.isalnum() or ch in '_.:-')) for ch in code)):
        return UNKNOWN
    return code


def _observe_and_embed(provider_module, provider, body, path, timeout, manifest):
    """In the child only: one embedding while recording what ``json.loads`` decodes."""
    from laya_index_a import IndexContractError
    real = provider_module.json
    seen = []

    class Observed:
        def __getattr__(self, name):
            return getattr(real, name)

        def loads(self, value, *args, **kwargs):
            seen.append(('raw', value))
            decoded = real.loads(value, *args, **kwargs)
            seen.append(('decoded', decoded))
            return decoded
    provider_module.json = Observed()
    try:
        provider.embed_document_chunks(body, path, timeout=timeout)
    except IndexContractError as exc:
        code = str(exc)
    else:
        return 'not_reproduced'
    finally:
        provider_module.json = real
    if code != 'provider_result_invalid':
        return code or UNKNOWN
    decoded = [value for kind, value in seen if kind == 'decoded']
    if decoded:
        return result_rule_code(decoded[-1], manifest)
    raw = [value for kind, value in seen if kind == 'raw']
    return 'empty_answer' if not raw or not str(raw[-1]).strip() else 'unreadable_answer'


def main():
    sys.dont_write_bytecode = True
    try:
        request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
        import laya_local_embed_provider as provider_module
        provider = provider_module.ReviewedLocalQmdProvider(request['manifest'])
        cause = _observe_and_embed(provider_module, provider, request['body'],
                                   request['path'], request['timeout'], request['manifest'])
    except Exception:
        cause = UNKNOWN
    sys.stdout.write(json.dumps({'cause': cause}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
