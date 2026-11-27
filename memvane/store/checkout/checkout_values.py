"""Pure checkout values: limits, schemas, binding/capture shapes and index framing."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import time

from memvane.store.types import CheckoutUnavailable

CAPTURE_SECONDS = 3.0
MAX_SECONDS = 10.0
MAX_GIT_BYTES = 32768
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_MARKER_BYTES = 65536
CAPTURE_ATTEMPTS = 2
REPOSITORY_SCHEMA = 'memvane-checkout-repository-v1'
WORKTREE_SCHEMA = 'memvane-checkout-worktree-v1'
CAPTURE_SCHEMA = 'memvane-checkout-capture-v1'
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MARKERS = ('rebase-merge', 'rebase-apply', 'sequencer', 'MERGE_HEAD',
           'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'index.lock', 'HEAD.lock')
REPOSITORY_FIELDS = {'schema', 'repoId', 'kind', 'commonGitDir', 'projectRoot',
                     'projectRelative', 'gitObjectFormat'}
WORKTREE_FIELDS = {'schema', 'worktreeId', 'repoId', 'repositoryDigest',
                   'project', 'checkout', 'gitDir'}
CAPTURE_FIELDS = {'schema', 'projectRoot', 'repoId', 'worktreeId', 'viewId',
                  'branchKey', 'headOid', 'gitObjectFormat', 'evidence', 'checkoutDigest'}


def _fail(reason):
    raise CheckoutUnavailable(reason)


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise CheckoutUnavailable('invalid_checkout_value') from exc
    return hashlib.sha256(raw).hexdigest()


def _deadline(value):
    now = time.monotonic()
    if value is None:
        return now + CAPTURE_SECONDS
    if type(value) not in (int, float):
        _fail('invalid_checkout_deadline')
    try:
        value = float(value)
    except OverflowError as exc:
        raise CheckoutUnavailable('invalid_checkout_deadline') from exc
    if not math.isfinite(value):
        _fail('invalid_checkout_deadline')
    if value <= now:
        _fail('checkout_budget_exhausted')
    return min(value, now + MAX_SECONDS)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail('checkout_budget_exhausted')
    return remaining


def _env():
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0',
               GIT_TERMINAL_PROMPT='0', GIT_LITERAL_PATHSPECS='1',
               GIT_NO_LAZY_FETCH='1', LC_ALL='C')
    return env


def _identity(value):
    return (isinstance(value, dict) and len(value) == 3 and set(value) == {'path', 'dev', 'ino'}
            and isinstance(value['path'], str) and len(value['path']) <= 4096
            and Path(value['path']).is_absolute()
            and not any(ch in value['path'] for ch in '\x00\n\r')
            and type(value['dev']) is int and value['dev'] >= 0
            and type(value['ino']) is int and value['ino'] > 0)


def _repository_fields(facts):
    git = facts['gitDir'] is not None
    return {'kind': 'git' if git else 'non-git',
            'commonGitDir': facts['commonGitDir'],
            'projectRoot': None if git else facts['project'],
            'projectRelative': facts['projectRelative'],
            'gitObjectFormat': facts['gitObjectFormat']}


def _persistent_directory_equal(stored, live):
    # Stored record against a live directory: path and inode (kernel/persistent_identity.py).
    from memvane.kernel.persistent_identity import persistent_identity_equal
    return stored is None and live is None or persistent_identity_equal(stored, live)


def _repository(value, facts):
    if (not isinstance(value, dict) or len(value) != len(REPOSITORY_FIELDS)
            or set(value) != REPOSITORY_FIELDS
            or value['schema'] != REPOSITORY_SCHEMA or not isinstance(value['repoId'], str)
            or not ID.fullmatch(value['repoId'])
            or not isinstance(value['projectRelative'], str)
            or (value['kind'] == 'git' and (not _identity(value['commonGitDir'])
                or value['projectRoot'] is not None or value['gitObjectFormat'] not in ('sha1', 'sha256')))
            or (value['kind'] == 'non-git' and (not _identity(value['projectRoot'])
                or value['commonGitDir'] is not None or value['gitObjectFormat'] is not None))
            or value['kind'] not in ('git', 'non-git')):
        _fail('invalid_repository_binding')
    if any(not _persistent_directory_equal(value[key], live)
           if key in ('commonGitDir', 'projectRoot') else value[key] != live
           for key, live in _repository_fields(facts).items()):
        _fail('repository_binding_changed')


def _worktree(value, repository, facts):
    if (not isinstance(value, dict) or len(value) != len(WORKTREE_FIELDS)
            or set(value) != WORKTREE_FIELDS
            or value['schema'] != WORKTREE_SCHEMA or not isinstance(value['worktreeId'], str)
            or not ID.fullmatch(value['worktreeId']) or not isinstance(value['repoId'], str)
            or not isinstance(value['repositoryDigest'], str)
            or not SHA.fullmatch(value['repositoryDigest'])
            or not _identity(value['project']) or not _identity(value['checkout'])
            or (value['gitDir'] is not None and not _identity(value['gitDir']))):
        _fail('invalid_worktree_binding')
    if (value['repoId'] != repository['repoId'] or value['repositoryDigest'] != _digest(repository)
            or any(not _persistent_directory_equal(value[key], facts[key])
                   for key in ('project', 'checkout', 'gitDir'))):
        _fail('worktree_binding_changed')


def _metadata_shape(value, *, directory=False):
    fields = {'path', 'dev', 'ino', 'mtimeNs', 'ctimeNs'}
    if not directory:
        fields |= {'size', 'sha256'}
    return (isinstance(value, dict) and len(value) == len(fields) and set(value) == fields
            and _identity({key: value[key] for key in ('path', 'dev', 'ino')})
            and all(type(value[key]) is int for key in ('mtimeNs', 'ctimeNs'))
            and (directory or (type(value['size']) is int and 0 <= value['size'] <= MAX_INDEX_BYTES
                 and isinstance(value['sha256'], str) and SHA.fullmatch(value['sha256']))))


def _capture_shape(value):
    if (not isinstance(value, dict) or len(value) != len(CAPTURE_FIELDS)
            or set(value) != CAPTURE_FIELDS or value['schema'] != CAPTURE_SCHEMA
            or not all(isinstance(value[key], str) and ID.fullmatch(value[key])
                       for key in ('repoId', 'worktreeId'))
            or not all(isinstance(value[key], str) and SHA.fullmatch(value[key])
                       for key in ('viewId', 'checkoutDigest'))):
        return False
    facts = value['evidence']
    fields = {'project', 'checkout', 'gitDir', 'commonGitDir', 'projectRelative',
              'gitObjectFormat', 'branchKey', 'headOid', 'index', 'operations'}
    if (not isinstance(facts, dict) or len(facts) != len(fields) or set(facts) != fields
            or not all(_identity(facts[key]) for key in ('project', 'checkout'))
            or not all(facts[key] is None or _identity(facts[key])
                       for key in ('gitDir', 'commonGitDir'))
            or not isinstance(facts['projectRelative'], str)
            or not 0 < len(facts['projectRelative']) <= 4096
            or facts['gitObjectFormat'] not in (None, 'sha1', 'sha256')
            or not isinstance(facts['branchKey'], str)
            or not 0 < len(facts['branchKey']) <= MAX_GIT_BYTES
            or (facts['headOid'] is not None and (not isinstance(facts['headOid'], str)
                or not re.fullmatch('[0-9a-f]{' + ('64' if facts['gitObjectFormat'] == 'sha256'
                                                 else '40') + '}', facts['headOid'])))
            or (facts['index'] is not None and not _metadata_shape(facts['index']))
            or not isinstance(facts['operations'], list)
            or len(facts['operations']) > len(MARKERS) + 1):
        return False
    for operation in facts['operations']:
        if (not isinstance(operation, dict) or len(operation) > 8
                or operation.get('kind') not in ('file', 'directory')
                or not _metadata_shape({key: item for key, item in operation.items() if key != 'kind'},
                                       directory=operation['kind'] == 'directory')):
            return False
    return (value['projectRoot'] == facts['project']['path']
            and all(value[key] == facts[key] for key in ('branchKey', 'headOid', 'gitObjectFormat'))
            and value['viewId'] == _digest([value['repoId'], value['worktreeId'], facts['branchKey']]))


def frame_index(data, object_format, deadline):
    oid_bytes = 20 if object_format == 'sha1' else 32
    end = len(data) - oid_bytes
    if end < 12 or data[:4] != b'DIRC':
        _fail('invalid_git_index')
    version, count = struct.unpack_from('>II', data, 4)
    fixed = 40 + oid_bytes + 2
    if version not in (2, 3, 4) or count > (end - 12) // (fixed + 1):
        _fail('invalid_git_index')
    offset, empty_names, previous_length = 12, False, 0
    for _ in range(count):
        _remaining(deadline)
        start = offset
        if offset + fixed > end:
            _fail('invalid_git_index')
        flags = int.from_bytes(data[offset + fixed - 2:offset + fixed], 'big')
        offset += fixed
        if flags & 0x4000:
            if version == 2 or offset + 2 > end:
                _fail('invalid_git_index')
            extended = int.from_bytes(data[offset:offset + 2], 'big')
            if extended & ~0x6000:
                _fail('invalid_git_index')
            offset += 2
        if version == 4:
            # Decode only the removal count; no path bytes need to expand.
            remove = 0
            for number in range(10):
                if offset >= end:
                    _fail('invalid_git_index')
                octet = data[offset]
                offset += 1
                remove = (octet & 0x7F if number == 0
                          else ((remove + 1) << 7) + (octet & 0x7F))
                if not octet & 0x80:
                    break
            else:
                _fail('invalid_git_index')
            if remove > previous_length:
                _fail('invalid_git_index')
        name_end = data.find(b'\0', offset, end)
        if name_end < 0:
            _fail('invalid_git_index')
        length = name_end - offset
        if version == 4:
            length += previous_length - remove
            previous_length = length
        if length > MAX_INDEX_BYTES or flags & 0xFFF != min(length, 0xFFF):
            _fail('invalid_git_index')
        empty_names = empty_names or length == 0
        offset = name_end + 1
        if version != 4:
            offset = start + ((offset - start + 7) // 8) * 8
            if offset > end or any(data[name_end:offset]):
                _fail('invalid_git_index')
    while offset < end:
        _remaining(deadline)
        if offset + 8 > end:
            _fail('invalid_git_index')
        signature = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], 'big')
        offset += 8
        if offset + size > end:
            _fail('invalid_git_index')
        if signature == b'link':
            _fail('split_index_unsupported')
        if not (65 <= signature[0] <= 90) and signature != b'sdir':
            _fail('unsupported_index_extension')
        offset += size
    # Git's index.skipHash can deliberately write an all-zero checksum. The
    # capture's own SHA-256 still binds every byte of that index image.
    checksum = data[end:]
    if empty_names or (any(checksum) and
                       hashlib.new(object_format, memoryview(data)[:end]).digest() != checksum):
        _fail('invalid_git_index')
    _remaining(deadline)
