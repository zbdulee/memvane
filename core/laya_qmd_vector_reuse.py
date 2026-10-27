"""Reuse an origin QMD build's vectors for identical laya-db chunks.

Migration-only: a QMD-origin initial build embeds the same documents with the
same GGUF and the same ported chunk recipe, so a document whose content hash,
chunk count and positions already exist in the origin QMD DB has equivalent
vectors (measured cosine >= 0.999 on a real project). Sampled documents are
re-embedded and compared; any disagreement turns reuse off for the run, and
every other failure falls back to embedding. Removed with the QMD origin path.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from pathlib import Path

MIN_COSINE = 0.999
SAMPLE_EVERY = 25


class QmdVectorReuse:
    def __init__(self, source_db, extension, model_path, dimension, *, sample_every=SAMPLE_EVERY):
        self.source_db = Path(source_db)
        self.extension = Path(extension) if extension is not None else None
        self.model_name = Path(model_path).name
        self.dimension = dimension
        self.sample_every = sample_every
        self.enabled = True
        self.disabled_reason = None
        self._db = None
        self._lookups = 0

    def _disable(self, reason):
        self.enabled = False
        self.disabled_reason = reason
        return None

    def _connect(self):
        if self._db is not None:
            return self._db
        db = None
        try:
            if not self.source_db.is_file():
                raise OSError('missing_source_db')
            db = sqlite3.connect(f'file:{self.source_db}?mode=ro', uri=True)
            if self.extension is not None:
                # vectors_vec is a sqlite-vec virtual table in the origin store.
                db.enable_load_extension(True)
                try:
                    db.load_extension(str(self.extension))
                finally:
                    db.enable_load_extension(False)
        except (OSError, sqlite3.Error, AttributeError):
            if db is not None:
                db.close()
            return self._disable('qmd_vector_reuse_unavailable')
        self._db = db
        return db

    def lookup(self, body):
        """Origin rows {'seq','pos','vector'} when the whole chunk set matches, else None."""
        if not self.enabled:
            return None
        db = self._connect()
        if db is None:
            return None
        sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
        try:
            rows = db.execute('SELECT seq,pos,model,total_chunks FROM content_vectors '
                              'WHERE hash=? ORDER BY seq', (sha,)).fetchall()
            if (not rows or any(seq != i or total != len(rows) or Path(model).name != self.model_name
                                for i, (seq, _, model, total) in enumerate(rows))):
                return None
            out, last = [], -1
            for seq, pos, _, _ in rows:
                found = db.execute('SELECT embedding FROM vectors_vec WHERE hash_seq=?',
                                   (f'{sha}_{seq}',)).fetchone()
                if (found is None or not isinstance(found[0], bytes)
                        or len(found[0]) != 4 * self.dimension or type(pos) is not int or pos <= last):
                    return None
                vector = list(struct.unpack(f'<{self.dimension}f', found[0]))
                if not all(math.isfinite(x) for x in vector):
                    return None
                out.append({'seq': seq, 'pos': pos, 'vector': vector})
                last = pos
        except sqlite3.Error:
            return self._disable('qmd_vector_reuse_unavailable')
        return out

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def take_sample(self):
        """The first reuse and every Nth after it is re-embedded for parity."""
        sample = self._lookups % self.sample_every == 0
        self._lookups += 1
        return sample

    def defer_sample(self):
        """The sample could not run (no model admission); the next reuse takes it."""
        self._lookups = max(0, self._lookups - 1)

    def verify(self, reused, fresh):
        """Same chunk layout and cosine >= MIN_COSINE per chunk, else reuse stops."""
        ok = (isinstance(fresh, list) and len(fresh) == len(reused) and all(
            a['seq'] == b['seq'] and a['pos'] == b['pos'] and _cosine(a['vector'], b['vector']) >= MIN_COSINE
            for a, b in zip(reused, fresh)))
        if not ok:
            self._disable('qmd_vector_reuse_mismatch')
        return ok


def _cosine(a, b):
    a, b = [float(x) for x in a], [float(x) for x in b]
    if len(a) != len(b):
        return -1.0
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return -1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def for_initial_build(identity, manifest, dimension):
    """Reuse source for a QMD-origin build; None for fresh builds or no origin."""
    if identity.get('originMode') == 'fresh_wiki_only' or not identity.get('sourceDb'):
        return None
    module = Path(manifest['moduleEntryPath'])
    node_modules = next((p for p in module.parents if p.name == 'node_modules'), None)
    if node_modules is None:
        return None
    extension = node_modules / 'sqlite-vec-darwin-arm64' / 'vec0'
    return QmdVectorReuse(identity['sourceDb'], extension, manifest['modelPath'], dimension)
