"""Ledger checkpoints of a frozen backfill item: its source bytes, paid answer and sibling writes.

One ``backfill_checkpoints`` row per binding (source job + cohort item). Each stage
change rewrites the row in one transaction, so a crash leaves either the previous
stage or the next one; the paid answer is never stored without its stage.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from memvane.kernel.atomic_rename import canonical_json_bytes

LIMIT = 8 * 1024 * 1024
STAGES = ('claimed', 'siblings')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _store(queue):
    from memvane.wiki.pipeline import api as pipeline
    return pipeline.open_pipeline_dir(queue.parent)


class FrozenWork:
    """The compile run lock serializes this frozen item's source and candidate evidence."""

    @classmethod
    def open(cls, queue, text, job, frozen_item, *, resume_only):
        source_job = {key: value for key, value in job.items() if key not in (
            'partialCompile', '_processedContentRevision', '_fallbackAttempts', '_requeue_count', '_backfillSkipped')}
        binding = {'sourceJob': source_job, 'cohort': list(frozen_item)}
        key = digest(canonical_json_bytes(binding))
        try:
            with _store(queue) as store:
                row = store.backfill_checkpoint(key)
                if row is None and resume_only:
                    return None
                return cls(queue, key, text, binding, row, store)
        except (ValueError, sqlite3.Error) as exc:
            raise OSError('backfill checkpoint unavailable') from exc

    def __init__(self, queue, key, text, binding, row, store):
        self.queue = queue
        self.key = key
        self.binding = binding
        self.source = (text + '\n').encode()
        self.revision = binding['cohort'][2]
        self.image = None
        if row is not None:
            state, raw, self.image = row['state'], row['raw'], row['image']
            if (state.get('version') != 1
                    or state.get('binding') != binding
                    or state.get('rawDigest') != digest(raw)
                    or state.get('rawLength') != len(raw)
                    or state.get('stage') not in STAGES
                    or not isinstance(state.get('results'), dict)
                    or not isinstance(state.get('engine'), str)
                    or (state['stage'] == 'siblings' and self.image is None)):
                raise OSError('unrecognized frozen backfill work')
            self.raw = raw
            self.state = state
        else:
            self.raw = self.source
            self.state = {'version': 1, 'binding': binding, 'stage': 'claimed',
                          'engine': binding['sourceJob'].get('engine'),
                          'rawLength': len(self.source), 'rawDigest': digest(self.source),
                          'results': {}, 'next': 'native_extractor'}
            store.put_backfill_checkpoint(self.key, self.state, self.raw)
        if self.has_answer:
            self.answer()

    def _save(self):
        try:
            with _store(self.queue) as store:
                store.put_backfill_checkpoint(self.key, self.state, self.raw, self.image)
        except (ValueError, sqlite3.Error) as exc:
            raise OSError('backfill checkpoint write failed') from exc

    @property
    def has_answer(self):
        return self.state.get('stage') == 'siblings'

    def prepare(self, extracted, engine):
        if not self.has_answer:
            image = canonical_json_bytes(extracted)
            if len(image) > LIMIT:
                raise OSError('frozen answer exceeds pass budget')
            self.image = image
            self.state.update(stage='siblings', engine=engine, next='native_writer', imageDigest=digest(image))
            self._save()
        return self.answer()

    def answer(self):
        if self.image is None or digest(self.image) != self.state.get('imageDigest'):
            raise OSError('frozen answer checksum mismatch')
        return json.loads(self.image)

    def settled(self):
        # A settled item and a missing checkpoint open alike (resume_only: None; otherwise a
        # fresh claim), so the row goes at once.
        try:
            with _store(self.queue) as store:
                store.delete_backfill_checkpoint(self.key)
        except (ValueError, sqlite3.Error) as exc:
            raise OSError('backfill checkpoint delete failed') from exc

    def write_sibling(self, key, writer):
        if key in self.state['results']:
            return self.state['results'][key]
        result = writer()
        if isinstance(result, dict) and result.get('action') in ('created', 'updated', 'candidate',
                                                                 'merge-needed'):
            self.state['results'][key] = result
            self._save()
        return result
