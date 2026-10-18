"""Stage B1/B2a: route a changed file to its approved indexing owner.

This is file-sync/indexing ownership only. A routed edit enqueues the owner
project's existing dirty-queue line; it never changes the current session's
recall scope, which stays bound to the query payload's cwd (Stage A rejects
stale results separately). Every refusal means "write nothing".

Ownership comes only from the shared store's collections registry plus its
approvedProjects, then is re-proved against the owner's current settings.
No HOME scan, no enrollment, no QMD or model call.

B1: a PRESENT regular file is routed by the current registry.

B2a: a MISSING path (deletion, or the old side of a move) has no current
metadata, so the current registry alone never routes it. It routes only when
an earlier accepted event recorded that this exact symlink-free path was a
regular file of the same approved, indexed, registered collection, and that
observation is still bound to the same registration revision (store
generation, approval map, registry file object) and the same approved
settings digest. The observation ledger is evidence, not an authority: the
current registry and owner settings are re-proved as for a present file.
Each side of a move is resolved independently.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

import config as qmd_config
import qmd_shared_store
import resolve_paths
from collection_match import select_collections

_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error)

LEDGER_NAME = 'owner-observations.json'
LEDGER_LOCK = 'owner-observations.lock'
LEDGER_SCHEMA = 'qmd-owner-observations-v1'
LEDGER_MAX_ENTRIES = 512
LEDGER_MAX_BYTES = 262144
_LOCK_WAIT_SECONDS = 1.0
_ROW_KEYS = {'projectRoot', 'collection', 'collectionPath', 'settingsSha256',
             'revision', 'identity'}


def registry_snapshot(home=None):
    """(approved, registry, revision), or a refusal reason. Legacy never routes."""
    try:
        return qmd_shared_store.approved_owner_revision(home), None
    except _READ_ERRORS as exc:
        if str(exc) == 'plugin_qmd_legacy_store_unscoped':
            return None, 'legacy_store'
        return None, 'store_unavailable'


def _lexical(raw, base):
    if (not isinstance(raw, str) or not raw
            or any(ch in raw for ch in '\x00\t\r\n')):
        return None, 'unsafe_path'
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(base) / candidate
    # `..` would let a symlinked directory vanish from the lexical path.
    if '..' in candidate.parts:
        return None, 'unsafe_path'
    return Path(os.path.abspath(candidate)), None


def present_target(raw, base):
    """A lexically absolute, symlink-free regular file, with its identity."""
    lexical, reason = _lexical(raw, base)
    if reason:
        return None, reason
    try:
        info = os.lstat(lexical)
        real = lexical.resolve(strict=True)
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, RuntimeError):
        return None, 'unresolvable'
    if stat.S_ISLNK(info.st_mode) or real != lexical:
        # A link leaf, or any linked directory on the way: the link and its
        # target may belong to different owners, so neither is proved.
        return None, 'symlink'
    if not stat.S_ISREG(info.st_mode):
        return None, 'not_regular'
    return (lexical, (info.st_dev, info.st_ino)), None


def missing_target(raw, base):
    """A lexically absolute missing path whose surviving ancestors are not links."""
    lexical, reason = _lexical(raw, base)
    if reason:
        return None, reason
    try:
        info = os.lstat(lexical)
    except FileNotFoundError:
        info = None
    except OSError:
        return None, 'unresolvable'
    if info is not None:
        return None, 'symlink' if stat.S_ISLNK(info.st_mode) else 'not_missing'
    ancestor = lexical.parent
    try:
        while True:
            try:
                info = os.lstat(ancestor)
                break
            except FileNotFoundError:
                if ancestor.parent == ancestor:
                    return None, 'unresolvable'
                ancestor = ancestor.parent
        if stat.S_ISLNK(info.st_mode) or ancestor.resolve(strict=True) != ancestor:
            return None, 'symlink'
    except (OSError, RuntimeError):
        return None, 'unresolvable'
    if not stat.S_ISDIR(info.st_mode):
        return None, 'unresolvable'
    return lexical, None


def _claims(target, registry):
    rows = []
    for name, owner in registry.items():
        try:
            target.relative_to(owner['path'])
        except ValueError:
            continue
        rows.append((len(Path(owner['path']).parts), name, owner))
    return rows


def conflicting_owner_claims(target, own_scope, snapshot):
    """True if the cwd claim is disputed by another owner or equal-depth name."""
    approved, registry = snapshot[0], snapshot[1]
    claims = [(depth, name, owner) for depth, name, owner in
              _claims(Path(target), registry) if owner['scope'] in approved]
    if any(owner['scope'] != str(own_scope) for _, _, owner in claims):
        return True
    if claims:
        deepest = max(depth for depth, _, _ in claims)
        if sum(depth == deepest for depth, _, _ in claims) > 1:
            return True
    return False


def _registered_owner(target, approved, registry):
    """(scope, name, owner) for one unambiguous approved indexed claim."""
    try:
        claims = _claims(target, registry)
    except _READ_ERRORS:
        return None, 'store_unavailable'
    if not claims:
        return None, 'unregistered'
    scopes = {owner['scope'] for _, _, owner in claims}
    # Nested projects or worktrees registering the same path need explicit
    # ownership evidence; never guess by the longest path across owners.
    if len(scopes) != 1:
        return None, 'ambiguous_owner'
    scope = scopes.pop()
    if scope not in approved:
        return None, 'not_approved'
    depth = max(row[0] for row in claims)
    deepest = [(name, owner) for d, name, owner in claims if d == depth]
    if len(deepest) != 1:
        return None, 'ambiguous_owner'
    name, owner = deepest[0]
    if owner['indexed'] is not True:
        return None, 'not_indexed'
    return (scope, name, owner), None


def _revalidate(scope, name, owner, target, approved_sha):
    """The TARGET owner's current settings must still prove this exact route."""
    root = Path(scope)
    try:
        if root.is_symlink() or root.resolve(strict=True) != root:
            return None, 'owner_root_changed'
        registered = Path(owner['path'])
        if registered.resolve(strict=True) != registered or not registered.is_dir():
            return None, 'registration_mismatch'
        settings = root / qmd_config.CONFIG_DIR_NAME / qmd_config.SETTINGS_FILE_NAME
        if settings.is_symlink() or not settings.is_file():
            return None, 'owner_config_missing'
        found = qmd_config.find_project_config(str(root))
        config = found['config']
        # A local optout marker resolves to indexing:false here as well.
        if found['configFormat'] == 'local-optout' or config.get('indexing') is not True:
            return None, 'owner_off'
        if found['projectRoot'] != scope or found['configFormat'] != 'auto-context-dir':
            return None, 'owner_config_changed'
        sha = hashlib.sha256(settings.read_bytes()).hexdigest()
        if sha != approved_sha:
            return None, 'stale_approval'
        if not qmd_config.event_enabled(config, 'postToolUse'):
            return None, 'owner_event_disabled'
        if not qmd_config.is_indexed_collection(qmd_config.role_map(config), name):
            return None, 'not_indexed'
        resolved = resolve_paths.resolve_paths(scope, json.dumps(config))
        if resolved.get('refused'):
            return None, 'owner_scope_refused'
        rows = [row for row in resolved.get('indexEntries', []) if row['name'] == name]
        if len(rows) != 1:
            return None, 'registration_mismatch'
        relative = Path(rows[0]['path']).expanduser()
        current = (relative if relative.is_absolute() else root / relative).resolve(strict=True)
        # The owner's own longest-prefix choice must agree with the registry,
        # so a deeper, not yet registered collection is not misattributed.
        if (str(current) != owner['path']
                or select_collections([str(target)], scope, config) != {name: owner['path']}):
            return None, 'registration_mismatch'
        from context_learning.synthetic_scope import invocation_in_generated_story
        if not invocation_in_generated_story(found, str(target.parent), [str(target)]):
            return None, 'generated_story_guard'
    except _READ_ERRORS:
        return None, 'owner_unreadable'
    return sha, None


def resolve_owner(raw, base, *, snapshot=None, home=None):
    """Present file only: ({projectRoot, collection, path, ...}, None) or (None, reason)."""
    present, reason = present_target(raw, base)
    if reason:
        return None, reason
    target, identity = present
    if snapshot is None:
        snapshot, reason = registry_snapshot(home)
        if reason:
            return None, reason
    approved, registry, revision = snapshot
    picked, reason = _registered_owner(target, approved, registry)
    if reason:
        return None, reason
    scope, name, owner = picked
    sha, reason = _revalidate(scope, name, owner, target, approved[scope])
    if reason:
        return None, reason
    return {'projectRoot': scope, 'collection': name, 'path': owner['path'],
            'target': str(target), 'identity': identity, 'settingsSha256': sha,
            'revision': revision, 'change': 'present'}, None


def resolve_missing(raw, base, *, snapshot=None, home=None):
    """B2a: a missing path routes only on a still-bound prior observation."""
    target, reason = missing_target(raw, base)
    if reason:
        return None, reason
    if snapshot is None:
        snapshot, reason = registry_snapshot(home)
        if reason:
            return None, reason
    approved, registry, revision = snapshot
    if revision is None:
        return None, 'unregistered'
    try:
        row = _read_ledger(_ledger_base(home)).get(str(target))
    except _READ_ERRORS:
        return None, 'observation_unavailable'
    if row is None:
        return None, 'no_prior_observation'
    if row['revision'] != revision:
        # Registry rewritten (retire/re-add, collision, prune), generation or
        # approval map changed: the observation describes a different lifetime.
        return None, 'stale_observation'
    picked, reason = _registered_owner(target, approved, registry)
    if reason:
        return None, reason
    scope, name, owner = picked
    if (scope, name, owner['path']) != (row['projectRoot'], row['collection'],
                                         row['collectionPath']):
        return None, 'observation_mismatch'
    if approved[scope] != row['settingsSha256']:
        return None, 'stale_approval'
    sha, reason = _revalidate(scope, name, owner, target, approved[scope])
    if reason:
        return None, reason
    return {'projectRoot': scope, 'collection': name, 'path': owner['path'],
            'target': str(target), 'identity': tuple(row['identity']),
            'settingsSha256': sha, 'revision': revision, 'change': 'missing'}, None


def resolve_change(raw, base, *, snapshot=None, home=None):
    """Present files by the current registry; missing paths only by B2a proof."""
    if snapshot is None:
        snapshot, reason = registry_snapshot(home)
        if reason:
            return None, reason
    owner, reason = resolve_owner(raw, base, snapshot=snapshot, home=home)
    if reason == 'missing':
        return resolve_missing(raw, base, snapshot=snapshot, home=home)
    return owner, reason


def _route(paths, base, resolver, home):
    snapshot, reason = registry_snapshot(home)
    if reason:
        return {}, [(raw, reason) for raw in paths]
    routed, refused = {}, []
    for raw in paths:
        owner, reason = resolver(raw, base, snapshot=snapshot, home=home)
        if reason:
            refused.append((raw, reason))
        else:
            routed.setdefault(owner['projectRoot'], {})[raw] = owner
    return routed, refused


def route_present(paths, base, *, home=None):
    """B1 only: group proved present owners as {projectRoot: {raw: owner}}."""
    return _route(paths, base, resolve_owner, home)


def route_changes(paths, base, *, home=None):
    """B1 + B2a: each path (either side of a move) is proved independently."""
    return _route(paths, base, resolve_change, home)


def enqueue_routed(routed, base, *, home=None):
    """Re-prove each target right before its owner's queue append.

    The dirty queue line keeps its existing format. A target whose identity,
    owner, registration, observation or approved settings changed since
    routing is dropped. A change after this final proof and before the append
    is not excluded. Accepted present files are then observed for B2a.
    """
    import dirty_queue
    written, observed = {}, []
    for scope, owners in sorted(routed.items()):
        selected, present = {}, []
        for raw, first in owners.items():
            again, reason = resolve_change(raw, base, home=home)
            if reason or again != first:
                continue
            selected[first['collection']] = first['path']
            if first['change'] == 'present':
                present.append(first)
        if not selected:
            continue
        try:
            dirty_queue.enqueue_project_collections(scope, selected)
        except _READ_ERRORS:
            continue
        written[scope] = selected
        observed.extend(present)
    if observed:
        try:
            record_observations(observed, home=home)
        except _READ_ERRORS:
            pass
    return written


def observe_present(paths, base, *, snapshot=None, home=None):
    """Record accepted present events (also the session's own) for B2a.

    Only a fully proved present regular file is recorded; missing, symlinked,
    unregistered, unapproved, unindexed or OFF paths are refused by
    resolve_owner and never written. Returns the number of recorded paths.
    """
    if snapshot is None:
        snapshot, reason = registry_snapshot(home)
        if reason:
            return 0
    owners = []
    for raw in paths:
        owner, reason = resolve_owner(raw, base, snapshot=snapshot, home=home)
        if not reason:
            owners.append(owner)
    return record_observations(owners, home=home) if owners else 0


# -- observation ledger (owner-private, bounded, atomic; evidence only) ------
def _ledger_base(home=None):
    return qmd_shared_store._private_dir(qmd_shared_store._unlinked_root(home))


def _valid_row(key, row):
    return (isinstance(key, str) and key and Path(key).is_absolute()
            and os.path.abspath(key) == key and '..' not in Path(key).parts
            and not any(ch in key for ch in '\x00\t\r\n')
            and isinstance(row, dict) and set(row) == _ROW_KEYS
            and all(isinstance(row[k], str) and row[k]
                    for k in ('projectRoot', 'collection', 'collectionPath'))
            and all(isinstance(row[k], str) and re.fullmatch('[0-9a-f]{64}', row[k])
                    for k in ('settingsSha256', 'revision'))
            and isinstance(row['identity'], list) and len(row['identity']) == 2
            and all(type(v) is int and v >= 0 for v in row['identity']))


def _read_ledger(base):
    """Entries in insertion order. Unsafe or damaged evidence raises."""
    try:
        fd = os.open(base / LEDGER_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('observation_ledger_unsafe')
        if info.st_size > LEDGER_MAX_BYTES:
            raise ValueError('observation_ledger_invalid')
        raw = os.read(fd, LEDGER_MAX_BYTES + 1)
        if len(raw) != info.st_size:
            raise ValueError('observation_ledger_invalid')
    finally:
        os.close(fd)
    value = json.loads(raw.decode('utf8'))
    if (not isinstance(value, dict) or set(value) != {'schema', 'entries'}
            or value['schema'] != LEDGER_SCHEMA or not isinstance(value['entries'], dict)
            or len(value['entries']) > LEDGER_MAX_ENTRIES
            or not all(_valid_row(k, r) for k, r in value['entries'].items())):
        raise ValueError('observation_ledger_invalid')
    return value['entries']


def _write_ledger(base, entries):
    while True:
        data = json.dumps({'schema': LEDGER_SCHEMA, 'entries': entries},
                          separators=(',', ':')).encode('utf8')
        if len(data) <= LEDGER_MAX_BYTES or not entries:
            break
        entries.pop(next(iter(entries)))  # Oldest first; losing it only fails closed.
    temp_fd, temp_name = tempfile.mkstemp(prefix='.owner-observations-', dir=base)
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temp_name, base / LEDGER_NAME)
        directory = os.open(base, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _locked_ledger(base):
    fd = os.open(base / LEDGER_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError('observation_ledger_unsafe')
        # A hook never waits long: a busy ledger only means no new evidence.
        until = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise ValueError('observation_ledger_busy')
                time.sleep(.02)
    except BaseException:
        os.close(fd)
        raise


def record_observations(owners, *, home=None):
    """Persist proved present owners; unsafe ledgers are never written through."""
    rows = {}
    for owner in owners:
        if owner.get('change') != 'present' or not owner.get('revision'):
            continue
        try:
            info = os.lstat(owner['target'])
        except OSError:
            continue
        # The file must still be the same regular file that was proved.
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != tuple(owner['identity'])):
            continue
        row = {'projectRoot': owner['projectRoot'], 'collection': owner['collection'],
               'collectionPath': owner['path'], 'settingsSha256': owner['settingsSha256'],
               'revision': owner['revision'], 'identity': [info.st_dev, info.st_ino]}
        if _valid_row(owner['target'], row):
            rows[owner['target']] = row
    if not rows:
        return 0
    base = _ledger_base(home)
    fd = _locked_ledger(base)
    try:
        try:
            entries = _read_ledger(base)
        except ValueError as exc:  # Includes JSON/UTF-8 decode errors.
            if str(exc) == 'observation_ledger_unsafe':
                raise
            entries = {}  # Damaged evidence is dropped, never trusted.
        for key, row in rows.items():
            entries.pop(key, None)
            entries[key] = row
        while len(entries) > LEDGER_MAX_ENTRIES:
            entries.pop(next(iter(entries)))
        _write_ledger(base, entries)
    finally:
        os.close(fd)
    return len(rows)
