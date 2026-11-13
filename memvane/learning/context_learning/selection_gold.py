"""Reviewed request-level 0–3 card choices, independent of pair relevance.

Only a trusted local reviewer may attest a choice. A model prediction cannot
silently become the reference choice, and a changed captured input invalidates it.
"""
import json

from .contracts import digest
from .store import canonical, database


def _table(db):
    db.execute('CREATE TABLE IF NOT EXISTS selection_gold (request_id TEXT PRIMARY KEY, body TEXT NOT NULL, review TEXT NOT NULL)')


def save_selection(state_dir, request_id, selected_ids, *, input_sha256, reviewer_id, reference):
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError('invalid_request_id')
    if not isinstance(selected_ids, list) or len(selected_ids) > 3 or any(not isinstance(x, str) or not x for x in selected_ids) or len(set(selected_ids)) != len(selected_ids):
        raise ValueError('invalid_selection')
    if any(not isinstance(x, str) or not x.strip() or len(x) > 512 for x in (reviewer_id, reference)):
        raise ValueError('invalid_review')
    with database(state_dir) as db:
        row = db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()
        if row is None or db.execute('SELECT 1 FROM tombstones WHERE request_id=?', (request_id,)).fetchone():
            raise ValueError('unknown_or_deleted_request')
        sample = json.loads(row[0])
        if input_sha256 != digest(canonical(sample)):
            raise ValueError('selection_input_mismatch')
        if not set(selected_ids) <= {c['candidate_id'] for c in sample['candidates']}:
            raise ValueError('unknown_selection_id')
        _table(db)
        body = canonical({'schema_version': 1, 'request_id': request_id,
                          'input_sha256': input_sha256, 'selected_ids': selected_ids})
        review = canonical({'reviewer_id': reviewer_id, 'reference': reference,
                            'selection_sha256': digest(body)})
        old = db.execute('SELECT body,review FROM selection_gold WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old != (body, review):
                raise ValueError('immutable_selection_conflict')
            return 'reviewed'
        db.execute('INSERT INTO selection_gold VALUES (?,?,?)', (request_id, body, review))
    return 'reviewed'


def load_selection(state_dir, request_id):
    with database(state_dir) as db:
        _table(db)
        row = db.execute('SELECT g.body,g.review,s.body FROM selection_gold g JOIN samples s ON s.request_id=g.request_id WHERE g.request_id=?', (request_id,)).fetchone()
        if row is None:
            raise ValueError('missing_reviewed_selection')
        body, review = json.loads(row[0]), json.loads(row[1])
        if body['input_sha256'] != digest(canonical(json.loads(row[2]))) or review['selection_sha256'] != digest(row[0]):
            raise ValueError('selection_integrity_mismatch')
        return {'selection': body, 'review': review}
