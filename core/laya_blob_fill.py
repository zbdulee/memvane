"""Inactive candidate content fill over immutable complete chunk/vector sets.

ContentFiller coordinates per-content callbacks through a private cooperative
lease (``laya_blob_lease``) and publishes only whole embedding sets through the
existing BlobStore validation. Recipe/profile IDs commit caller declarations,
not callback semantics. Callbacks run outside SQLite transactions and are not
preempted by elapsed checks; the caller bounds them. There is no retry loop and
no crash-wide exactly-once inference claim.
"""
from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
import math
import sqlite3
import struct
import threading
import time

import laya_blob_lease as lease
import laya_blob_values as v
from laya_blob_store import BlobStore
from laya_blob_values import BlobStoreError

MAX_DEADLINE = 300

_Record = namedtuple('_Record', 'claim store path identity incarnation key lease')


class _Claim:
    __slots__ = ()

    def __repr__(self):
        return '<content claim>'


def _key(blob_sha, recipe_id, profile_id):
    return tuple(v.identifier(value) for value in (blob_sha, recipe_id, profile_id))


def _deadline(seconds):
    if type(seconds) not in (int, float) or not 0 < seconds <= MAX_DEADLINE or not math.isfinite(seconds):
        v.fail('content_deadline_invalid')
    return time.monotonic() + seconds


def _expire(deadline):
    if time.monotonic() >= deadline:
        v.fail('content_fill_deadline')


def _result(status, key, digest, count):
    return {'status': status, 'blob_sha': key[0], 'recipe_id': key[1], 'profile_id': key[2],
            'set_digest': digest, 'chunk_count': count}


def _pack(output, inputs, dimension):
    """Exact N x dimension finite nonzero float32 rows bound to the ordered input strings."""
    if not isinstance(output, (list, tuple)) or len(output) != len(inputs):
        v.fail('embedding_count_mismatch')
    if len(output) * dimension * 4 > v.MAX_SET_BYTES:
        v.fail('embedding_set_too_large')
    rows = []
    for seq, (vector, text) in enumerate(zip(output, inputs)):
        if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
            v.fail('embedding_dimension_mismatch')
        values = []
        for number in vector:
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                v.fail('embedding_value_invalid')
            try:
                number = float(number)
                packed = struct.pack('<f', number) if math.isfinite(number) else None
            except (OverflowError, struct.error):
                packed = None
            if packed is None:
                v.fail('embedding_value_invalid')
            values.append(number)
        raw = struct.pack('<' + 'f' * dimension, *values)
        unpacked = struct.unpack('<' + 'f' * dimension, raw)
        if not all(math.isfinite(x) for x in unpacked) or math.fsum(x * x for x in unpacked) <= 0:
            v.fail('embedding_value_invalid')
        try:
            input_sha = v.hash_bytes(text.encode('utf8'))
        except UnicodeError:
            v.fail('embedding_inputs_invalid')
        rows.append(dict(seq=seq, input_sha=input_sha, embedding=raw))
    return rows


class ContentFiller:
    def __init__(self, store):
        if not isinstance(store, BlobStore):
            v.fail('content_store_invalid')
        self._store = store
        self._claims = {}
        self._guard = threading.Lock()

    def _idle(self):
        try:
            busy = self._store._db.in_transaction
        except sqlite3.Error:
            v.fail('candidate_unavailable')
        if busy:
            v.fail('content_fill_in_transaction')

    @contextmanager
    def _read(self):
        self._idle()
        try:
            with self._store._session() as db:
                yield db
        except BlobStoreError:
            raise
        except (sqlite3.Error, OSError):
            v.fail('candidate_unavailable')

    def _call(self, fn, *args):
        self._idle()
        try:
            return fn(*args)
        except BlobStoreError:
            raise
        except (sqlite3.Error, OSError):
            v.fail('candidate_unavailable')

    @contextmanager
    def claim_content(self, blob_sha, recipe_id, profile_id):
        key, store = _key(blob_sha, recipe_id, profile_id), self._store
        with self._read():
            path, identity, incarnation = store._path, store._identity, store._incarnation
        self._idle()
        held = lease.acquire(path, v.digest('memvane-content-lock-v1', [incarnation, *key]))
        claim = _Claim()
        try:
            with self._guard:
                self._claims[id(claim)] = _Record(claim, store, path, identity, incarnation, key, held)
            yield claim
        finally:
            with self._guard:
                self._claims.pop(id(claim), None)
            lease.release(held)

    def validate_claim(self, claim):
        self._validate(claim, None)

    def _validate(self, claim, key):
        with self._guard:
            record = self._claims.get(id(claim))
        if record is None or record.claim is not claim:
            v.fail('content_claim_invalid')
        store = self._store
        if (record.store is not store or (key is not None and record.key != key)
                or (store._path, store._identity, store._incarnation)
                != (record.path, record.identity, record.incarnation)):
            v.fail('content_claim_foreign')
        lease.check(record.lease)
        with self._read():  # rechecks candidate file identity, header and incarnation
            pass

    def _cached(self, key):
        """Complete valid set → result; absent embedding set → None; any other damage refuses."""
        store = self._store
        with self._read() as db:
            store._blob(key[0]); store._recipe(key[1]); store._profile(key[2])
            head = db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                              key).fetchone()
            if head is None:
                return None
            rows = store._vectors(*key)
            return _result('cache_hit', key, v.embedding_digest(*key, rows), len(rows))

    def _prepare(self, key):
        store = self._store
        with self._read() as db:
            store._blob(key[0])
            recipe, profile = store._recipe(key[1]), store._profile(key[2])
            head = db.execute('SELECT 1 FROM chunk_sets WHERE blob_sha=? AND recipe_id=?', key[:2]).fetchone()
            if head is not None:
                store._chunks(key[0], key[1])
            return recipe, profile, head is not None

    def ensure_content(self, blob_sha, recipe_id, profile_id, *, chunker, embedder, write_fence,
                       deadline_seconds=30):
        deadline = _deadline(deadline_seconds)
        if not all(callable(fn) for fn in (chunker, embedder, write_fence)):
            v.fail('content_callback_invalid')
        key = _key(blob_sha, recipe_id, profile_id)
        hit = self._cached(key)
        _expire(deadline)
        if hit is not None:
            return hit
        with self.claim_content(*key) as claim:
            hit = self._cached(key)
            _expire(deadline)
            if hit is not None:
                return hit
            self._validate(claim, key)
            self._idle()
            write_fence()
            recipe, profile, chunked = self._prepare(key)
            if not chunked:
                document = self._call(self._store.document, key[0])
                _expire(deadline)
                rows = chunker(document, recipe)
                _expire(deadline)
                self._validate(claim, key)
                self._call(self._store.put_chunk_set, key[0], key[1], rows)
            inputs = self._call(self._store.embedding_inputs, *key)
            if (not isinstance(inputs, list) or not 1 <= len(inputs) <= v.MAX_CHUNKS
                    or any(type(text) is not str for text in inputs)):
                v.fail('embedding_inputs_invalid')
            dimension = profile['dimension']
            _expire(deadline)
            output = embedder(list(inputs), profile)
            rows = _pack(output, inputs, dimension)
            self._validate(claim, key)
            _expire(deadline)
            digest = self._call(self._store.put_embedding_set, *key, rows)
        return _result('filled', key, digest, len(rows))
