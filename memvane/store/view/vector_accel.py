"""Optional Accelerate cosine kernel. No provider, cache or persistent state.

The native ABI comes from the macOS SDK vDSP.h: strides are signed long,
lengths unsigned long. Little-endian float32 store rows are widened for the
arithmetic so finite float32 extremes and subnormals retain valid norms.
Only the store's revalidated candidates supply final result scores.
"""
from __future__ import annotations

from functools import lru_cache
import math
import os
import sys
import time

from memvane.store.blob import blob_values as v
from memvane.store.types import DEADLINE_EXCEEDED

BLOCK_ROWS = 4096


def _left(deadline):
    if time.monotonic() >= deadline:
        v.fail(DEADLINE_EXCEEDED)


class _Kernel:
    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        self.library = ctypes.CDLL('/System/Library/Frameworks/Accelerate.framework/Accelerate')
        fp, dp = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_double)
        stride, length = ctypes.c_long, ctypes.c_ulong
        self.widen = self.library.vDSP_vspdp
        self.widen.argtypes = [fp, stride, dp, stride, length]
        self.widen.restype = None
        self.square = self.library.vDSP_vsqD
        self.square.argtypes = [dp, stride, dp, stride, length]
        self.square.restype = None
        self.multiply = self.library.vDSP_mmulD
        self.multiply.argtypes = [dp, stride, dp, stride, dp, stride, length, length, length]
        self.multiply.restype = None

    def scores(self, packed, dimension, query, deadline, timings):
        """Cosines in packed-row order, checking the deadline between blocks."""
        _left(deadline)
        c = self.ctypes
        width = dimension * 4
        if not dimension or len(packed) % width or len(query) != dimension:
            v.fail('embedding_set_invalid')
        count = len(packed) // width
        timings.setdefault('vectorScanBlocks', 0)
        if not count:
            return []
        # A single double scratch block; squaring reuses it after the dot.
        size = min(count, BLOCK_ROWS)
        matrix = (c.c_double * (size * dimension))()
        dots, norms = (c.c_double * size)(), (c.c_double * size)()
        ones = (c.c_double * dimension)(*([1.] * dimension))
        q = (c.c_double * dimension)(*query)
        answer = []
        for start in range(0, count, BLOCK_ROWS):
            _left(deadline)
            rows = min(BLOCK_ROWS, count - start)
            source = (c.c_float * (rows * dimension)).from_buffer(packed, start * width)
            self.widen(source, 1, matrix, 1, rows * dimension)
            self.multiply(matrix, 1, q, 1, dots, 1, rows, 1, dimension)
            self.square(matrix, 1, matrix, 1, rows * dimension)
            self.multiply(matrix, 1, ones, 1, norms, 1, rows, 1, dimension)
            timings['vectorScanBlocks'] += 1
            for i in range(rows):
                norm, dot = norms[i], dots[i]
                if norm <= 0 or not math.isfinite(norm) or not math.isfinite(dot):
                    v.fail('embedding_values_invalid')
                answer.append(max(-1., min(1., dot / math.sqrt(norm))))
        _left(deadline)
        return answer


@lru_cache(maxsize=1)
def _load():
    """One bounded native self-test per process; unavailable means Python."""
    try:
        import struct
        native = _Kernel()
        scores = native.scores(bytearray(struct.pack('<4f', 3., 4., 0., -2.)),
                               2, (1., 0.), time.monotonic() + 1., {})
        if len(scores) != 2 or abs(scores[0] - .6) > 1e-12 or scores[1] != 0.:
            return None
        return native
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        return None


def requested():
    return sys.platform == 'darwin' and os.environ.get('MEMVANE_VECTOR_ACCEL', '').lower() != 'off'


def kernel():
    """Lazy opt-out and platform gate, before importing ctypes or dlopen."""
    if not requested():
        return None
    return _load()
