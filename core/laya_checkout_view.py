"""Caller-owned checkout registrations and bounded read-only Git captures.

These values identify observations, not journal or data-access authority.
Source bytes require a separate scan manifest. No live route imports this API.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import struct
import subprocess
import time
import uuid

from onboarding_choice import canonical

CAPTURE_SECONDS = 3.0
MAX_SECONDS = 10.0
GIT_SECONDS = 1.0
MAX_GIT_BYTES = 32768
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_MARKER_BYTES = 65536
CAPTURE_ATTEMPTS = 2
MIN_GIT_VERSION = (2, 39)
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


class CheckoutUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


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


def _git(args, root, deadline, *, ok=(0,)):
    end = min(deadline, time.monotonic() + GIT_SECONDS)
    _remaining(end)
    process = None
    selector = selectors.DefaultSelector()
    buffers = {'out': bytearray(), 'err': bytearray()}
    try:
        process = subprocess.Popen(
            ['git', '-c', 'log.showSignature=false', '-c', 'core.fsmonitor=false',
             '-c', 'core.hooksPath=' + os.devnull, *args], cwd=str(root), env=_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for stream, label in ((process.stdout, 'out'), (process.stderr, 'err')):
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            for key, _ in selector.select(min(_remaining(end), .05)):
                block = os.read(key.fileobj.fileno(), MAX_GIT_BYTES + 1)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(block)
                if sum(map(len, buffers.values())) > MAX_GIT_BYTES:
                    _fail('git_output_limit')
        try:
            code = process.wait(timeout=_remaining(end))
        except subprocess.TimeoutExpired as exc:
            raise CheckoutUnavailable('git_timeout') from exc
        if code not in ok:
            _fail('git_exit_' + str(code))
        try:
            return code, buffers['out'].decode('utf-8').rstrip('\n')
        except UnicodeError as exc:
            raise CheckoutUnavailable('git_output_encoding') from exc
    except OSError as exc:
        raise CheckoutUnavailable('git_unavailable') from exc
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _directory(path):
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or any(ch in raw for ch in '\x00\n\r'):
            _fail('invalid_checkout_path')
        resolved = canonical(raw)
        fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            observed, current = os.fstat(fd), resolved.lstat()
            if (not stat.S_ISDIR(observed.st_mode) or
                    (observed.st_dev, observed.st_ino) != (current.st_dev, current.st_ino)):
                _fail('checkout_changed')
            return {'path': str(resolved), 'dev': observed.st_dev, 'ino': observed.st_ino}
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        if isinstance(exc, CheckoutUnavailable):
            raise
        raise CheckoutUnavailable('checkout_directory_unavailable') from exc


def _dotgit(root):
    for directory in (root, *root.parents):
        path = directory / '.git'
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CheckoutUnavailable('git_metadata_unavailable') from exc
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            _fail('unsafe_git_metadata')
        return path
    return None


def _file(path, limit, deadline, *, collect=False):
    _remaining(deadline)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckoutUnavailable('git_metadata_unavailable') from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            _fail('git_metadata_limit')
        digest, count, parts = hashlib.sha256(), 0, []
        while True:
            _remaining(deadline)
            block = os.read(fd, min(65536, limit + 1 - count))
            if not block:
                break
            count += len(block)
            if count > limit:
                _fail('git_metadata_limit')
            digest.update(block)
            if collect:
                parts.append(block)
        after, current = os.fstat(fd), path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            _fail('checkout_changed')
        row = {'path': str(path), 'dev': after.st_dev, 'ino': after.st_ino,
               'size': count, 'mtimeNs': after.st_mtime_ns,
               'ctimeNs': after.st_ctime_ns, 'sha256': digest.hexdigest()}
        return (row, b''.join(parts)) if collect else row
    except OSError as exc:
        raise CheckoutUnavailable('git_metadata_unavailable') from exc
    finally:
        os.close(fd)


def _index(path, object_format, deadline):
    """Frame the bounded primary index without Git loading/freshening it.

    Index v2/v3/v4 framing follows git-scm.com/docs/index-format. No shared
    index is opened: a ``link`` extension refuses before any Git index reader.
    This validates framing/checksum, not the semantics of optional caches.
    """
    observed = _file(path, MAX_INDEX_BYTES, deadline, collect=True)
    if observed is None:
        return None
    row, data = observed
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
    return row


def _ref(root, object_format, deadline):
    status, branch = _git(['symbolic-ref', '--quiet', 'HEAD'], root, deadline, ok=(0, 1))
    if status == 0:
        if not branch.startswith('refs/heads/') or '\n' in branch:
            _fail('invalid_checkout_ref')
        _git(['check-ref-format', branch], root, deadline)
    else:
        branch = None
    status, head = _git(['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'],
                        root, deadline, ok=(0, 1))
    if status == 1:
        if branch is None:
            _fail('invalid_checkout_head')
        status, _ = _git(['show-ref', '--verify', '--quiet', branch], root, deadline, ok=(0, 1))
        if status != 1:
            _fail('invalid_checkout_head')
        return branch, None
    length = 40 if object_format == 'sha1' else 64
    if not re.fullmatch('[0-9a-f]{' + str(length) + '}', head):
        _fail('invalid_checkout_head')
    return branch or 'detached:' + head, head


def _operations(admin, common, deadline):
    paths = [admin / marker for marker in MARKERS]
    paths.append(common / 'packed-refs.lock')
    rows = []
    for path in paths:
        _remaining(deadline)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CheckoutUnavailable('git_metadata_unavailable') from exc
        if stat.S_ISDIR(info.st_mode):
            rows.append({'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino,
                         'mtimeNs': info.st_mtime_ns, 'ctimeNs': info.st_ctime_ns,
                         'kind': 'directory'})
        elif stat.S_ISREG(info.st_mode):
            row = _file(path, MAX_MARKER_BYTES, deadline)
            if row is None:
                _fail('checkout_changed')
            rows.append({**row, 'kind': 'file'})
        else:
            _fail('unsafe_git_metadata')
    return rows


def _facts(project_root, deadline):
    _remaining(deadline)
    project = _directory(project_root)
    root = Path(project['path'])
    marker = _dotgit(root)
    if marker is None:
        if (root / 'HEAD').exists() and (root / 'objects').is_dir():
            _fail('bare_repository_unsupported')
        return {'project': project, 'checkout': project, 'gitDir': None,
                'commonGitDir': None, 'projectRelative': '.', 'gitObjectFormat': None,
                'branchKey': 'non-git', 'headOid': None, 'index': None, 'operations': []}
    # This floor supports GIT_NO_LAZY_FETCH. Unknown/older Git must stop
    # before repository/object queries, never silently ignore that fence.
    _, version = _git(['--version'], root, deadline)
    parsed = re.match(r'^git version ([0-9]{1,3})\.([0-9]{1,3})(?:\.|\s|$)', version)
    if parsed is None or tuple(map(int, parsed.groups())) < MIN_GIT_VERSION:
        _fail('git_version_unsupported')
    _, raw = _git(['rev-parse', '--path-format=absolute', '--show-toplevel', '--git-dir',
                   '--git-common-dir', '--show-object-format=storage',
                   '--is-bare-repository', '--is-inside-work-tree'], root, deadline)
    fields = raw.split('\n')
    if len(fields) != 6 or fields[3] not in ('sha1', 'sha256') or fields[4:] != ['false', 'true']:
        _fail('invalid_git_checkout')
    checkout, admin, common = map(_directory, fields[:3])
    top, git_dir, common_dir = (Path(item['path']) for item in (checkout, admin, common))
    if marker != top / '.git':
        _fail('invalid_git_checkout')
    try:
        relative = root.relative_to(top).as_posix()
    except ValueError as exc:
        raise CheckoutUnavailable('invalid_git_checkout') from exc
    index = _index(git_dir / 'index', fields[3], deadline)
    branch, head = _ref(root, fields[3], deadline)
    operations = _operations(git_dir, common_dir, deadline)
    if _ref(root, fields[3], deadline) != (branch, head):
        _fail('checkout_changed')
    for item in (project, checkout, admin, common):
        if _directory(item['path']) != item:
            _fail('checkout_changed')
    return {'project': project, 'checkout': checkout, 'gitDir': admin,
            'commonGitDir': common, 'projectRelative': relative, 'gitObjectFormat': fields[3],
            'branchKey': branch, 'headOid': head, 'index': index, 'operations': operations}


def _stable(root, deadline):
    for _ in range(CAPTURE_ATTEMPTS):
        try:
            first, second = _facts(root, deadline), _facts(root, deadline)
            if first == second:
                return second
        except CheckoutUnavailable as exc:
            if exc.reason != 'checkout_changed':
                raise
        _remaining(deadline)
    _fail('checkout_changed')


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
    if {key: value[key] for key in _repository_fields(facts)} != _repository_fields(facts):
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
            or any(value[key] != facts[key] for key in ('project', 'checkout', 'gitDir'))):
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


def register_repository(project_root, *, deadline=None):
    facts = _stable(project_root, _deadline(deadline))
    return {'schema': REPOSITORY_SCHEMA, 'repoId': uuid.uuid4().hex, **_repository_fields(facts)}


def register_worktree(project_root, repository, *, deadline=None):
    facts = _stable(project_root, _deadline(deadline))
    _repository(repository, facts)
    return {'schema': WORKTREE_SCHEMA, 'worktreeId': uuid.uuid4().hex,
            'repoId': repository['repoId'], 'repositoryDigest': _digest(repository),
            **{key: facts[key] for key in ('project', 'checkout', 'gitDir')}}


def capture_view(project_root, repository, worktree, *, deadline=None):
    facts = _stable(project_root, _deadline(deadline))
    _repository(repository, facts)
    _worktree(worktree, repository, facts)
    value = {'schema': CAPTURE_SCHEMA, 'projectRoot': facts['project']['path'],
             'repoId': repository['repoId'], 'worktreeId': worktree['worktreeId'],
             'viewId': _digest([repository['repoId'], worktree['worktreeId'], facts['branchKey']]),
             'branchKey': facts['branchKey'], 'headOid': facts['headOid'],
             'gitObjectFormat': facts['gitObjectFormat'], 'evidence': facts}
    return {**value, 'checkoutDigest': _digest(value)}


def capture_matches(capture, repository, worktree, *, deadline=None):
    try:
        if (not _capture_shape(capture)
                or _digest({key: value for key, value in capture.items()
                            if key != 'checkoutDigest'}) != capture['checkoutDigest']):
            return False
        return capture_view(capture['projectRoot'], repository, worktree,
                            deadline=deadline) == capture
    except (CheckoutUnavailable, OSError, ValueError, TypeError, RecursionError):
        return False
