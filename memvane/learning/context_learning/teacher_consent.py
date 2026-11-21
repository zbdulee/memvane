"""Default-teacher consent v2: the fixed proposal and its file check.

The file is ``project-teacher-consent.json`` in the namespace root (0600), as
``{acceptedSha256, proposal}``. The v1 check (``general_teacher.require_consent``)
refuses this shape by its key set, so an older bundle never reads it as an
explicit consent. Payload size and egress checks belong to the teacher.
"""
from __future__ import annotations

import errno
import hashlib
from pathlib import Path

from memvane.kernel.resource_deferred import ResourceDeferred

from .dataset import read_private_json
from .store import canonical

NAME = 'project-teacher-consent.json'
SCHEMA = 'memvane-teacher-consent-v2'
GRANTED_BY = ('default-enrollment', 'default-enrollment-upgrade')
RECIPIENTS = [{'host': 'claude', 'transport': 'claude-cli'},
              {'host': 'codex', 'transport': 'codex-cli'}]
FIELDS = ['edit_text', 'candidate_body', 'candidate_id', 'revision_sha256']
KEYS = frozenset(('schema', 'projectRoot', 'purpose', 'recipients', 'modelPolicy', 'passes',
                  'fields', 'maxInputBytes', 'maxOutputBytes', 'timeoutSeconds', 'maxRssMib',
                  'grantedBy', 'grantedAtUnixMs', 'enabled'))
BUDGETS = (('maxInputBytes', 1024, 65536), ('maxOutputBytes', 1024, 65536),
           ('timeoutSeconds', 1, 60), ('maxRssMib', 512, 4096))
# A read that could not finish says nothing about the consent (deferred, retried).
UNREADABLE = 'teacher_consent_unreadable'
# Errors that describe the file itself: gone, a link, or not the owner's to read.
FILE_ERRNOS = frozenset((errno.ENOENT, errno.ELOOP, errno.EACCES, errno.EPERM,
                         errno.ENOTDIR, errno.EISDIR))


def sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def proposal(project, *, granted_by, granted_at_unix_ms):
    """The fixed template; only the project, origin and grant time vary."""
    if granted_by not in GRANTED_BY or type(granted_at_unix_ms) is not int or granted_at_unix_ms <= 0:
        raise ValueError('teacher_consent_scope')
    return {'schema': SCHEMA, 'projectRoot': str(Path(project)),
            'purpose': 'selection-gold',
            'recipients': [dict(row) for row in RECIPIENTS],
            'modelPolicy': 'lowest-tier', 'passes': 1, 'fields': list(FIELDS),
            'maxInputBytes': 65536, 'maxOutputBytes': 65536,
            'timeoutSeconds': 60, 'maxRssMib': 4096,
            'grantedBy': granted_by, 'grantedAtUnixMs': granted_at_unix_ms, 'enabled': True}


def envelope(value):
    return {'acceptedSha256': sha256(value), 'proposal': value}


def require_consent_v2(root, project):
    """Return the accepted v2 proposal or raise ``teacher_consent_*``.

    An I/O error outside ``FILE_ERRNOS`` raises ``ResourceDeferred(UNREADABLE)``:
    the caller keeps its state (a kept answer included) and reads again later.
    """
    root, project = Path(root), Path(project).resolve(strict=True)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('teacher_consent_scope')
    path = root / NAME
    if not path.exists() and not path.is_symlink():
        raise ValueError('teacher_consent_required')
    try:
        try:
            value = read_private_json(path)
        except ValueError as exc:
            raise ValueError('teacher_consent_scope') from exc
        row = value['proposal']
        if (not isinstance(value, dict) or set(value) != {'acceptedSha256', 'proposal'}
                or not isinstance(row, dict) or set(row) != KEYS
                or value['acceptedSha256'] != sha256(row)
                or row['schema'] != SCHEMA or row['projectRoot'] != str(project)
                or row['purpose'] != 'selection-gold' or row['recipients'] != RECIPIENTS
                or row['modelPolicy'] != 'lowest-tier'
                or type(row['passes']) is not int or row['passes'] != 1
                or row['fields'] != FIELDS or row['grantedBy'] not in GRANTED_BY
                or type(row['grantedAtUnixMs']) is not int or row['grantedAtUnixMs'] <= 0
                or row['enabled'] is not True):
            raise ValueError('teacher_consent_scope')
        for key, low, high in BUDGETS:
            if type(row[key]) is not int or not low <= row[key] <= high:
                raise ValueError('teacher_consent_budget')
        return row
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise ValueError('teacher_consent_required') from exc
        if exc.errno in FILE_ERRNOS:
            raise ValueError('teacher_consent_scope') from exc
        raise ResourceDeferred(UNREADABLE) from exc
    except (KeyError, TypeError) as exc:
        raise ValueError('teacher_consent_scope') from exc
