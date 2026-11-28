"""One-time review reload of generated cards left without a verify row.

The verify side (``wiki/verify/review_queue.py``) proposes one ``kind: review``
payload per machine-owned, fresh ``generated`` card, with ``expectedTargetHash``
set to the live card bytes. This transaction keeps a payload only when nothing
already settles those bytes:

- an open (``queued``/``claimed``) row of the same target: that row reviews it;
- a terminal (``done``/``dropped``) ``kind: review`` row of the same target whose
  ``expectedTargetHash`` is the same SHA: the bytes were reviewed already. A
  ``replace`` row's ``expectedTargetHash`` names the card it would replace; that
  row reviewed its pending image, never the live bytes, so it does not count.

The cutover of this release writes the marker with its import, so only a project
a released 1.1.4~1.1.6 cutover already migrated reloads. Retention removes a
terminal row after 30 days; a reload running later reviews those bytes once more.

A payload left with ``engine`` None (no producing engine known) is counted
``engineUnknown`` and not queued. The queued payloads and the ``REVIEW_RELOAD_MARKER``
commit together, so a crash reloads nothing or everything once, and a later pass
reloads nothing.
"""
from __future__ import annotations

import json
import time

from memvane.wiki.pipeline.types import REVIEW_RELOAD_MARKER


def _reviewed(cur, target: str, sha: str) -> bool:
    for row in cur.execute("SELECT payload_json FROM verify_jobs WHERE target_path = ? "
                           "AND state IN ('done', 'dropped')", (target,)):
        try:
            payload = json.loads(row[0])
        except ValueError:
            continue
        if (isinstance(payload, dict) and payload.get('kind', 'review') == 'review'
                and payload.get('expectedTargetHash') == sha):
            return True
    return False


def reload_reviews(store, payloads: list[dict], marker: str = REVIEW_RELOAD_MARKER) -> dict:
    """Queue the unsettled ``payloads`` and set ``marker`` in one transaction."""
    counts = {'queued': 0, 'open': 0, 'reviewed': 0, 'engineUnknown': 0}
    now = time.time_ns()
    with store._write_tx() as cur:
        if cur.execute('SELECT 1 FROM pipeline_meta WHERE key = ?', (marker,)).fetchone():
            return {**counts, 'alreadyReloaded': True}
        for index, payload in enumerate(payloads):
            target, sha = payload['targetPath'], payload['expectedTargetHash']
            if cur.execute("SELECT 1 FROM verify_jobs WHERE target_path = ? AND state IN ('queued', 'claimed')",
                           (target,)).fetchone():
                counts['open'] += 1
            elif _reviewed(cur, target, sha):
                counts['reviewed'] += 1
            elif not payload.get('engine'):
                counts['engineUnknown'] += 1
            else:
                store._insert_job(cur, 'verify', payload, now + index)
                counts['queued'] += 1
        cur.execute('INSERT INTO pipeline_meta(key, value) VALUES(?, ?)', (marker, str(time.time())))
    return {**counts, 'alreadyReloaded': False}
