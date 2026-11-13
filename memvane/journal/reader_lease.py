"""UID-wide reader lifetime leases and steady, fully proved v5 endpoints.

Readers never create coordination files. This module grants no writer role.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import stat
import time
import weakref

from memvane.journal import journal_reader as reader

_shared_stack = ContextVar('reader_shared_lifetimes', default=())
_shared_scopes = weakref.WeakSet()
_scoped_readers = weakref.WeakKeyDictionary()
_reader_selections = weakref.WeakKeyDictionary()


class _SharedScope:
    """Cooperative FD ownership; callers must not close or repurpose lease FDs."""
    pass


def _check_shared_scope(scope):
    _need(scope in _shared_scopes and scope.pid == os.getpid(), 'reader_lifetime_expired')
    _named(*scope.named)


def _require_reader_scope(install_id):
    for scope in reversed(_shared_stack.get()):
        if scope.install_id == install_id:
            _check_shared_scope(scope)
            return scope
    raise ValueError('reader_shared_lifetime_required')


def _bind_reader_scope(endpoint, scope):
    _check_shared_scope(scope)
    _need(endpoint in reader._issued and endpoint in reader._read_only
          and endpoint.record['install_id'] == scope.install_id, 'reader_lifetime_invalid')
    scope.tokens.add(endpoint)
    _scoped_readers[endpoint] = scope


def _bound(endpoint, *, capability):
    """Require the issued read-only endpoint and its live reader scope."""
    _need(capability == reader.SCHEMA3_CAPABILITY, 'reader_endpoint_unsupported')
    _need(type(endpoint) is reader.VerifiedEndpoint and endpoint in reader._issued
          and endpoint in reader._read_only and endpoint in _scoped_readers,
          'reader_endpoint_not_issued')
    scope = _scoped_readers[endpoint]
    _check_shared_scope(scope)
    _need(scope in _shared_stack.get() and endpoint.record['install_id'] == scope.install_id,
          'reader_lifetime_invalid')
    _need(endpoint.record['capability'] == capability, 'reader_endpoint_unsupported')
    return scope


def _check_reader_scope(endpoint):
    scope = _scoped_readers.get(endpoint)
    if scope is not None:
        _check_shared_scope(scope)


def _need(value, reason):
    reader._need(value, reason)


def _private_file(info):
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
          and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
          'reader_lifetime_unsafe')


def _parent(install_id, *, create):
    path = reader._lock_dir(install_id, create=create)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
              and stat.S_IMODE(info.st_mode) == 0o700
              and os.path.samestat(info, path.lstat()), 'reader_lifetime_changed')
        return path, fd, info
    except BaseException:
        os.close(fd)
        raise


def _named(install_id, parent, parent_fd, parent_info, fd):
    # Re-run the existing UID/test-namespace private ancestor validation after
    # flock, and compare the open parent as well as the final named inode.
    _need(reader._lock_dir(install_id, create=False) == parent
          and os.path.samestat(parent_info, os.fstat(parent_fd))
          and os.path.samestat(parent_info, parent.lstat()), 'reader_lifetime_changed')
    info = os.fstat(fd)
    named = os.stat('reader-lifetime.lock', dir_fd=parent_fd, follow_symlinks=False)
    _private_file(info)
    _private_file(named)
    _need(os.path.samestat(info, named), 'reader_lifetime_changed')


def _prepare_lifetime(install_id, *, create=True):
    """Conversion writer prepares a stable lock before the v4 transaction."""
    _need(type(create) is bool, 'reader_lifetime_invalid')
    parent, parent_fd, parent_info = _parent(install_id, create=create)
    try:
        if create:
            try:
                fd = os.open('reader-lifetime.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
        else:
            fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        try:
            _named(install_id, parent, parent_fd, parent_info, fd)
            if create:
                os.fsync(fd)
                os.fsync(parent_fd)
            _named(install_id, parent, parent_fd, parent_info, fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


@contextmanager
def reader_lifetime(install_id, *, exclusive):
    """Noncreating shared/exclusive lease; both roles open the lock read-only."""
    _need(type(exclusive) is bool, 'reader_lifetime_invalid')
    parent, parent_fd, parent_info = _parent(install_id, create=False)
    fd = None
    scope = None
    context_token = None
    try:
        fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NOFOLLOW |
                     os.O_NONBLOCK, dir_fd=parent_fd)
        _private_file(os.fstat(fd))
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('reader_lifetime_busy') from exc
        _named(install_id, parent, parent_fd, parent_info, fd)
        if not exclusive:
            scope = _SharedScope()
            scope.install_id, scope.pid = install_id, os.getpid()
            scope.named = (install_id, parent, parent_fd, parent_info, fd)
            scope.tokens = weakref.WeakSet()
            _shared_scopes.add(scope)
            context_token = _shared_stack.set((*_shared_stack.get(), scope))
        yield fd
    finally:
        try:
            if scope is not None:
                _shared_scopes.discard(scope)
                if context_token is not None:
                    _shared_stack.reset(context_token)
                for endpoint in list(scope.tokens):
                    _scoped_readers.pop(endpoint, None)
                    reader._revoke_reader(endpoint)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent_fd)


def _forward_image(before, after):
    """Admit only an exact immutable prefix followed by reader-only CAS steps."""
    old_bundle, old, old_stored, prefix, endpoints, _ = before
    bundle, selected, _stored, transitions, current, records = after
    _need(bundle == old_bundle and selected.epoch > old.epoch
          and transitions[:len(prefix)] == prefix
          and all(current.get(key) == value for key, value in endpoints.items()),
          'reader_store_changed')
    for key, record in records.items():
        fields = reader.stable_fields(record, old_stored)
        _need(reader.records_agree({name: record.get(name) for name in fields}, old_stored, fields)
              and current[key][1] == endpoints[old.preferred_endpoint][1],
              'reader_install_changed')


@contextmanager
def resolve_endpoint(project, *, action, host_root):
    """Resolve and hold a reader without invoking legacy journal recovery."""
    with reader.journal.read_only_journals():
        with _resolve_endpoint(project, action=action, host_root=host_root) as endpoint:
            yield endpoint


@contextmanager
def _resolve_endpoint(project, *, action, host_root):
    """Yield only the persisted preferred steady endpoint under lifetime SH."""
    journal = reader.journal
    project, host_root = Path(project), Path(host_root)
    _need(type(action) is str and action == 'recall'
          and project.is_absolute() and project.resolve(strict=True) == project
          and host_root.is_absolute() and host_root.resolve(strict=True) == host_root,
          'reader_resolution_invalid')
    first = journal.select_route(str(project), action=action)
    _need(first.get('state') == 'candidate' and first.get('projectRoot') == str(project),
          'reader_selection_unproved')
    path = reader._journal(first['journalPath'])
    if first.get('routeSchema') == 'memvane-route-v4':
        with reader_lifetime(first['installId'], exclusive=False):
            row = journal.inspect_journal(path)
            _need(row['state'] == 'selected' and isinstance(row['reader'], dict)
                  and journal.select_route(str(project), action=action) == first,
                  'reader_selection_unproved')
            endpoint = reader._issue_v1_reader(path.parent, project, host_root, row['reader'])
            selected = object.__new__(reader.ReaderSelection)
            object.__setattr__(selected, 'epoch', row['epoch'])
            object.__setattr__(selected, 'preferred_endpoint', endpoint.endpoint_id)
            object.__setattr__(selected, 'previous_endpoint', None)
            object.__setattr__(selected, 'transition_id', row['installId'])
            _reader_selections[endpoint] = selected
            try:
                _need(journal.inspect_journal(path) == row, 'reader_selection_changed')
                yield endpoint
            finally:
                _reader_selections.pop(endpoint, None)
                reader._revoke_reader(endpoint)
        return
    raise ValueError('reader_schema_unknown')
