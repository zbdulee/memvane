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

import laya_journal_reader as reader

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


def _image(db):
    """Capture one complete immutable journal image, including its CAS prefix."""
    bundle = reader.journal._row(db)
    selected, stored = reader._selection(db)
    transitions = tuple(db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id '
                                   'FROM reader_transitions ORDER BY epoch'))
    endpoints = {row[0]: (row[1], row[2]) for row in db.execute(
        'SELECT endpoint_id,record,attestation FROM read_endpoints')}
    records = {key: reader._stored_endpoint(db, key) for key in endpoints}
    _need(len(transitions) == selected.epoch, 'reader_selection_invalid')
    previous = None
    referenced = set()
    for epoch, preferred, old, transition in transitions:
        _need(old == previous and type(transition) is str
              and reader.journal.ID.fullmatch(transition), 'reader_selection_invalid')
        referenced.add(preferred)
        previous = preferred
    _need(tuple(row[0] for row in transitions) == tuple(range(1, selected.epoch + 1))
          and transitions[-1] == (selected.epoch, selected.preferred_endpoint,
                                  selected.previous_endpoint, selected.transition_id)
          and referenced == set(endpoints), 'reader_selection_invalid')
    return bundle, selected, stored, transitions, endpoints, records


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


def _read_v5_bundle(path):
    """Read a candidate journal without the legacy writer-recovery opener."""
    with reader._readonly(path, path.parent) as db:
        _need(reader.journal._schema_version_read(db) == 5, 'reader_schema_unknown')
        return reader.journal._row(db)


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
    initial = _read_v5_bundle(path)
    _need(initial['state'] in ('active', 'restored_old') and initial['pending'] is None
          and isinstance(initial['selected'], dict), 'reader_selection_unproved')
    install_id = initial['selected']['installId']
    _need(first.get('installId') == install_id, 'reader_install_changed')
    with reader_lifetime(install_id, exclusive=False):
        decision = journal.select_route(str(project), action=action)
        _need(decision == first and _read_v5_bundle(path) == initial,
              'reader_selection_changed')
        token = None
        try:
            prior = None
            for attempt in range(3):
                node, image = reader._node(path), None
                proved = False
                try:
                    with reader._readonly(path, path.parent) as db:
                        image = _image(db)
                        bundle_row, selected, stored, _transitions, endpoints, _records = image
                        _need(bundle_row['state'] in ('active', 'restored_old')
                              and bundle_row['pending'] is None
                              and bundle_row['selected']['installId'] == install_id,
                              'reader_install_changed')
                        _need(stored['install_id'] == install_id and stored['project'] == str(project)
                              and stored['state'] == str(path.parent), 'reader_install_changed')
                        if prior is not None:
                            _forward_image(prior, image)
                        from laya_reader_windows import reader_proof_windows
                        with reader_proof_windows(install_id, deadline=time.monotonic()+2):
                            token = reader._persisted_endpoint(path.parent, project, host_root, selected.preferred_endpoint,
                                          stored, endpoints[selected.preferred_endpoint][1])
                        _need(journal.select_route(str(project), action=action) == decision,
                              'reader_selection_changed')
                        proved = True
                    break
                except ValueError as exc:
                    if token is not None:
                        reader._revoke_reader(token)
                        token = None
                    # A replaced name is insufficient. The next complete image
                    # must prove a strict reader CAS extension of this snapshot.
                    if (not proved or str(exc) != 'reader_store_changed' or image is None or
                            reader._node(path) == node or attempt == 2):
                        raise
                    prior = image
            _reader_selections[token] = selected
            yield token
        finally:
            if token is not None:
                _reader_selections.pop(token, None)
                reader._revoke_reader(token)
