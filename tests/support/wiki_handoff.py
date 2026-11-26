"""Cross-domain fixtures for a compiler driving the verifier's durable work.

Use the verifier's own owner, quota and snapshot operations so assertions see
the same identities and bytes as a verifier stopped during an actual claim.
"""
from memvane.wiki.verify.tests import support as verifier

waiting_reviews = verifier.waiting_reviews
deferred_replacements = verifier.deferred_replacements
retained_batches = verifier.retained_batches
full_copy_quota = verifier.full_copy_quota


def held_replacement(queue, sha):
    matching = [work for work in waiting_reviews(queue)
                if work.state['binding']['job'].get('pendingPath') and
                any(row['sha256'] == sha for row in work.state['binding']['job']['sourceRevisions'])]
    work, = matching
    return work


def during_native_review(queue, bound, work, callback):
    return verifier.during_native_review(queue, bound, work, callback)


def work_rows(queue, kind, *, closed=False):
    """(state, raw bytes or None, stage id) of each stored work, oldest first, read as stored."""
    import json
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    path = queue.parent / DB_NAME
    if not path.exists():
        return []
    with sqlite3.connect(str(path)) as db:
        rows = db.execute('SELECT control_json, raw_blob, stage_id FROM staged_cards WHERE queue = ? AND kind = ? '
                          'AND closed = ? ORDER BY created_ns, stage_id',
                          (queue.name, kind, 1 if closed else 0)).fetchall()
    return [(json.loads(control), None if raw is None else bytes(raw), stage) for control, raw, stage in rows]


def corrupt_work(queue, stage_id, control=b'unreadable reservation'):
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        db.execute('UPDATE staged_cards SET control_json = ? WHERE stage_id = ?', (control.decode(), stage_id))


def work_control(queue, stage_id):
    import sqlite3
    from memvane.wiki.pipeline.types import DB_NAME
    with sqlite3.connect(str(queue.parent / DB_NAME)) as db:
        row = db.execute('SELECT control_json FROM staged_cards WHERE stage_id = ?', (stage_id,)).fetchone()
    return None if row is None else row[0].encode()
