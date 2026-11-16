"""Synthetic native-kernel checks; no models, live stores or wall-clock assertions."""
import importlib.util
import math
import random
import struct
import sys
import time
import unittest
from unittest import mock

from memvane.store.blob.blob_values import BlobStoreError
from memvane.store.view import view_vectors as vectors


class KernelChecks(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('memvane.store.view.vector_accel'),
                             'Accelerate kernel module exists')
        from memvane.store.view import vector_accel
        return vector_accel

    def native(self):
        module = self.module()
        with mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'on'}):
            native = module.kernel()
        if sys.platform != 'darwin':
            self.skipTest('native Accelerate requires macOS')
        self.assertIsNotNone(native, 'real macOS Accelerate loads and passes its self-test')
        return module, native

    def test_kill_switch_and_non_macos_never_load_the_library(self):
        module = self.module()
        with mock.patch.object(module, '_load') as load:
            with mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}):
                self.assertIsNone(module.kernel())
            with mock.patch.object(module.sys, 'platform', 'linux'):
                self.assertIsNone(module.kernel())
            load.assert_not_called()

    def test_library_absence_falls_back(self):
        module = self.module()
        import ctypes
        module._load.cache_clear()
        self.addCleanup(module._load.cache_clear)
        with mock.patch.object(ctypes, 'CDLL', side_effect=OSError('synthetic unavailable')):
            self.assertIsNone(module._load())

    def test_ctypes_import_failure_falls_back(self):
        module = self.module()
        module._load.cache_clear()
        self.addCleanup(module._load.cache_clear)
        with mock.patch.object(module, '_Kernel', side_effect=ImportError('synthetic missing ctypes')):
            self.assertIsNone(module._load())

    def test_native_thousands_match_python_top_and_cross_block_boundary(self):
        module, native = self.native()
        rng = random.Random(1010)
        dimension, count = 16, 4200
        query = vectors.query_values([rng.uniform(-1, 1) for _ in range(dimension)])
        packed = [struct.pack('<16f', *[rng.uniform(-2, 2) for _ in range(dimension)])
                  for _ in range(count)]
        stats = {}
        actual = native.scores(bytearray(b''.join(packed)), dimension, query,
                               time.monotonic() + 20, stats)
        expected = [vectors._set_score(query, [{'embedding': row}], dimension, None)
                    for row in packed]
        self.assertEqual(sorted(range(count), key=lambda i: (-actual[i], i))[:60],
                         sorted(range(count), key=lambda i: (-expected[i], i))[:60])
        self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 1e-10)
        self.assertEqual(stats['vectorScanBlocks'], math.ceil(count / module.BLOCK_ROWS))

    def test_native_extreme_finite_values_and_invalid_rows(self):
        _module, native = self.native()
        for values in ([1e38, -1e38], [1e-44, 1e-44], [3.0, 4.0]):
            packed = struct.pack('<2f', *values)
            expected = vectors._set_score((1., 0.), [{'embedding': packed}], 2, None)
            actual = native.scores(bytearray(packed), 2, (1., 0.), time.monotonic() + 10, {})
            self.assertAlmostEqual(actual[0], expected, places=12)
        for values in ([0., 0.], [float('nan'), 1.], [float('inf'), 1.]):
            with self.assertRaises(BlobStoreError) as caught:
                native.scores(bytearray(struct.pack('<2f', *values)), 2, (1., 0.),
                              time.monotonic() + 10, {})
            self.assertEqual(caught.exception.reason, 'embedding_values_invalid')

    def test_expired_deadline_scans_zero_blocks(self):
        _module, native = self.native()
        stats = {'vectorScanBlocks': 0}
        with self.assertRaises(BlobStoreError) as caught:
            native.scores(bytearray(struct.pack('<2f', 1., 0.)), 2, (1., 0.),
                          time.monotonic() - 1, stats)
        self.assertEqual(caught.exception.reason, 'vector_deadline_exceeded')
        self.assertEqual(stats['vectorScanBlocks'], 0)

    def test_deadline_between_blocks_stops_after_one_block(self):
        module, native = self.native()
        stats = {}
        packed = bytearray(struct.pack('<2f', 1., 0.) * (module.BLOCK_ROWS + 1))
        with mock.patch.object(module, '_left', side_effect=[None, None,
                               BlobStoreError('vector_deadline_exceeded')]):
            with self.assertRaises(BlobStoreError):
                native.scores(packed, 2, (1., 0.), time.monotonic() + 10, stats)
        self.assertEqual(stats['vectorScanBlocks'], 1)
