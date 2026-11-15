"""Read-only route selection for external-edit notifications."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memvane.route.api import classify_v4 as _classify_v4, pairs as _pairs, read_regular as _read_regular
from memvane.route.types import MAX_RECORD_BYTES, QMD_SELECTION_MARKER, RECORD, SCHEMA_V4, SELECTION_SCHEMA_V1
from memvane.settings import api as project_dir


def _decision(state, reason, **details):
    return {"state": state, "reason": reason, **details}


def select_notification_route(project):
    """Classify an external-edit hint without opening the recipient store."""
    from memvane.journal.ledger_io import _read_json
    try:
        root = Path(project)
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ValueError('invalid_project_root')
        marker = _read_json(project_dir.runtime_file(root, QMD_SELECTION_MARKER))
        raw = _read_regular(project_dir.runtime_file(root, RECORD), MAX_RECORD_BYTES)
        route = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
        if (not isinstance(route, dict) or route.get('schema') != SCHEMA_V4
                or not isinstance(marker, dict) or set(marker) !=
                {'schema', 'projectRoot', 'installId', 'routeSha256'}
                or marker['schema'] != SELECTION_SCHEMA_V1
                or marker['projectRoot'] != str(root)
                or marker['installId'] != route.get('installId')
                or marker['routeSha256'] != hashlib.sha256(raw).hexdigest()):
            raise ValueError('selection_marker_invalid')
        return _classify_v4(root, project_dir.settings_path(root), route, inspect_store=False)
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError, KeyError):
        return _decision('blocked', 'selection_marker_invalid')
