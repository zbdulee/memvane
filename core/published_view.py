"""Request identity and a pure publication decision for a future QMD view.

The CAS decision is exercised by synthetic stores only. It does not publish
the current shared QMD database, which workers still update in place.
"""
from __future__ import annotations

import hashlib
import json


ROUTE_KEYS = ('INDEX_PATH', 'QMD_CONFIG_DIR', 'XDG_CACHE_HOME', 'generation',
              'selected', 'shared')
ENDPOINT_KEYS = ('pid', 'url', 'node', 'entry', 'runtime', 'store')


def request_identity(route: dict, endpoint: dict | None, project_root: str,
                     config: dict) -> tuple:
    """Capture the complete selected route and scope used by one request."""
    if not isinstance(route, dict) or not isinstance(config, dict):
        raise ValueError('published_view_identity_invalid')
    selected = route.get('selected') is True
    if selected and any(not route.get(key) for key in ROUTE_KEYS[:4]):
        raise ValueError('published_view_identity_invalid')
    if route.get('shared') is True:
        if not isinstance(endpoint, dict) or any(not endpoint.get(key) for key in ENDPOINT_KEYS):
            raise ValueError('published_view_identity_invalid')
    elif endpoint is not None:
        raise ValueError('published_view_identity_invalid')
    scope = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False).encode('utf-8')).hexdigest()
    return (project_root, scope, tuple(route.get(key) for key in ROUTE_KEYS),
            tuple(endpoint.get(key) for key in ENDPOINT_KEYS) if endpoint else None)


def publication_decision(current: dict, expected: dict, prepared: dict) -> dict | None:
    """Return a prepared view only when its owner and predecessor still match.

    Callers must serialize the read/decision/write. This function is the
    comparison contract, not a filesystem transaction or a worker fence.
    """
    keys = ('owner', 'incarnation', 'worktree', 'scopeRevision', 'storeGeneration',
            'contentRevision')
    if any(not isinstance(row, dict) or any(not row.get(key) for key in keys)
           for row in (current, expected, prepared)):
        return None
    if current != expected:
        return None
    if any(prepared[key] != expected[key] for key in keys[:-1]):
        return None
    if prepared['contentRevision'] == current['contentRevision']:
        return None
    return prepared.copy()
