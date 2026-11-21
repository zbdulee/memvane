"""Bundled teacher model tier table and model resolution.

A Python module on purpose: the code identity hashes (``inference_identity``,
``project_scope._code_snapshot``) cover ``*.py`` only, so a table change is a
code change. A model outside this table is never requested (decision 4).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import time

SCHEMA = 'memvane-teacher-model-tiers-v1'
RELEASE = '1.0.24'
TIER = 1
TIER1 = {
    'claude': {'request': 'haiku',                # value passed to --model
               'ids': ('claude-haiku-5-5',)},     # exact ids the CLI may resolve to
    'codex': {'slugs': ('gpt-6-luna', 'gpt-5.6-luna'),  # preference order
              'effort': 'low'},
}
CATALOG_NAME = 'models_cache.json'
MAX_CATALOG_BYTES = 4 * 1024 * 1024
UNRESOLVED = 'teacher_model_unresolved'


def claude_request():
    return TIER1['claude']['request']


def claude_model_ok(model) -> bool:
    """An exact table id, optionally with a ``-YYYYMMDD`` date suffix only."""
    if not isinstance(model, str) or len(model) > 80:
        return False
    for known in TIER1['claude']['ids']:
        if model == known or re.fullmatch(re.escape(known) + r'-[0-9]{8}', model):
            return True
    return False


def codex_home(environ) -> Path:
    """``$CODEX_HOME`` or ``$HOME/.codex``; never a per-profile copy."""
    raw = environ.get('CODEX_HOME')
    if isinstance(raw, str) and raw:
        return Path(raw)
    home = environ.get('HOME')
    if not isinstance(home, str) or not home:
        raise ValueError(UNRESOLVED)
    return Path(home) / '.codex'


def _read_catalog(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > MAX_CATALOG_BYTES):
            raise ValueError(UNRESOLVED)
        data = b''
        while len(data) <= MAX_CATALOG_BYTES:
            block = os.read(fd, MAX_CATALOG_BYTES + 1 - len(data))
            if not block:
                break
            data += block
        if len(data) > MAX_CATALOG_BYTES:
            raise ValueError(UNRESOLVED)
        return json.loads(data)
    finally:
        os.close(fd)


def _age_days(fetched_at, now):
    if not isinstance(fetched_at, str) or len(fetched_at) > 64:
        return None
    try:
        moment = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (now - moment.timestamp()) / 86400
    return max(0, int(age))


def resolve_codex(environ, now=None) -> dict:
    """The first table slug the user's catalog lists with ``low`` effort.

    Raises ValueError('teacher_model_unresolved') for a missing, unsafe or
    malformed catalog, or an empty intersection with the table.
    """
    try:
        value = _read_catalog(codex_home(environ) / CATALOG_NAME)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(UNRESOLVED) from exc
    if not isinstance(value, dict) or not isinstance(value.get('models'), list):
        raise ValueError(UNRESOLVED)
    effort = TIER1['codex']['effort']
    offered = set()
    for model in value['models']:
        if not isinstance(model, dict) or model.get('visibility') != 'list':
            continue
        levels = model.get('supported_reasoning_levels')
        if not isinstance(levels, list):
            continue
        if any(isinstance(level, dict) and level.get('effort') == effort for level in levels):
            if isinstance(model.get('slug'), str):
                offered.add(model['slug'])
    for slug in TIER1['codex']['slugs']:
        if slug in offered:
            return {'host': 'codex', 'model': slug, 'tier': TIER, 'effort': effort,
                    'catalogAgeDays': _age_days(value.get('fetched_at'),
                                                time.time() if now is None else now)}
    raise ValueError(UNRESOLVED)
