"""Value contracts for the inactive Git-blob candidate format."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

SHA = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{32}\Z')
MAX_BODY = 4 * 1024 * 1024


class BlobStoreError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def fail(reason):
    raise BlobStoreError(reason)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail('invalid_canonical_value')


def digest(domain, value):
    try:
        return hashlib.sha256(domain.encode('ascii') + b'\0' +
                              canonical(value).encode('utf8')).hexdigest()
    except UnicodeError:
        fail('invalid_canonical_value')


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def identifier(value, pattern=SHA):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail('invalid_identifier')
    return value


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(c in value for c in '\x00\r\n\\') or value.startswith('/')
            or any(not p or p.startswith('.') for p in value.split('/'))):
        fail('invalid_relative_path')
    return value


def repository(value):
    from laya_checkout_view import REPOSITORY_FIELDS, REPOSITORY_SCHEMA, _identity
    if (not isinstance(value, dict) or set(value) != REPOSITORY_FIELDS
            or value.get('schema') != REPOSITORY_SCHEMA):
        fail('invalid_repository_binding')
    identifier(value.get('repoId'), UUID)
    kind = value.get('kind')
    if kind == 'git':
        valid = (_identity(value.get('commonGitDir')) and value.get('projectRoot') is None
                 and value.get('gitObjectFormat') in ('sha1', 'sha256'))
    elif kind == 'non-git':
        valid = (_identity(value.get('projectRoot')) and value.get('commonGitDir') is None
                 and value.get('gitObjectFormat') is None)
    else:
        valid = False
    project_rel = value.get('projectRelative')
    if not valid or not isinstance(project_rel, str):
        fail('invalid_repository_binding')
    if project_rel != '.':
        relative(project_rel)
    return json.loads(canonical(value))


def parse_canonical(text):
    try:
        value = json.loads(text)
        if canonical(value) != text:
            fail('noncanonical_json')
        return value
    except (ValueError, TypeError, RecursionError):
        fail('invalid_canonical_json')
