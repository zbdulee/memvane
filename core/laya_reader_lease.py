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
from laya_move_read_seal import MoveReadSeal, stage_move_reader
from laya_move_reader_journal import publish_move_reader


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


def _move_identity(state, project, intent):
    """Bind lifetime admission to the real selected journal, including resume.

    Move metadata is already validated by the owning mover. It supplies only
    its exact old/new state aliases, never a search path or a reader capability.
    The mover retains all of its own route/approval/provider validations.
    """
    journal = reader.journal
    state, project = Path(state), Path(project)
    path = state / 'install-journal.sqlite'
    version = journal.journal_version(path)
    _need(version in (3, 4, 5), 'reader_schema_unknown')
    if version != 5:
        return None
    state = reader._state(state)
    path = reader._journal(path)
    _need(project.is_absolute() and project.resolve(strict=True) == project,
          'reader_move_project_invalid')
    with reader._readonly(path, state) as db:
        row = journal._row(db)
        selection, record = reader._selection(db)
        selected = row['selected']
        _need(row['state'] in ('active', 'restored_old') and row['pending'] is None
              and isinstance(selected, dict) and row['project_root'] == str(project)
              and [row['project_dev'], row['project_ino']] == reader._node(project)
              and isinstance(selected.get('vectorProfile'), dict),
              'reader_move_selection_unproved')
        install_id = selected['installId']
        states = {str(state)}
        if intent is not None:
            _need(intent.get('projectRoot') == str(project)
                  and intent.get('installId') == install_id, 'reader_install_changed')
            if 'fromRoot' in intent or 'toRoot' in intent:
                old, new = intent.get('fromRoot'), intent.get('toRoot')
                _need(type(old) is str and type(new) is str and str(state) in (old, new)
                      and Path(old).is_absolute() and Path(new).is_absolute()
                      and Path(old).name == install_id and Path(new).name == install_id,
                      'reader_move_state_invalid')
                states.update((old, new))
            else:
                _need(intent.get('stateRoot') == str(state), 'reader_move_state_invalid')
        _need(record['install_id'] == install_id and record['scope'] == row['scope_id']
              and record['project'] == str(project) and record['state'] in states
              and record['state_node'] == reader._node(state)
              and record['project_node'] == reader._node(project)
              and record['profile'] == selected['vectorProfile']['profileId'],
              'reader_install_changed')
        return install_id, row, selection, record


@contextmanager
def move_lifetime(state, project, *, intent=None):
    """Exclude v5 readers during an existing mover's physical switch/settle.

    Legacy journal formats retain their existing mover behavior. This is
    admission only: it does not seal retained tuples or legal crash prefixes.
    """
    before = _move_identity(state, project, intent)
    if before is None:
        yield None
        return
    with reader_lifetime(before[0], exclusive=True) as fd:
        _need(_move_identity(state, project, intent) == before, 'reader_selection_changed')
        yield fd


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
    from laya_move_reader_journal import seal_prefix
    return bundle, selected, stored, transitions, endpoints, records, seal_prefix(db)


def _forward_image(before, after):
    """Admit only an exact immutable prefix followed by reader-only CAS steps."""
    old_bundle, old, old_stored, prefix, endpoints, _, old_seals = before
    bundle, selected, _stored, transitions, current, records, new_seals = after
    _need(bundle == old_bundle and selected.epoch > old.epoch
          and transitions[:len(prefix)] == prefix
          and all(current.get(key) == value for key, value in endpoints.items()),
          'reader_store_changed')
    _need(all(new_seals.get(key) == value for key,value in old_seals.items()), 'reader_store_changed')
    stable = ('scope', 'install_id', 'project', 'state', 'incarnation', 'project_node',
              'state_node', 'attestation_sha256', 'profile', 'checkout_policy', 'settings', 'roots')
    for key, record in records.items():
        _need(all(record.get(name) == old_stored[name] for name in stable)
              and current[key][1] == endpoints[old.preferred_endpoint][1],
              'reader_install_changed')


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
            prior = None
            for attempt in range(3):
                node, image = reader._node(path), None
                proved = False
                try:
                    with reader._readonly(path, path.parent) as db:
                        image = _image(db)
                        bundle_row, selected, stored, _transitions, endpoints, _records, _seals = image
                        _need(bundle_row['state'] in ('active', 'restored_old')
                              and bundle_row['pending'] is None
                              and bundle_row['selected']['installId'] == install_id,
                              'reader_install_changed')
                        _need(stored['install_id'] == install_id and stored['project'] == str(project)
                              and stored['state'] == str(path.parent), 'reader_install_changed')
                        if prior is not None:
                            _forward_image(prior, image)
                        token = reader._persisted_endpoint(path.parent, project, host_root,
                            selected.preferred_endpoint, stored, endpoints[selected.preferred_endpoint][1])
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
            yield token
        finally:
            if token is not None:
                reader._revoke_reader(token)
