"""UID-wide reader lifetime leases and steady, fully proved v5 endpoints.

Readers never create coordination files. Store movement and retained tuples
require their separate writer integration; this module grants no writer role.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

import laya_journal_reader as reader


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


def _prepare_lifetime(install_id):
    """Conversion writer prepares a stable lock before the v4 transaction."""
    parent, parent_fd, parent_info = _parent(install_id, create=True)
    try:
        try:
            fd = os.open('reader-lifetime.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        try:
            _named(install_id, parent, parent_fd, parent_info, fd)
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
    try:
        fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NOFOLLOW |
                     os.O_NONBLOCK, dir_fd=parent_fd)
        _private_file(os.fstat(fd))
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('reader_lifetime_busy') from exc
        _named(install_id, parent, parent_fd, parent_info, fd)
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


@contextmanager
def resolve_endpoint(project, *, action, host_root):
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
    initial = journal.inspect_journal(path)
    _need(initial['state'] in ('active', 'restored_old') and initial['pending'] is None
          and isinstance(initial['selected'], dict), 'reader_selection_unproved')
    install_id = initial['selected']['installId']
    _need(first.get('installId') == install_id, 'reader_install_changed')
    with reader_lifetime(install_id, exclusive=False):
        decision = journal.select_route(str(project), action=action)
        _need(decision == first and journal.inspect_journal(path) == initial,
              'reader_selection_changed')
        token = None
        try:
            # The journal COW inode and transaction pin selection+endpoint as
            # one complete image. Full evidence must agree with that image.
            with reader._readonly(path, path.parent) as db:
                bundle_row = journal._row(db)
                _need(bundle_row['state'] in ('active', 'restored_old')
                      and bundle_row['pending'] is None
                      and bundle_row['selected']['installId'] == install_id,
                      'reader_install_changed')
                selected, stored = reader._selection(db)
                _need(stored['install_id'] == install_id and stored['project'] == str(project)
                      and stored['state'] == str(path.parent), 'reader_install_changed')
                attestation = db.execute('SELECT attestation FROM read_endpoints WHERE endpoint_id=?',
                                         (selected.preferred_endpoint,)).fetchone()[0]
                token = reader._persisted_endpoint(path.parent, project, host_root,
                    selected.preferred_endpoint, stored, attestation)
                _need(journal.select_route(str(project), action=action) == decision,
                      'reader_selection_changed')
            yield token
        finally:
            if token is not None:
                reader._revoke_reader(token)
