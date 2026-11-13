"""Vector profile identity and exact vector codec."""
import functools
import hashlib
import json
import math
import operator
import re
import struct
import unicodedata
from laya_store_primitives import IndexContractError

_HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_DIMENSION = 2048


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def profile_identity(model_id, embedding_fingerprint, dimension):
    if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 512
            or any(unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
                   for ch in model_id)
            or not _hex(embedding_fingerprint)
            or type(dimension) is not int or not 1 <= dimension <= MAX_DIMENSION):
        raise IndexContractError('invalid_vector_profile')
    return hashlib.sha256(_json([model_id, embedding_fingerprint,
                                 dimension, 'cosine'])).hexdigest()


def _blob(vector, dimension):
    if (not isinstance(vector, (list, tuple)) or len(vector) != dimension
            or any(type(v) not in (int, float) for v in vector)):
        raise IndexContractError('invalid_vector')
    try:
        if not all(math.isfinite(v) for v in vector):
            raise IndexContractError('invalid_vector')
        blob = struct.pack('<' + 'f' * dimension, *vector)
        values = struct.unpack('<' + 'f' * dimension, blob)
    except (OverflowError, struct.error) as exc:
        raise IndexContractError('invalid_vector') from exc
    total = _sum_squares(values)
    if not math.isfinite(total) or total <= 0:
        raise IndexContractError('invalid_vector')
    return blob, values


@functools.lru_cache(maxsize=8)
def _vector_struct(dimension):
    return struct.Struct('<' + 'f' * dimension)


def _sum_squares(values):
    """Exactly rounded sum of squares; inf or nan when any value is not finite.

    ``math.fsum`` is exact and order independent, so summing the same products
    through C-level ``map`` equals the per-element generator form bit for bit.
    A non-finite value makes the sum non-finite (a float32 square cannot
    overflow a double sum), so one pass replaces the separate finiteness scan.
    """
    try:
        return math.fsum(map(operator.mul, values, values))
    except (OverflowError, ValueError):
        return math.inf


def _unpack(blob, dimension):
    if not isinstance(blob, bytes) or len(blob) != dimension * 4:
        raise IndexContractError('vector_blob_invalid')
    values = _vector_struct(dimension).unpack(blob)
    total = _sum_squares(values)
    if not math.isfinite(total) or total <= 0:
        raise IndexContractError('vector_blob_invalid')
    return values


