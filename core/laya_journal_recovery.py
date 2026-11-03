"""Explicit installer recovery of one interrupted in-place v4 -> v5 transaction."""
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile

import laya_install_journal as J
import laya_journal_reader as R

_MAGIC = bytes.fromhex('d9d505f920a163d7')
_FIELDS = {'format', 'project', 'project_node', 'state', 'state_node', 'journal_node',
           'install', 'base_node', 'base_sha', 'row_sha', 'epoch', 'creation_epoch',
           'endpoint', 'attestation_sha', 'transition'}


def _require(ok):
    R._need(ok, 'initial_conversion_evidence_invalid')


def _directory(path):
    st = path.lstat()
    _require(path.resolve(strict=True) == path and stat.S_ISDIR(st.st_mode)
             and st.st_uid == os.getuid() and not st.st_mode & 0o077)
    return path


def _exclusive(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _write(path, data):
    with os.fdopen(_exclusive(path), 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _read(path, limit):
    R._file(path, path.parent)
    return R._read_private(path, limit)


def _v4_row(db):
    objects = db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    _require(objects == [('selection', J._CREATE)])
    row = J._row(db)
    _require(row['_version'] == 4)
    return row


class _Preimage:
    def __init__(self, state, project):
        self.directory = _directory(state / J._INITIAL_INTENT)
        self.logical_path = R._journal(state / R._JOURNAL)
        self.base = self.directory / 'base.sqlite'
        self.intent = self.directory / 'intent.json'
        self.nodes = [R._node(p) for p in (self.directory, self.intent, self.base)]
        self.descriptor = _read(self.intent, 16384)
        data = json.loads(self.descriptor)
        _require(set(data) == _FIELDS and R._raw(data) == self.descriptor
                 and data['format'] == 'memvane-initial-reader-v5'
                 and (data['project'], data['project_node']) == (str(project), R._node(project))
                 and (data['state'], data['state_node']) == (str(state), R._node(state))
                 and data['journal_node'] == R._node(self.logical_path)
                 and data['base_node'] == self.nodes[2]
                 and J.ID.fullmatch(data['transition']) and J.ID.fullmatch(data['install'])
                 and type(data['creation_epoch']) is int and 1 <= data['creation_epoch'] <= data['epoch'])
        self.data = data
        self.revalidate()
        with closing(self.open_read()) as db:
            self.row = _v4_row(db)
        _require(self.row['_version'] == 4 and self.row['epoch'] == data['epoch']
                 and self.row['selected']['installId'] == data['install']
                 and R._sha(R._raw(self.row)) == data['row_sha'])

    def revalidate(self):
        _directory(self.directory)
        _require(set(os.listdir(self.directory)) == {'base.sqlite', 'intent.json'}
                 and self.nodes == [R._node(p) for p in (self.directory, self.intent, self.base)]
                 and R._node(_directory(self.logical_path.parent)) == self.data['state_node']
                 and R._node(Path(self.data['project'])) == self.data['project_node']
                 and _read(self.intent, 16384) == self.descriptor
                 and R._file(self.logical_path, self.logical_path.parent) == self.data['journal_node']
                 and R._sha(_read(self.base, R._MAX_IMAGE)) == self.data['base_sha'])

    def open_read(self):
        _require(R._file(self.base, self.directory) == self.nodes[2])
        db = sqlite3.connect(self.base.as_uri() + '?mode=ro&immutable=1', uri=True, isolation_level=None)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        return db


@contextmanager
def _preimage_reads(image):
    image.revalidate()
    token = J._initial_preimage.set(image)
    try:
        yield
    finally:
        J._initial_preimage.reset(token)


def prepare(state, project, endpoint, record, proof):
    path = R._journal(state / R._JOURNAL)
    final = state / J._INITIAL_INTENT
    _require(not os.path.lexists(final))
    stage = Path(tempfile.mkdtemp(prefix='.reader-prepare-', dir=state))
    base = stage / 'base.sqlite'
    manifest = stage / 'intent.json'
    try:
        os.close(_exclusive(base))
        with R._readonly(path, state) as src, closing(sqlite3.connect(
                base.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as dest:
            row = _v4_row(src)
            _require(row['_version'] == 4 and row['epoch'] == proof['selectionEpoch']
                     and row['selected']['installId'] == proof['installId']
                     and path.stat().st_size <= R._MAX_IMAGE)
            src.backup(dest)
        R._sync(base)
        data = dict(format='memvane-initial-reader-v5', state=str(state), state_node=R._node(state),
                    project=str(project), project_node=R._node(project), journal_node=R._node(path),
                    install=proof['installId'], base_node=R._node(base), base_sha=R._sha(_read(base, R._MAX_IMAGE)),
                    row_sha=R._sha(R._raw(row)), epoch=row['epoch'], creation_epoch=record['creation_epoch'],
                    endpoint=endpoint.endpoint_id, attestation_sha=record['attestation_sha256'],
                    transition=secrets.token_hex(16))
        _write(manifest, R._raw(data))
        R._sync(stage, True)
        _require(not os.path.lexists(final))
        os.rename(stage, final)
        R._sync(state, True)
    finally:
        if stage.exists():
            for owned in (manifest, base):
                if owned.exists():
                    owned.unlink()
            stage.rmdir()
    result = _Preimage(state, project)
    J._step('initial-intent-durable')
    return result


def finish(image):
    image.revalidate()
    stage = image.logical_path.parent / ('.reader-rehearse-' + image.data['transition'])
    if os.path.lexists(stage):
        with _rehearsal(image):
            pass
    # Retire both references atomically; a restart never trusts half an intent.
    old = image.directory.with_name('.reader-finished-' + secrets.token_hex(16))
    os.rename(image.directory, old)
    R._sync(old.parent, True)
    for name, node in zip(('intent.json', 'base.sqlite'), image.nodes[1:]):
        path = old / name
        _require(R._file(path, old) == node)
        path.unlink()
    old.rmdir()
    R._sync(old.parent, True)


def _match_live(db, image):
    row = J._row(db)
    if row['_version'] == 5:
        selected, _ = R._selection(db)
        _require(selected.epoch == 1 and selected.previous_endpoint is None and
                 selected.preferred_endpoint == image.data['endpoint'] and
                 selected.transition_id == image.data['transition'])
        row.update(_version=4, schema=J.SCHEMA)
    else:
        _v4_row(db)
    _require(row == image.row)


def _retire_clean_forward(image, state, project, executing_root):
    """Retire a stale intent only for proved, clean v4 mark-write progress."""
    path = image.logical_path
    sidecars = tuple(Path(str(path) + suffix) for suffix in ('-journal', '-wal', '-shm'))
    if any(os.path.lexists(side) for side in sidecars):
        return False
    with R._readonly(path, state) as db:
        current = J._row(db)
        if current['_version'] != 4 or current == image.row:
            return False
        _v4_row(db)
        _require(current['epoch'] > image.row['epoch']
                 and current['new_write_seen'] >= image.row['new_write_seen']
                 and current == dict(image.row, epoch=current['epoch'],
                                     new_write_seen=current['new_write_seen']))
        # No preimage context here: prove the actual current journal and every
        # store/provider binding while this read snapshot prevents a commit.
        record, att, proof = R._evidence(state, project, executing_root)
        _require(proof['selectionEpoch'] == current['epoch']
                 and proof['installId'] == image.data['install']
                 and R._sha(att) == image.data['attestation_sha'])
        record['creation_epoch'] = image.data['creation_epoch']
        _require(R._sha(b'memvane-reader-endpoint-v1\0' + R._raw(record)) == image.data['endpoint'])
        image.revalidate()
        _require(not any(os.path.lexists(side) for side in sidecars))
        finish(image)
    return True


@contextmanager
def _rehearsal(image):
    """One private, intent-bound metadata copy; never enumerate other jobs."""
    image.revalidate()
    state = image.logical_path.parent
    stage = state / ('.reader-rehearse-' + image.data['transition'])
    created = False
    try:
        stage.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    _directory(stage)
    stage_node = R._node(stage)
    owner = stage / 'owner.json'
    binding = R._raw(dict(format='memvane-reader-rehearsal-v1',
        state_node=image.data['state_node'], stage_node=stage_node,
        intent_node=image.nodes[1], intent_sha=R._sha(image.descriptor),
        transition=image.data['transition'], journal_node=image.data['journal_node']))
    if created:
        _write(owner, binding)
        R._sync(stage, True)
        R._sync(state, True)
    owner_node = R._file(owner, stage)

    def clear(remove=False):
        image.revalidate()
        _directory(stage)
        _require(R._node(stage) == stage_node and stat.S_IMODE(stage.lstat().st_mode) == 0o700
                 and R._file(owner, stage) == owner_node and _read(owner, 16384) == binding
                 and set(os.listdir(stage)) <= {'owner.json', 'trial.sqlite', 'trial.sqlite-journal'})
        entries = [(path, R._file(path, stage)) for path in
                   (stage / 'trial.sqlite-journal', stage / 'trial.sqlite') if os.path.lexists(path)]
        for path, node in entries:
            _require(R._node(stage) == stage_node and R._file(path, stage) == node)
            path.unlink()
        if remove:
            _require(R._file(owner, stage) == owner_node and _read(owner, 16384) == binding)
            owner.unlink()
            _require(R._node(stage) == stage_node)
            stage.rmdir()
            R._sync(state, True)
        else:
            R._sync(stage, True)

    clear()
    try:
        yield stage / 'trial.sqlite'
    finally:
        clear(remove=True)


def _repair_if_hot(image):
    path = image.logical_path
    side = Path(str(path) + '-journal')
    _require(not any(os.path.lexists(str(path) + s) for s in ('-wal', '-shm')))
    if os.path.lexists(side):
        R._file(side, path.parent)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        try:
            db.execute('PRAGMA application_id').fetchone()
        except sqlite3.OperationalError as exc:
            readonly = getattr(sqlite3, 'SQLITE_READONLY', 8)
            if (getattr(exc, 'sqlite_errorcode', 0) & 255) != readonly and 'readonly' not in str(exc).lower():
                raise
        else:
            _match_live(db, image)
            return
    # Rehearse SQLite recovery on private copies. A foreign rollback file
    # cannot repair the live inode first and only then fail our row check.
    image.revalidate()
    side_node = R._file(side, path.parent)
    raw = _read(path, R._MAX_IMAGE)
    rollback = _read(side, R._MAX_IMAGE)
    pages = int.from_bytes(rollback[16:20], 'big')
    page_size = int.from_bytes(rollback[24:28], 'big')
    _require(raw[:16] == b'SQLite format 3\0' and int.from_bytes(raw[60:64], 'big') in (4, 5)
             and len(rollback) > 512 and rollback[:8] == _MAGIC and not rollback.endswith(_MAGIC)
             and page_size in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
             and 0 < pages * page_size <= R._MAX_IMAGE)
    with _rehearsal(image) as candidate:
        _write(candidate, raw)
        _write(Path(str(candidate) + '-journal'), rollback)
        with closing(sqlite3.connect(candidate.as_uri() + '?mode=rw', uri=True)) as db:
            _require(_v4_row(db) == image.row and db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
    image.revalidate()
    _require(R._file(side, path.parent) == side_node and _read(side, R._MAX_IMAGE) == rollback
             and _read(path, R._MAX_IMAGE) == raw)
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as db:
        _require(_v4_row(db) == image.row)
    R._sync(path)
    R._sync(path.parent, True)
    J._step('initial-recovery-rolled-back')


def recover_initial(state, project, executing_root):
    from laya_initial_build import owner
    import laya_fresh_wiki_origin as fresh
    import project_dir
    from laya_index_sync import _owner_lock
    from laya_ledger_common import _operation_lock
    if not os.path.lexists(state / J._INITIAL_INTENT):
        return
    with ExitStack() as locks:
        locks.enter_context(R._install_lock(state))
        if not os.path.lexists(state / J._INITIAL_INTENT):
            return
        R._need(locks.enter_context(owner(state)), 'initial_build_busy')
        locks.enter_context(_operation_lock(project_dir.runtime_file(project, fresh.MARKER)))
        image = _Preimage(state, project)
        with _preimage_reads(image):
            _, _, proof = R._evidence(state, project, executing_root)
            queue = Path(J.select_route(str(project), action='recall')['queuePath'])
        R._need(locks.enter_context(_owner_lock(queue)), 'sync_owner_busy')
        locks.enter_context(R._transition(proof['installId']))
        with _preimage_reads(image):
            record, att, proof = R._evidence(state, project, executing_root)
            _require(proof['installId'] == image.data['install'] and
                     proof['selectionEpoch'] == image.data['epoch'] and
                     R._sha(att) == image.data['attestation_sha'])
            record['creation_epoch'] = image.data['creation_epoch']
            _require(R._sha(b'memvane-reader-endpoint-v1\0' + R._raw(record)) == image.data['endpoint'])
        image.revalidate()
        if _retire_clean_forward(image, state, project, executing_root):
            return
        J._step('initial-recovery-proved')
        _repair_if_hot(image)
        finish(image)
