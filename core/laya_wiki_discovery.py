"""Selected v2 raw -> wiki source discovery from private source evidence.

The Laya index contains wiki cards only. After a successful sync (indexed or
unchanged), the off-hook worker scans bounded raw roots and compares path/hash
pairs with a private cursor; raw bodies never enter Laya membership, FTS or
vectors. Cutover seeds the cursor from the proved QMD inventory before route
activation. Changed revisions go to the existing
`wiki_compile_enqueue.enqueue_changed_sources` trigger policy. Deleted paths
are dropped; wiki-only changes are ignored.

Consent is never inferred from a laya-sync wake. A selected host hook records a
grant only for SessionStart (`session_start`) or a successful file-change event
(`sync`); index, stale recall and wiki-card sync record none. Each grant is one
random id with two phases. It is recorded provisional before the hook requests
its wake, and activated only after every hook check has passed; a run never
uses a provisional id, so a hook that fails (wake refused, proof changed, any
later rejection) or dies, or whose record write was uncertain, leaves an inert
id that only the per-kind cap retires. After activation the hook requests one
more coalesced wake: a run that started earlier saw the grant provisional and
may already be ACKed. The worker snapshots ids of both phases before its scan
and may use only snapshotted ids that are active under the lock, so the scan
always follows the event, a grant recorded mid-run waits for its own wake and
a grant revoked mid-run is never used. Without an accepted grant revisions
stay pending. A denied grant is retired alone; its revisions wait for an
accepted one. One enqueue per run obeys the existing per-wake cap:
`deferredFiles` stay pending and wait for the next grant, never a self-wake. A
blocked run ends its wake, so it retires the active grants it observed; its
revisions stay pending. The worker never removes a provisional id.

State is two small JSON files beside the bound claim queue, named by scope and
DB incarnation: the cursor (`baseGen`, `rawSnapshot` and pending `{path: sha256}`, spent
grant ids; never source bytes) and the active and provisional grant ids. Both are 0600,
no-follow and bounded, replaced atomically under one lock. Order: cursor
journal -> enqueue -> ACK (cursor + spent ids, one replace) -> grant removal,
so a crash re-enqueues (the live-queue key coalesces it), never loses a
revision, and a failed removal can never let a spent grant pay for the
deferred tail. Unknown or corrupt state fails closed: nothing is enqueued and
nothing is reset to a fresh baseline. A selected installed route requires its
pre-activation cursor; losing that cursor blocks discovery. Standalone
synthetic routes without an installation journal may start at the current raw
snapshot and report `cursorStarted`, without claiming earlier changes.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

STATE_SCHEMA = 'laya-wiki-discovery-v2'
GRANTS_SCHEMA = 'laya-wiki-discovery-grants-v3'
# Processing order: the sync grant accepts the wider existing trigger set.
GRANT_KINDS = ('sync', 'session_start')
MAX_FILE_BYTES = 1 << 20
MAX_PENDING = 4096
MAX_MEMBERS = 50_000
MAX_GRANT_COUNT = 1000
MAX_LINEAGE = 4096
# Standalone synthetic routes can start without a pre-activation cursor.
FIRST_RUN_LINEAGE_STEPS = 1
HOOK_LOCK_SECONDS = 1.0
WORKER_LOCK_SECONDS = 5.0
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_GENERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_STATE_KEYS = frozenset({'schema', 'scopeId', 'dbIncarnation', 'collectionRoots',
                         'rawCollections', 'baseGen', 'rawSnapshot', 'pending', 'spentGrants'})
_GRANT_KEYS = frozenset({'schema', 'scopeId', 'dbIncarnation', 'grants', 'provisional'})


class DiscoveryBlocked(Exception):
    """Fail-closed reason code; never a path or source byte."""


class GrantUncertain(DiscoveryBlocked):
    """A failed write left an id that may still be active."""


class DiscoveryBusy(Exception):
    """The lock stayed held past its bounded wait."""


def _binding(decision):
    scope, incarnation = decision.get('scopeId'), decision.get('dbIncarnation')
    queue, project = decision.get('queuePath'), decision.get('projectRoot')
    collections, roots = decision.get('collections'), decision.get('collectionRoots')
    if (not isinstance(scope, str) or not _HEX32.fullmatch(scope)
            or not isinstance(incarnation, str) or not _HEX32.fullmatch(incarnation)
            or not isinstance(queue, str) or not Path(queue).is_absolute()
            or not isinstance(project, str) or not Path(project).is_absolute()
            or not isinstance(collections, list) or not isinstance(roots, dict)
            or any(not isinstance(item, dict) or roots.get(item.get('name')) != item.get('sourceRoot')
                   for item in collections) or len(roots) != len(collections)):
        raise DiscoveryBlocked('binding_invalid')
    raw = sorted(item['name'] for item in collections if item.get('role') == 'raw')
    if not raw:
        raise DiscoveryBlocked('binding_invalid')
    return scope, incarnation, Path(queue), dict(sorted(roots.items())), raw


def _paths(decision):
    scope, incarnation, queue, _roots, _raw = _binding(decision)
    try:
        info = os.lstat(queue.parent)
    except OSError as exc:
        raise DiscoveryBlocked('state_dir_unavailable') from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise DiscoveryBlocked('state_dir_unsafe')
    stem = '%s.wiki-discovery-v2-%s-%s' % (queue.name, scope, incarnation)
    return (queue.parent / (stem + '.json'), queue.parent / (stem + '.grants.json'),
            queue.parent / (queue.name + '.wiki-discovery.lock'))


@contextmanager
def _locked(path, wait):
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    except OSError as exc:
        raise DiscoveryBlocked('lock_unavailable') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise DiscoveryBlocked('lock_unsafe')
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DiscoveryBusy('lock_busy')
                time.sleep(.02)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def _read(path):
    """One owned, private, regular JSON object; None only when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DiscoveryBlocked('state_unreadable') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_size > MAX_FILE_BYTES):
            raise DiscoveryBlocked('state_unsafe')
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise DiscoveryBlocked('state_unsafe')
    finally:
        os.close(fd)
    try:
        value = json.loads(b''.join(chunks).decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise DiscoveryBlocked('state_corrupt') from exc
    if not isinstance(value, dict):
        raise DiscoveryBlocked('state_corrupt')
    return value


def _write(path, value):
    """Atomic 0600 replace; the temp is exclusive and never followed."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        raise DiscoveryBlocked('state_too_large')
    tmp = path.with_name('.%s.%s.tmp' % (path.name, uuid.uuid4().hex))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _grant_ids(ids, limit):
    return (isinstance(ids, list) and len(ids) <= limit
            and all(isinstance(grant, str) and _HEX32.fullmatch(grant) for grant in ids)
            and len(set(ids)) == len(ids))


def _quarantine(path):
    """Move an unparseable grants file aside under a unique name; keep it."""
    os.replace(path, str(path) + '.corrupt-%d' % time.time_ns())


def _read_grants(path, decision, *, heal=False):
    """(active, provisional) ids by kind; an id is in at most one list.

    ``heal`` (hook entry points only): a grants file that does not parse or
    does not match its schema is moved aside and read as empty. Without it
    every SessionStart and file-change hook failed for good, with nothing to
    repair the file. Losing grants only delays compiles until the next
    accepted trigger. Worker reads keep failing closed (``blocked``), so the
    drain never runs on grants it cannot prove; the next hook heals the file.
    An unsafe file (owner, mode, links, size) always fails: it may not be ours.
    """
    scope, incarnation, _queue, _roots, _raw = _binding(decision)
    empty = ({kind: [] for kind in GRANT_KINDS}, {kind: [] for kind in GRANT_KINDS})
    try:
        value = _read(path)
    except DiscoveryBlocked as exc:
        if not heal or str(exc) != 'state_corrupt':
            raise
        _quarantine(path)
        return empty
    if value is None:
        return empty
    phases = (value.get('grants'), value.get('provisional'))
    if (set(value) != _GRANT_KEYS or value['schema'] != GRANTS_SCHEMA
            or (value['scopeId'], value['dbIncarnation']) != (scope, incarnation)
            or any(not isinstance(ids, dict) or set(ids) != set(GRANT_KINDS)
                   or any(not _grant_ids(row, MAX_GRANT_COUNT) for row in ids.values())
                   for ids in phases)):
        if not heal:
            raise DiscoveryBlocked('grants_corrupt')
        _quarantine(path)
        return empty
    every = [grant for ids in phases for kind in GRANT_KINDS for grant in ids[kind]]
    if len(set(every)) != len(every):
        if not heal:
            raise DiscoveryBlocked('grants_corrupt')
        _quarantine(path)
        return empty
    return tuple({kind: list(ids[kind]) for kind in GRANT_KINDS} for ids in phases)


def _write_grants(path, decision, active, provisional):
    scope, incarnation, _queue, _roots, _raw = _binding(decision)
    _write(path, {'schema': GRANTS_SCHEMA, 'scopeId': scope,
                  'dbIncarnation': incarnation, 'grants': active,
                  'provisional': provisional})


def _drop(path, decision, ids, *, provisional=True):
    """Remove exactly these grant ids (caller holds the lock); True if any was live.

    The worker passes provisional=False: it never retires an id whose hook
    may still activate it.
    """
    if not ids:
        return False
    active, waiting = _read_grants(path, decision)
    kept = {kind: [grant for grant in active[kind] if grant not in ids]
            for kind in GRANT_KINDS}
    kept_waiting = ({kind: [grant for grant in waiting[kind] if grant not in ids]
                     for kind in GRANT_KINDS} if provisional else waiting)
    if (kept, kept_waiting) == (active, waiting):
        return False
    _write_grants(path, decision, kept, kept_waiting)
    return True


def _withdraw(path, decision, grant):
    """Remove one id; if the write fails, prove by re-read that it is not active.

    The replace may land before the directory fsync fails, so a failed write
    is not proof either way. True unless the id was proved absent before.
    Raises GrantUncertain if the id may still be usable.
    """
    try:
        return _drop(path, decision, {grant})
    except Exception as exc:
        try:
            active, _waiting = _read_grants(path, decision)
        except Exception:
            raise GrantUncertain('grant_uncertain') from exc
        if any(grant in active[kind] for kind in GRANT_KINDS):
            raise GrantUncertain('grant_uncertain') from exc
        return True


def record_grant(decision, kind, *, wait=HOOK_LOCK_SECONDS):
    """Hook side: record one host event, provisional; no run may use it yet.

    Returns the grant id for activate_grant once every hook check passed.
    The grant says only which event happened. Whether it pays for a compile
    is the existing trigger gate, applied off-hook when the grant is used.
    """
    if kind not in GRANT_KINDS:
        raise ValueError('invalid_wiki_grant')
    _state, path, lock = _paths(decision)
    grant = uuid.uuid4().hex
    with _locked(lock, wait):
        active, waiting = _read_grants(path, decision, heal=True)
        # Bounded: past the cap the oldest provisional id of this kind goes;
        # its hook then fails to activate it, closed.
        waiting[kind] = (waiting[kind] + [grant])[-MAX_GRANT_COUNT:]
        try:
            _write_grants(path, decision, active, waiting)
        except BaseException:
            # The replace may have landed. The caller never learns this id, so
            # it is never activated; removal only frees its slot.
            try:
                _drop(path, decision, {grant})
            except Exception:
                pass
            raise
    return grant


def activate_grant(decision, kind, grant, *, wait=HOOK_LOCK_SECONDS):
    """Hook side, after every check passed: make this event's grant usable.

    False if the id is no longer provisional (trimmed or withdrawn); nothing
    was activated. A failed write is withdrawn before the error propagates;
    GrantUncertain means even that could not be proved.
    """
    if kind not in GRANT_KINDS:
        raise ValueError('invalid_wiki_grant')
    _state, path, lock = _paths(decision)
    with _locked(lock, wait):
        active, waiting = _read_grants(path, decision, heal=True)
        if grant not in waiting[kind]:
            return False
        waiting[kind] = [item for item in waiting[kind] if item != grant]
        active[kind] = (active[kind] + [grant])[-MAX_GRANT_COUNT:]
        try:
            _write_grants(path, decision, active, waiting)
        except BaseException:
            _withdraw(path, decision, grant)
            raise
    return True


def revoke_grant(decision, grant, *, wait=HOOK_LOCK_SECONDS):
    """Hook side: withdraw this event's grant after the hook failed.

    True if it was still recorded. False means it was trimmed, or a run
    already used or retired it after activation, i.e. after every hook check.
    Raises GrantUncertain if a failed write leaves it possibly active.
    """
    _state, path, lock = _paths(decision)
    with _locked(lock, wait):
        return _withdraw(path, decision, grant)


def observe_grants(decision):
    """Worker side, before the sync scan: ids of both phases this run may use.

    Replacement is atomic, so no lock is needed to read. Under the lock the
    run uses only those still active then: a provisional id becomes usable
    once its hook passed, a revoked one never. An id recorded after this
    point waits for the wake its hook requests after activation.
    """
    try:
        _state, grants, _lock = _paths(decision)
        active, waiting = _read_grants(grants, decision)
        return {'grants': active, 'provisional': waiting}
    except DiscoveryBlocked as exc:
        return {'grants': None, 'reason': str(exc)}


def _state_doc(decision, base, raw_snapshot, pending, spent=()):
    scope, incarnation, _queue, roots, raw = _binding(decision)
    return {'schema': STATE_SCHEMA, 'scopeId': scope, 'dbIncarnation': incarnation,
            'collectionRoots': roots, 'rawCollections': raw,
            'baseGen': base, 'rawSnapshot': dict(sorted(raw_snapshot.items())),
            'pending': dict(sorted(pending.items())),
            'spentGrants': sorted(spent)}


def _raw_path(path, raw):
    if not isinstance(path, str) or '/' not in path:
        return False
    name, rel = path.split('/', 1)
    return name in raw and all(part not in ('', '.', '..') for part in rel.split('/'))


def _validate_state(value, decision):
    scope, incarnation, _queue, roots, raw = _binding(decision)
    # A cursor written before spent ids existed has none.
    if (set(value) not in (_STATE_KEYS, _STATE_KEYS - {'spentGrants'})
            or value['schema'] != STATE_SCHEMA):
        raise DiscoveryBlocked('state_corrupt')
    if (value['scopeId'], value['dbIncarnation']) != (scope, incarnation):
        raise DiscoveryBlocked('state_binding_mismatch')
    if value['collectionRoots'] != roots or value['rawCollections'] != raw:
        raise DiscoveryBlocked('state_roots_changed')
    pending = value['pending']
    snapshot = value['rawSnapshot']
    if (not _grant_ids(value.get('spentGrants', []), len(GRANT_KINDS) * MAX_GRANT_COUNT)
            or not isinstance(value['baseGen'], str) or not _GENERATION.fullmatch(value['baseGen'])
            or not isinstance(snapshot, dict) or len(snapshot) > MAX_MEMBERS
            or any(not _raw_path(path, raw) or not isinstance(sha, str)
                   or not _HEX64.fullmatch(sha) for path, sha in snapshot.items())
            or not isinstance(pending, dict) or len(pending) > MAX_PENDING
            or any(not _raw_path(path, raw) or not isinstance(sha, str)
                   or not _HEX64.fullmatch(sha) for path, sha in pending.items())):
        raise DiscoveryBlocked('state_corrupt')


def _members(db, scope, gen):
    from laya_index_a import _digest
    row = db.execute('SELECT scope,state,digest FROM generations WHERE gen=?', (gen,)).fetchone()
    if row is None or row[0] != scope or row[1] != 'published':
        raise DiscoveryBlocked('generation_unproved')
    count = db.execute('SELECT COUNT(*) FROM membership WHERE gen=?', (gen,)).fetchone()[0]
    if count > MAX_MEMBERS:
        raise DiscoveryBlocked('membership_over_cap')
    rows = db.execute('SELECT path,hash,eligible FROM membership WHERE gen=? AND scope=?',
                      (gen, scope)).fetchall()
    if len(rows) != count or _digest(scope, rows) != row[2]:
        raise DiscoveryBlocked('membership_digest')
    return rows


def _lineage_root(db, scope, head, steps=None):
    """Earliest published ancestor of head along sealed `base_gen` links.

    ``steps`` stops after that many links and returns the generation reached
    (still fully checked); None walks to the baseless first generation.

    Stage binds `base_gen` to the head it replaced and publish CASes on it,
    so the chain is the scope's publication order and ends at a baseless
    first generation. Only seal and generation rows are read, never
    membership. A missing seal, missing or unpublished or foreign ancestor,
    a cycle or more than MAX_LINEAGE generations fails closed.
    """
    gen, seen = head, set()
    while True:
        if gen in seen:
            raise DiscoveryBlocked('lineage_cycle')
        if steps is None and len(seen) >= MAX_LINEAGE:
            # A step limit is its own bound; MAX_LINEAGE caps a full walk.
            raise DiscoveryBlocked('lineage_over_cap')
        seen.add(gen)
        row = db.execute('''SELECT g.scope,g.state,s.scope,s.base_gen FROM generations g
                            LEFT JOIN candidate_seals s ON s.gen=g.gen WHERE g.gen=?''',
                         (gen,)).fetchone()
        if gen == head and (row is None or row[2] is None):
            raise DiscoveryBlocked('head_base_unproved')
        if row is None or row[0] != scope or row[1] != 'published' or row[2] != scope:
            raise DiscoveryBlocked('lineage_broken')
        if row[3] is None or (steps is not None and len(seen) > steps):
            return gen
        if not isinstance(row[3], str) or not _GENERATION.fullmatch(row[3]):
            raise DiscoveryBlocked('lineage_broken')
        gen = row[3]


def _read_view(decision, base):
    """Prove the selected wiki generation and its sealed lineage in one read.

    The source change cursor is separate from membership. A synthetic route
    without a cursor checks one sealed predecessor for structural continuity;
    this generation is not used as a raw source baseline.
    """
    from laya_index_a import LayaIndexA
    scope, incarnation, _queue, _roots, _raw = _binding(decision)
    index = LayaIndexA(decision['dbPath'])
    try:
        if index.incarnation() != incarnation:
            raise DiscoveryBlocked('db_incarnation_changed')
        db = index.db
        db.execute('BEGIN')
        try:
            index._check_header(db)
            row = db.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
            if row is None:
                raise DiscoveryBlocked('head_missing')
            head = row[0]
            if base is None:
                base = _lineage_root(db, scope, head, FIRST_RUN_LINEAGE_STEPS)
            if not isinstance(head, str) or not _GENERATION.fullmatch(head) \
                    or not isinstance(base, str) or not _GENERATION.fullmatch(base):
                raise DiscoveryBlocked('generation_unproved')
            head_rows = _members(db, scope, head)
            base_rows = head_rows if base == head else _members(db, scope, base)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        index.close()
    return head, base, base_rows, head_rows


class _PendingOverflow(Exception):
    def __init__(self, count):
        super().__init__(count)
        self.count = count


def _raw_snapshot(decision, raw):
    """Scan bounded raw roots without adding raw bodies to Laya membership.

    Only names and hashes leave this function. The source port checks every
    path without following symlinks and refuses an unstable file. Its bounded
    200-file/5-MB scan is off-hook, like the existing origin source proof.
    """
    from laya_index_a import IndexContractError, LayaIndexA
    from laya_index_source_port import scan_collections
    index = LayaIndexA(decision['dbPath'])
    try:
        if index.incarnation() != decision['dbIncarnation']:
            raise DiscoveryBlocked('db_incarnation_changed')
        roots = {name: decision['collectionRoots'][name] for name in raw}
        scan = scan_collections(roots, decision['scopeId'], index)
        return {file.path: file.sha256 for file in scan.files if file.eligible}
    except IndexContractError as exc:
        raise DiscoveryBlocked('raw_source_stale') from exc
    finally:
        index.close()


def seed_source_snapshot(decision, baseline):
    """Pre-activation baseline for an exact, still-inactive v2 candidate.

    Cutover calls this before publishing its route and rechecks source proof
    afterward. A failed activation retains the inert snapshot; another
    activation with changed source must re-plan instead of silently reseeding.
    """
    state_path, _grants, lock = _paths(decision)
    _scope, _inc, _queue, _roots, raw = _binding(decision)
    if (not isinstance(baseline, dict) or len(baseline) > MAX_MEMBERS
            or any(not _raw_path(path, raw) or not isinstance(sha, str)
                   or not _HEX64.fullmatch(sha) for path, sha in baseline.items())):
        raise DiscoveryBlocked('baseline_invalid')
    with _locked(lock, WORKER_LOCK_SECONDS):
        # A background origin plan can accept more raw files than the bounded
        # discovery scan. Refuse *before* route publication rather than select
        # a candidate whose source cursor can never advance. The QMD inventory
        # may lag the source files: retain that baseline so the next selected
        # sync discovers the intervening revisions instead of hiding them.
        _raw_snapshot(decision, raw)
        existing = _read(state_path)
        if existing is not None:
            _validate_state(existing, decision)
            if existing['rawSnapshot'] != baseline:
                raise DiscoveryBlocked('baseline_changed')
            return existing['rawSnapshot']
        head, _base, _base_rows, _head_rows = _read_view(decision, None)
        _write(state_path, _state_doc(decision, head, baseline, {}))
        return baseline


def _changed_files(pending, roots):
    """The changedFiles shape enqueue_changed_sources already consumes."""
    files = {}
    for path, sha in sorted(pending.items()):
        name, rel = path.split('/', 1)
        entry = files.setdefault(name, {'root': roots[name], 'created': [], 'updated': [],
                                        'deleted': [], 'revisions': {}})
        entry['updated'].append(rel)
        entry['revisions'][rel] = sha
    return files


def _project_config(decision):
    """Current settings, accepted only while they still name the bound route."""
    import config as qmd_config
    cfg = qmd_config.load_project_config(decision['projectRoot'])
    roles = qmd_config.role_map(cfg)
    if (cfg.get('indexing') is not True
            or sorted(cfg.get('collections') or []) != sorted(decision['collectionRoots'])
            or any(roles.get(item['name']) != item['role'] for item in decision['collections'])):
        raise DiscoveryBlocked('settings_changed')
    return cfg


def _outcome(state, reason=None, **extra):
    return {'state': state, 'reason': reason, **extra}


def drain_after_sync(decision, observed, *, recheck):
    """Discover, journal and enqueue changed raw revisions; bounded, one pass.

    `recheck` re-proves the selected route and raises on change. Outcomes:
    drained (nothing pending), pending (no accepted grant yet, or capped),
    blocked (fail closed; no cursor write, the observed active grants are retired
    with the wake) and retry (transient; keep the wake). Unexpected errors
    propagate, so the runtime dispatcher keeps the wake and its grants.
    """
    phases = ((observed.get('grants'), observed.get('provisional'))
              if isinstance(observed, dict) else ())
    seen_ids = {grant for seen in phases if isinstance(seen, dict)
                for kind in GRANT_KINDS for grant in seen.get(kind) or ()
                if isinstance(grant, str)}
    try:
        state_path, grants_path, lock = _paths(decision)
        _scope, _inc, _queue, roots, raw = _binding(decision)
        with _locked(lock, WORKER_LOCK_SECONDS):
            try:
                return _drain_locked(decision, seen_ids, recheck, state_path,
                                     grants_path, roots, raw)
            except DiscoveryBlocked:
                # The dispatcher ACKs a blocked run's wake, so the grants it
                # observed end here too; a later wake must not inherit them.
                # A provisional one is not yet accepted and has its own wake.
                try:
                    _drop(grants_path, decision, seen_ids, provisional=False)
                except (DiscoveryBlocked, OSError):
                    pass
                raise
    except DiscoveryBusy as exc:
        return _outcome('retry', str(exc))
    except DiscoveryBlocked as exc:
        return _outcome('blocked', str(exc))


def _drain_locked(decision, seen_ids, recheck, state_path, grants_path, roots, raw):
    # Whether this run starts the cursor; a blocked run raises and writes none.
    starting = not os.path.lexists(state_path)
    result = _drain_once(decision, seen_ids, recheck, state_path, grants_path, roots, raw)
    if starting:
        result['cursorStarted'] = True
    return result


def _drain_once(decision, seen_ids, recheck, state_path, grants_path, roots, raw):
    recheck()
    state = _read(state_path)
    if state is None and os.path.isfile(decision['journalPath']):
        # A real selected route is seeded before activation. Losing that
        # cursor must not silently declare existing source edits as baseline.
        raise DiscoveryBlocked('snapshot_missing')
    if state is not None:
        _validate_state(state, decision)
    spent = set(state.get('spentGrants', ())) if state is not None else set()
    # An ACK may have landed without its grant removal: finish that first.
    _drop(grants_path, decision, spent, provisional=False)
    live_ids, _waiting = _read_grants(grants_path, decision)
    # Usable: observed before the scan (either phase) and active now, i.e.
    # every hook check passed, not revoked and not spent by an earlier ACK.
    usable = {kind: [grant for grant in live_ids[kind] if grant in seen_ids]
              for kind in GRANT_KINDS}
    live = [kind for kind in GRANT_KINDS if usable[kind]]
    used = {grant for kind in live for grant in usable[kind]}
    head, base, _base_rows, _head_rows = _read_view(
        decision, state['baseGen'] if state is not None else None)
    snapshot = _raw_snapshot(decision, raw)
    before = state['pending'] if state is not None else {}
    try:
        if state is None:
            # A route created without pre-activation seeding cannot prove an
            # earlier raw snapshot; report cursorStarted and baseline here.
            pending = {}
        else:
            old = state['rawSnapshot']
            pending = {path: sha for path, sha in snapshot.items()
                       if path in before or old.get(path) != sha}
            if len(pending) > MAX_PENDING:
                raise _PendingOverflow(len(pending))
        overflow = None
    except _PendingOverflow as exc:
        pending, overflow = None, exc.count
    if pending is None:
        # Over the cap: skip the burst instead of blocking forever. Those raw
        # revisions are not compiled (no backfill); cards built from older
        # bytes fail the freshness check and are not served. Spend the
        # observed grants with the cursor, as a normal ACK does.
        _write(state_path, _state_doc(decision, head, snapshot, {}, used))
        _drop(grants_path, decision, used, provisional=False)
        return _outcome('drained', 'pending_over_cap_skipped', generation=head,
                        pending=0, skipped=overflow)
    if (state is None or base != head or pending != before
            or state['rawSnapshot'] != snapshot):
        # Journal first: a crash after this keeps every revision.
        _write(state_path, _state_doc(decision, head, snapshot, pending))
    if not pending:
        _drop(grants_path, decision, used, provisional=False)
        return _outcome('drained', generation=head, pending=0)
    if not live:
        return _outcome('pending', 'no_accepted_grant', generation=head,
                        pending=len(pending))
    cfg = _project_config(decision)
    import wiki_compile_enqueue
    for kind in live:
        recheck()
        result = wiki_compile_enqueue.enqueue_changed_sources(
            decision['projectRoot'], cfg, _changed_files(pending, roots),
            session_start=kind == 'session_start')
        if not isinstance(result, dict) or not isinstance(result.get('deferredFiles'), list):
            raise DiscoveryBlocked('enqueue_result_invalid')
        if result.get('reason') == 'compile_disabled':
            continue  # denied for this trigger only: keep revisions
        if result.get('reason') not in ('queued', 'capped', 'no_sources'):
            raise DiscoveryBlocked('enqueue_' + str(result.get('reason')))
        deferred = {'%s/%s' % tuple(item) for item in result['deferredFiles']}
        pending = {path: sha for path, sha in pending.items() if path in deferred}
        # ACK and spend in one replace. One enqueue per run is the per-wake
        # cap; it uses every observed grant, so the deferred tail waits for a
        # new one even if the removal below fails and the wake is retried.
        _write(state_path, _state_doc(decision, head, snapshot, pending, used))
        _drop(grants_path, decision, used, provisional=False)
        return _outcome('pending' if pending else 'drained',
                        'capped' if pending else None, generation=head,
                        pending=len(pending), queued=result.get('queued', 0))
    _drop(grants_path, decision, used, provisional=False)
    return _outcome('pending', 'compile_trigger_denied', generation=head,
                    pending=len(pending))
