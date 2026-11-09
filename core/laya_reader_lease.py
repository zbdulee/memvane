"""UID-wide reader lifetime leases and steady, fully proved v5 endpoints.

Readers never create coordination files. Store movement and retained tuples
require their separate writer integration; this module grants no writer role.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import math
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import stat
import time
import weakref

import laya_journal_reader as reader
from laya_move_read_seal import MoveReadSeal, stage_move_reader
from laya_move_reader_journal import publish_move_reader

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
              and reader.persistent_node_equal([row['project_dev'], row['project_ino']], reader._node(project))
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
              and reader.persistent_node_equal(record['state_node'], reader._node(state))
              and reader.persistent_node_equal(record['project_node'], reader._node(project))
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
    for key, record in records.items():
        _need(all(record.get(name) == old_stored[name]
                  for name in reader.stable_fields(record, old_stored))
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
    pointer = None
    fenced = False
    if first.get('state') == 'candidate':
        _need(first.get('projectRoot') == str(project), 'reader_selection_unproved')
        path = reader._journal(first['journalPath'])
    else:
        # Both a project move and a bundle upgrade can expose an upgrading
        # ledger. A present move pointer chooses the retained-move proof;
        # malformed or unsupported pointers must not fall through to upgrade.
        import laya_project_dir_move as mover
        pointer = mover.read_pointer(project)
        if pointer is not None:
            _need(pointer['mode'] == 'rename', 'reader_selection_unproved')
            path = reader._journal(Path(pointer['stateRoot']) / reader._JOURNAL)
        else:
            import laya_fresh_wiki_origin as fresh
            hint = fresh._fence_record(project)
            _need(first.get('state') == 'blocked' and hint is not None and
                  hint['projectRoot'] == str(project) and hint['phase'] == 'upgrading',
                  'reader_selection_unproved')
            path = reader._journal(Path(hint['journalPath']))
            fenced = True
    initial = _read_v5_bundle(path)
    _need(((initial['state'] in ('active', 'restored_old') and initial['pending'] is None) or
           (fenced and initial['state'] == 'pending' and initial['pending'] is not None))
          and isinstance(initial['selected'], dict), 'reader_selection_unproved')
    install_id = initial['selected']['installId']
    _need(pointer is not None or fenced or first.get('installId') == install_id, 'reader_install_changed')
    with reader_lifetime(install_id, exclusive=False):
        decision = journal.select_route(str(project), action=action)
        _need(decision == first and _read_v5_bundle(path) == initial,
              'reader_selection_changed')
        if pointer is not None:
            _need(mover.read_pointer(project) == pointer, 'reader_selection_changed')
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
                        _need(((bundle_row['state'] in ('active', 'restored_old')
                                and bundle_row['pending'] is None) or
                               (fenced and bundle_row['state'] == 'pending'
                                and bundle_row['pending'] is not None))
                              and bundle_row['selected']['installId'] == install_id,
                              'reader_install_changed')
                        _need(stored['install_id'] == install_id and stored['project'] == str(project)
                              and stored['state'] == str(path.parent), 'reader_install_changed')
                        _need(pointer is None or stored.get('capability') == 'v6-retained-ro-v1',
                              'reader_selection_unproved')
                        if prior is not None:
                            _forward_image(prior, image)
                        from laya_reader_windows import reader_proof_windows
                        with reader_proof_windows(install_id, deadline=time.monotonic()+2):
                            issue = (reader._persisted_upgrade_endpoint if fenced else
                                     reader._persisted_endpoint)
                            token = issue(path.parent, project, host_root, selected.preferred_endpoint,
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


@dataclass(frozen=True)
class PinnedConnections:
    connections: tuple
    values: tuple


def _snapshot_time(deadline):
    if time.monotonic() >= deadline:
        raise ValueError('reader_snapshot_deadline')


def _snapshot_identity(path):
    path=Path(path)
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError('reader_snapshot_identity_changed')
    parent=path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise ValueError('reader_snapshot_identity_changed')
    parent_id=(parent.st_dev,parent.st_ino,parent.st_mode,parent.st_uid)
    try:
        info=path.lstat()
    except FileNotFoundError:
        return ('absent',parent_id)
    if (path.is_symlink() or path.resolve(strict=True) != path
            or not stat.S_ISREG(info.st_mode)):
        raise ValueError('reader_snapshot_identity_changed')
    return ('present',parent_id,info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink)


def _snapshot_check_connection(db,path):
    if db.in_transaction or db.isolation_level is not None:
        raise ValueError('reader_snapshot_connection_invalid')
    if db.execute('PRAGMA query_only').fetchone() != (1,):
        raise ValueError('reader_snapshot_connection_invalid')
    rows=db.execute('PRAGMA database_list').fetchall()
    main=[item[2] for item in rows if item[1]=='main']
    if len(main)!=1 or Path(main[0]) != path:
        raise ValueError('reader_snapshot_connection_invalid')


def _snapshot_version(db):
    value=db.execute('PRAGMA main.data_version').fetchone()
    if value is None or len(value)!=1 or type(value[0]) is not int:
        raise ValueError('reader_snapshot_connection_invalid')
    return value[0]


@contextmanager
def pin_common(factory, *, deadline, fallback=None):
    """Pin a simultaneous image, then optionally try a protected cut.

    Factory yields (path, monitoring connection, snapshot connection, pin
    callback) tuples and owns all handle cleanup. An absent file is represented
    by (path, None, None, None), and its parent/name are observed on both sides.
    The callback observes only
    the snapshot connection and returns the caller's initial pinned value.
    No authorization or full v6 reader proof is conferred by this helper.
    """
    if (not callable(factory) or (fallback is not None and not callable(fallback))
            or type(deadline) not in (int,float)
            or not math.isfinite(deadline)):
        raise ValueError('reader_snapshot_input_invalid')
    for _attempt in range(3 if fallback is not None else 2):
        _snapshot_time(deadline)
        with factory() as members:
            members=tuple(members)
            if not members:
                raise ValueError('reader_snapshot_input_invalid')
            paths=[];handles=[];started=[]
            try:
                for member in members:
                    if not isinstance(member,tuple) or len(member)!=4:
                        raise ValueError('reader_snapshot_input_invalid')
                    path=Path(member[0]);monitor,snapshot=member[1:3]
                    absent=(monitor is None and snapshot is None and member[3] is None)
                    if path in paths or (not absent and (not callable(member[3])
                            or monitor is None or snapshot is None or monitor is snapshot
                            or any(db is monitor or db is snapshot for db in handles))):
                        raise ValueError('reader_snapshot_input_invalid')
                    _snapshot_time(deadline)
                    if not absent:
                        _snapshot_check_connection(monitor,path);_snapshot_check_connection(snapshot,path)
                        handles.extend((monitor,snapshot))
                    paths.append(path)
                with (fallback() if _attempt == 2 else nullcontext()):
                    _snapshot_time(deadline)
                    before=[]
                    for path,monitor,_snapshot,_pin in members:
                        _snapshot_time(deadline);identity=_snapshot_identity(path)
                        if (identity[0]=='absent') != (monitor is None):
                            raise ValueError('reader_snapshot_identity_changed')
                        before.append((identity,None if monitor is None else _snapshot_version(monitor)))
                    values=[]
                    for _path,_monitor,snapshot,pin in members:
                        if snapshot is None:
                            values.append(None)
                            continue
                        _snapshot_time(deadline);snapshot.execute('BEGIN');started.append(snapshot)
                        # A SELECT establishes even a snapshot of an empty table.
                        snapshot.execute('SELECT count(*) FROM main.sqlite_master').fetchone()
                        values.append(pin(snapshot))
                        if not snapshot.in_transaction:
                            raise ValueError('reader_snapshot_connection_invalid')
                    after=[]
                    for path,monitor,_snapshot,_pin in members:
                        _snapshot_time(deadline);after.append((_snapshot_identity(path),None if monitor is None else _snapshot_version(monitor)))
                    if before != after:
                        continue
                _snapshot_time(deadline)
                yield PinnedConnections(tuple(member[2] for member in members),tuple(values))
                return
            finally:
                rollback_error = None
                for db in reversed(started):
                    if db.in_transaction:
                        try:
                            db.rollback()
                        except BaseException as exc:
                            if rollback_error is None:
                                rollback_error = exc
                if rollback_error is not None:
                    raise rollback_error
    raise ValueError('reader_snapshot_changed')
