"""Read-only selection of the plugin's single QMD database generation.

The managed QMD executable is a separate concern.  An installed executable
never authorizes falling back to the user's ordinary ~/.cache/qmd database.
Existing project shadow generations remain untouched until an explicit,
reviewed migration selects this plugin-wide store.
"""
from __future__ import annotations

import hashlib
import fcntl
import install_deadline
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import subprocess
import time
import uuid


SCHEMA = 'qmd-plugin-shared-store-v1'
LEGACY_ENROLL_JOURNAL = 'legacy-enrollment-journal.json'
LEGACY_ENROLL_GRANT = 'legacy-enrollment-grant.json'


def _legacy_enrollment(base, selected):
    """Read the separate grant for one old, unscoped selected generation.

    A prepared/publishing intent is a *deny* for its new owner, even if its
    metadata reservation survived a crash. It never upgrades the old
    prepared.json or purports to prove the old embedding model.
    """
    path = base / LEGACY_ENROLL_JOURNAL
    if not path.exists() and not path.is_symlink():
        return None, None
    file = _private_file(path)
    if file.stat().st_size > 65536:
        raise ValueError('legacy_enrollment_journal_invalid')
    journal = json.loads(file.read_text())
    if (not isinstance(journal, dict) or journal.get('schema') != 'qmd-legacy-enrollment-v1'
            or journal.get('phase') not in ('prepared', 'publishing', 'completed')
            or not isinstance(journal.get('request'), dict)
            or not isinstance(journal['request'].get('project'), str)
            or type(journal.get('backupVerified')) is not bool
            or not isinstance(journal.get('backupPath'), str)
            or not isinstance(journal.get('requestSha256'), str)
            or journal['requestSha256'] != hashlib.sha256(json.dumps(
                journal['request'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()):
        raise ValueError('legacy_enrollment_journal_invalid')
    request = journal['request']
    if journal['phase']=='completed' and journal['backupVerified'] is not True:
        raise ValueError('legacy_enrollment_journal_invalid')
    backup=base/('legacy-enrollment-backup-'+journal['requestSha256'][:24]+'.sqlite')
    if journal['backupPath']!=str(backup):
        raise ValueError('legacy_enrollment_journal_invalid')
    if journal['backupVerified']:
        if _private_file(backup).stat().st_size==0:
            raise ValueError('legacy_enrollment_backup_invalid')
    pointer = _private_file(base / 'active.json')
    info = pointer.stat()
    legacy=request.get('legacySelected')
    pinned = legacy.get('pointer', {}) if isinstance(legacy,dict) else {}
    if (request.get('generation') != selected['generation']
            or request.get('pointerSha256') != hashlib.sha256(pointer.read_bytes()).hexdigest()
            or [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            != [pinned.get(key) for key in ('dev','ino','bytes','mtimeNs','ctimeNs')]):
        raise ValueError('legacy_enrollment_selected_store_changed')
    grant_path = base / LEGACY_ENROLL_GRANT
    if journal['phase'] != 'completed':
        return request['project'], None
    grant_file = _private_file(grant_path)
    if grant_file.stat().st_size > 65536:
        raise ValueError('legacy_enrollment_grant_invalid')
    grant = json.loads(grant_file.read_text())
    if (not isinstance(grant, dict) or set(grant) != {'schema', 'requestSha256'}
            or grant['schema'] != 'qmd-legacy-enrollment-grant-v1'
            or grant['requestSha256'] != journal['requestSha256']):
        raise ValueError('legacy_enrollment_grant_invalid')
    return request['project'], request

def _scope_policy(generation,prepared):
    sha=prepared.get('scopePolicySha256')
    if sha is None:return None  # Existing reviewed legacy store format.
    file=_private_file(generation/'scope-policy.json')
    if file.stat().st_size>65536 or hashlib.sha256(file.read_bytes()).hexdigest()!=sha:
        raise ValueError('plugin_qmd_scope_policy_changed')
    value=json.loads(file.read_text())
    if (not isinstance(value,dict) or set(value)!={'schema','projects'} or value['schema']!='qmd-shared-scope-v1'
        or not isinstance(value['projects'],dict) or not value['projects'] or any(
            not isinstance(p,str) or not Path(p).is_absolute() or str(Path(p).resolve())!=p
            or not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for p,h in value['projects'].items())):
        raise ValueError('plugin_qmd_scope_policy_changed')
    return value['projects']

def _assert_activated(selected,home=None):
    journal=json.loads(_private_file(_unlinked_root(home)/'bootstrap-journal.json').read_text())
    if journal.get('phase')!='activated' or journal.get('generation')!=selected['generation']:
        raise ValueError('project_shared_store_transition_pending')


def check_project(scope,selected=None,*,home=None):
    selected=selected or select(home)
    approved=selected.get('approvedProjects')
    if approved is None:
        # Old selected stores predate scope-policy.json. Preserve their
        # *registered* owners, but never let a freshly published settings.json
        # turn into permission to add a new collection to the shared DB.
        # Registration for a new owner now needs a separately reviewed
        # transition. This check precedes register_config and worker writes.
        import config as qmd_config
        owner=qmd_config.find_project_config(str(scope))['projectRoot']
        base=_unlinked_root(home)
        pending, grant=_legacy_enrollment(base, selected)
        if owner == pending and grant is None:
            raise ValueError('project_shared_migration_not_approved')
        registry=_registry(base)
        existing={name: row for name,row in registry.items()
                  if row['scope']==owner}
        if not existing:
            raise ValueError('project_shared_migration_not_approved')
        wanted=_collection_owners(owner,qmd_config.find_project_config(owner)['config'])
        if wanted!=existing:
            raise ValueError('project_shared_migration_not_approved')
        if owner == pending:
            import install_update
            settings=Path(owner)/'.auto-context/settings.json'
            if (install_update.file_sha(settings) != grant.get('settingsSha256')
                    or sorted(wanted) != grant.get('collectionNames')):
                raise ValueError('project_shared_migration_not_approved')
        return
    _assert_activated(selected,home)
    import config as qmd_config
    owner=qmd_config.find_project_config(str(scope))['projectRoot']
    if owner not in approved:raise ValueError('project_shared_migration_not_approved')


def approved_owner_registry(home=None):
    """Read-only (approvedProjects, registry) snapshot for change-owner routing.

    Only a scoped, activated store carries the approval set that can name an
    owner other than the event's own project. A legacy store has none, so it
    never authorizes routing; a damaged registry raises like any other read.
    """
    selected=select(home)
    approved=selected.get('approvedProjects')
    if approved is None:
        project, grant=_legacy_enrollment(_unlinked_root(home),selected)
        if grant is None:
            raise ValueError('plugin_qmd_legacy_store_unscoped')
        registry=_registry(_unlinked_root(home))
        return {project:grant['settingsSha256']}, {
            name:row for name,row in registry.items() if row['scope']==project}
    _assert_activated(selected,home)
    return dict(approved),_registry(_unlinked_root(home))


def approved_owner_revision(home=None):
    """Like approved_owner_registry, plus a registration-lifetime revision.

    The revision binds the selected generation, the whole approval map and the
    exact registry file object (device, inode, ctime, mtime, byte digest) read
    through one descriptor. Every registry mutation is a new file installed by
    rename, so a retire/re-add with identical bytes still changes the revision.
    It is None when no registry file exists (nothing is registered).
    """
    selected=select(home)
    approved=selected.get('approvedProjects')
    if approved is None:
        project, grant=_legacy_enrollment(_unlinked_root(home),selected)
        if grant is None:
            raise ValueError('plugin_qmd_legacy_store_unscoped')
        approved={project:grant['settingsSha256']}
    else:
        _assert_activated(selected,home)
    registry,identity=_registry_with_identity(_unlinked_root(home))
    if selected.get('approvedProjects') is None:
        registry={name:row for name,row in registry.items() if row['scope']==project}
    if identity is None:
        return dict(approved),registry,None
    material={'generation':selected['generation'],'approved':sorted(approved.items()),
              'registry':identity}
    revision=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'))
                            .encode('utf8')).hexdigest()
    return dict(approved),registry,revision


def _enrolled_projects(generation, prepared):
    """Owner-approved additions belong only to this sealed store generation."""
    path = generation / 'enrolled-projects.json'
    if not path.exists() and not path.is_symlink():
        return {}
    file = _private_file(path)
    if file.stat().st_size > 1048576:
        raise ValueError('plugin_qmd_scope_enrollment_invalid')
    value = json.loads(file.read_text())
    if (not isinstance(value, dict) or set(value) != {'schema', 'generation', 'scopePolicySha256', 'projects'}
            or value['schema'] != 'qmd-shared-enrollment-v1'
            or value['generation'] != str(generation)
            or value['scopePolicySha256'] != prepared.get('scopePolicySha256')
            or not isinstance(value['projects'], dict)):
        raise ValueError('plugin_qmd_scope_enrollment_invalid')
    for project, row in value['projects'].items():
        if (not isinstance(project, str) or not Path(project).is_absolute()
                or str(Path(project).resolve()) != project or not isinstance(row, dict)
                or set(row) != {'settingsSha256', 'queued'}
                or not isinstance(row['settingsSha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', row['settingsSha256'])
                or type(row['queued']) is not bool):
            raise ValueError('plugin_qmd_scope_enrollment_invalid')
    return value['projects']


def root(home=None):
    return Path(home or Path.home()) / 'Library/Application Support/qmd-auto-context/shared-qmd'


def _unlinked_root(home=None):
    base = root(home)
    home_path = Path(home or Path.home())
    for part in (base, *base.parents):
        if part.is_symlink():
            raise ValueError('unsafe_plugin_qmd_store')
        if part == home_path:
            break
    return base


def _private_dir(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('plugin_qmd_store_not_ready')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_plugin_qmd_store')
    return path


def _private_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('plugin_qmd_store_not_ready')
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_nlink != 1):
        raise ValueError('unsafe_plugin_qmd_store')
    return path


def _registered_config_extension(base, index, raw, baseline_sha):
    """Accept only QMD's added, registered collections over the staged config.

    QMD ``collection add`` rewrites ``index.yml`` before the next routed
    ``update``. The original proof still pins every byte outside the
    collection block; the registry pins each added name and absolute path.
    Unknown YAML shapes fail closed rather than blessing a changed model.
    """
    try:
        value = raw.decode('utf8')
    except UnicodeDecodeError:
        return False
    lines = value.splitlines(keepends=True)
    if not lines or lines[0] != 'collections:\n':
        return False
    boundary = next((i for i, line in enumerate(lines[1:], 1)
                     if line and not line.startswith(' ')), None)
    if boundary is None or boundary == 1:
        return False
    baseline = ('collections: {}\n' + ''.join(lines[boundary:])).encode('utf8')
    if hashlib.sha256(baseline).hexdigest() != baseline_sha:
        return False
    registry = _registry(base)
    section = lines[1:boundary]
    if len(section) % 3 or not registry:
        return False

    def scalar(raw_value):
        raw_value = raw_value.rstrip('\n')
        if raw_value.startswith('"'):
            try:
                parsed = json.loads(raw_value)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, str) else None
        if raw_value.startswith("'"):
            return (raw_value[1:-1].replace("''", "'")
                    if len(raw_value) >= 2 and raw_value.endswith("'") else None)
        if (not raw_value or raw_value.strip() != raw_value
                or '#' in raw_value or ': ' in raw_value
                or raw_value.startswith(('{', '[', '&', '*', '!', '|', '>', '%', '@', '`'))):
            return None
        return raw_value

    seen = {}
    for offset in range(0, len(section), 3):
        heading, path_line, pattern_line = section[offset:offset + 3]
        match = re.fullmatch(r'  ([A-Za-z0-9][A-Za-z0-9._-]{0,127}):\n', heading)
        if (not match or not path_line.startswith('    path: ')
                or not pattern_line.startswith('    pattern: ')):
            return False
        name = match.group(1)
        owner = registry.get(name)
        path = scalar(path_line[len('    path: '):])
        pattern = scalar(pattern_line[len('    pattern: '):])
        if (name in seen or not owner or owner['indexed'] is not True
                or path != owner['path'] or pattern != '**/*.md'):
            return False
        seen[name] = (path, pattern)
    # A registry entry permits a future add; it cannot prove that an existing
    # indexed collection was retained. QMD writes this table during add, so
    # compare the complete physical set before accepting a changed config.
    import sqlite_read
    try:
        with sqlite_read.connect(index) as db:
            rows = db.execute('SELECT name,path,pattern FROM store_collections').fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return False
    return bool(seen) and len(rows) == len(seen) and all(
        seen.get(name) == (path, pattern) for name, path, pattern in rows)


def select(home=None):
    """Return a proved, common DB/config/cache tuple; never create files."""
    base = _private_dir(_unlinked_root(home))
    pointer = _private_file(base / 'active.json')
    if pointer.stat().st_size > 65536:
        raise ValueError('plugin_qmd_store_pointer_invalid')
    value = json.loads(pointer.read_text())
    if (not isinstance(value, dict) or set(value) != {'schema', 'generation'}
            or value['schema'] != SCHEMA or not isinstance(value['generation'], str)
            or not re.fullmatch(r'g-[0-9a-f]{32}', value['generation'])):
        raise ValueError('plugin_qmd_store_pointer_invalid')
    generations = _private_dir(base / 'generations')
    generation = _private_dir(generations / value['generation'])
    index = _private_file(generation / 'index.sqlite')
    config = _private_dir(generation / 'config')
    config_file = _private_file(config / 'index.yml')
    cache = _private_dir(generation / 'cache')
    proof = _private_file(generation / 'prepared.json')
    if proof.stat().st_size > 65536:
        raise ValueError('plugin_qmd_store_proof_invalid')
    prepared = json.loads(proof.read_text())
    fields={'schema','generation','configSha256','indexSchemaSha256'}
    if (not isinstance(prepared, dict) or set(prepared) not in
            (fields,fields|{'scopePolicySha256'})
            or prepared['schema'] != SCHEMA
            or prepared['generation'] != value['generation']):
        raise ValueError('plugin_qmd_store_proof_invalid')
    approved=_scope_policy(generation,prepared)
    config_bytes = config_file.read_bytes()
    if (prepared['configSha256'] != hashlib.sha256(config_bytes).hexdigest()
            and not _registered_config_extension(base, index, config_bytes,
                                                 prepared['configSha256'])):
        raise ValueError('plugin_qmd_store_proof_invalid')
    import runtime_update
    inspected = runtime_update.inspect_index(index, include_file_hash=False)
    if (inspected.get('status') != 'readable'
            or inspected.get('schemaSha256') != prepared['indexSchemaSha256']):
        raise ValueError('plugin_qmd_store_index_changed')
    result={'INDEX_PATH': str(index), 'QMD_CONFIG_DIR': str(config),
            'XDG_CACHE_HOME': str(cache), 'generation': str(generation),
            'selected': True, 'shared': True}
    if approved is not None:
        additions = _enrolled_projects(generation, prepared)
        if set(additions) & set(approved):
            raise ValueError('plugin_qmd_scope_enrollment_invalid')
        result['approvedProjects'] = {**approved, **{p: row['settingsSha256'] for p, row in additions.items()}}
    return result


def _registry(base):
    path = base / 'collections.json'
    if not path.exists():
        return {}
    file = _private_file(path)
    if file.stat().st_size > 1048576:
        raise ValueError('plugin_qmd_registry_invalid')
    return _valid_registry(json.loads(file.read_text()))


def _registry_with_identity(base):
    """(registry, identity) from one descriptor, or ({}, None) when absent."""
    import stat as stat_mode
    try:
        fd = os.open(base / 'collections.json', os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}, None
    try:
        info = os.fstat(fd)
        if (not stat_mode.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe_plugin_qmd_store')
        if info.st_size > 1048576:
            raise ValueError('plugin_qmd_registry_invalid')
        chunks, remaining = [], 1048577
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk); remaining -= len(chunk)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        if (len(raw) != info.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError('plugin_qmd_registry_changed')
    finally:
        os.close(fd)
    value = _valid_registry(json.loads(raw.decode('utf8')))
    return value, [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns,
                   hashlib.sha256(raw).hexdigest()]


def _valid_registry(value):
    if not isinstance(value, dict) or any(
            not isinstance(name, str) or not isinstance(owner, dict)
            or set(owner) != {'scope', 'path', 'indexed'}
            or not all(isinstance(owner[key], str) for key in ('scope', 'path'))
            or type(owner['indexed']) is not bool
            for name, owner in value.items()):
        raise ValueError('plugin_qmd_registry_invalid')
    return value


def register_config(scope, config, *, home=None):
    """Reserve all indexed names atomically before any QMD collection write."""
    selected = select(home)
    base = _unlinked_root(home)
    scope = Path(scope).resolve(strict=True)
    check_project(scope,selected,home=home)
    wanted=_collection_owners(scope, config)
    if selected.get('approvedProjects') is None:
        pending,grant=_legacy_enrollment(base,selected)
        if str(scope)!=pending:
            existing=_registry(base)
            if any(existing.get(name)!=owner for name,owner in wanted.items()):
                raise ValueError('project_shared_migration_not_approved')
        elif grant is None:
            raise ValueError('project_shared_migration_not_approved')
    _reserve_owners(base, wanted)
    return selected


def _collection_owners(scope, config):
    """The same strict name/path validation for workers and approved enrollment."""
    scope = Path(scope).resolve(strict=True)
    import resolve_paths
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    if resolved.get('refused'):
        raise ValueError('plugin_qmd_collection_scope_invalid')
    wanted = {}
    indexed = {entry['name'] for entry in resolved.get('indexEntries', [])}
    # Reserve source-role names too: update.sh may unregister a former indexed
    # collection with that name. It must never remove another project's row.
    for entry in resolved.get('entries', []):
        name, relative = entry['name'], entry['path']
        if (not isinstance(relative, str) or not isinstance(name, str)
                or not name or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name)
                or not resolve_paths.safe_collection_path(
                    scope, relative, resolve_paths.allowed_roots(config))):
            raise ValueError('plugin_qmd_collection_scope_invalid')
        path = (scope / relative).resolve(strict=True)
        if not path.is_dir() or path.is_symlink():
            raise ValueError('plugin_qmd_collection_scope_invalid')
        wanted[name] = {'scope': str(scope), 'path': str(path),
                        'indexed': name in indexed}
    return wanted


def _reserve_owners(base, wanted):
    """Reserve names before opening a newly approved project's route.

    A crash leaves metadata reservations for an identical approved retry;
    never reassign another owner's name or write a QMD collection here.
    """
    lock = base / 'collections.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_plugin_qmd_registry')
        install_deadline.flock(fd, fcntl.LOCK_EX)
        current = _registry(base)
        for name, owner in wanted.items():
            if name not in current or current[name] == owner:
                continue
            # A path move needs an explicit index migration. Reassigning the
            # registry first would let an old qmd://name/relative hit resolve
            # against an unindexed file at the new path.
            if current[name].get('scope') == owner['scope']:
                raise ValueError('plugin_qmd_collection_path_change_requires_migration')
            raise ValueError('plugin_qmd_collection_collision')
        # An unchanged retry keeps the same registry file object, so prior-owner
        # observations bound to it stay valid. Any real change still installs a
        # new file by rename and invalidates them (no retire/re-add ABA).
        if wanted and all(current.get(name) == owner for name, owner in wanted.items()):
            return
        current.update(wanted)
        _write_registry(base, current)
    finally:
        os.close(fd)


def _write_registry(base, value):
    """Install a new registry file; caller holds ``collections.lock``.

    Its mtime is strictly after the replaced file's, so a retire/re-add that
    restores identical bytes can never repeat the prior file identity even if
    the filesystem reuses the inode within one coarse timestamp tick.
    """
    path = base / 'collections.json'
    try:
        previous = os.lstat(path).st_mtime_ns
    except FileNotFoundError:
        previous = 0
    temp_fd, temp_name = tempfile.mkstemp(prefix='.collections-', dir=base)
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, 'w') as out:
            json.dump(value, out, sort_keys=True, separators=(',', ':'))
            out.flush(); os.fsync(out.fileno())
        stamp = max(time.time_ns(), previous + 1)
        os.utime(temp_name, ns=(stamp, stamp))
        os.replace(temp_name, path)
        directory = os.open(base, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def verify_missing_collection(scope, name, config, *, home=None):
    """Authorize only this owner's missing-path removal during SessionStart."""
    selected = select(home)
    scope = Path(scope).resolve(strict=True)
    import resolve_paths
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    if resolved.get('refused'):
        raise ValueError('plugin_qmd_collection_scope_invalid')
    matches = [row for row in resolved.get('entries', []) if row['name'] == name]
    if len(matches) != 1:
        raise ValueError('plugin_qmd_collection_scope_invalid')
    relative = matches[0]['path']
    if not resolve_paths.safe_collection_path(
            scope, relative, resolve_paths.allowed_roots(config)):
        raise ValueError('plugin_qmd_collection_scope_invalid')
    path = (scope / relative).resolve(strict=False)
    owner = _registry(_unlinked_root(home)).get(name)
    if (path.exists() or path.is_symlink() or not isinstance(owner, dict)
            or owner.get('scope') != str(scope) or owner.get('path') != str(path)):
        raise ValueError('plugin_qmd_collection_scope_invalid')
    # The name-based QMD remove below must address the same physical path.
    # Check the SQLite row even if index.yml still matches the empty baseline.
    import sqlite_read
    with sqlite_read.connect(Path(selected['INDEX_PATH'])) as db:
        physical = db.execute('SELECT path FROM store_collections WHERE name=?',
                              (name,)).fetchall()
    if physical != [(str(path),)]:
        raise ValueError('plugin_qmd_collection_scope_invalid')


def _discard_unpublished_reservations(scope, *, home=None):
    """Recover only an unapproved owner's metadata with no physical collection.

    Never changes SQLite, a committed approval, or another owner's registry.
    Common setup/writer locks are held by the enrollment caller.
    """
    base = _unlinked_root(home)
    fd = os.open(base/'collections.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_plugin_qmd_registry')
        install_deadline.flock(fd, fcntl.LOCK_EX)
        selected = select(home)
        approved = selected.get('approvedProjects')
        if approved is None or scope in approved:
            return
        current = _registry(base)
        stale = {n for n, row in current.items() if row['scope'] == scope}
        if not stale:
            return
        import sqlite_read
        with sqlite_read.connect(selected['INDEX_PATH']) as db:
            physical = {row[0] for row in db.execute('SELECT name FROM store_collections')}
        if stale & physical:
            raise ValueError('plugin_qmd_unapproved_physical_collection')
        for name in stale:
            del current[name]
        _write_registry(base, current)
    finally:
        os.close(fd)


def retire_pruned(scope, config, *, home=None):
    """Retire only absent, physically removed names after scoped settings prune."""
    selected = select(home)
    scope = Path(scope).resolve(strict=True)
    import resolve_paths
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    current_names = {row['name'] for row in resolved.get('entries', [])}
    base = _unlinked_root(home)
    lock = base / 'collections.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_plugin_qmd_registry')
        install_deadline.flock(fd, fcntl.LOCK_EX)
        registry = _registry(base)
        stale = {name: owner for name, owner in registry.items()
                 if owner['scope'] == str(scope) and name not in current_names}
        if not stale:
            return 0
        import sqlite_read
        with sqlite_read.connect(Path(selected['INDEX_PATH'])) as db:
            physical = {row[0] for row in db.execute('SELECT name FROM store_collections')}
        if any(name in physical or Path(owner['path']).exists()
               or Path(owner['path']).is_symlink() for name, owner in stale.items()):
            raise ValueError('plugin_qmd_prune_not_settled')
        for name in stale:
            del registry[name]
        _write_registry(base, registry)
        return len(stale)
    finally:
        os.close(fd)


def owned_collections(scope, config, *, home=None):
    """Only indexed names pinned to this exact source directory may be queried."""
    base = _unlinked_root(home)
    select(home)
    scope = Path(scope).resolve(strict=True)
    registry = _registry(base)
    import resolve_paths
    resolved = resolve_paths.resolve_paths(str(scope), json.dumps(config))
    if resolved.get('refused'):
        return set()
    names = set()
    for entry in resolved.get('indexEntries', []):
        name, relative = entry['name'], entry['path']
        expected = {'scope': str(scope), 'path': str((scope / relative).resolve()),
                    'indexed': True}
        if registry.get(name) == expected:
            names.add(name)
    return names


def assert_all_enrolled(*, home=None):
    """Block every shared QMD write while any registered owner is withdrawn.

    QMD update/embed act on the entire physical DB, even when invoked from a
    different story. Retain historical rows and registry entries; resolving a
    revoked collection requires a separate, explicit migration decision.
    """
    selected = select(home)
    registry = _registry(_unlinked_root(home))
    approved = selected.get('approvedProjects')
    if approved is not None:
        # Metadata reserved before an approval receipt is not an indexed
        # project. A physical unapproved name is still a hard blocker.
        import sqlite_read
        with sqlite_read.connect(selected['INDEX_PATH']) as db:
            physical = {row[0] for row in db.execute('SELECT name FROM store_collections')}
        if any(n in physical and row['scope'] not in approved for n,row in registry.items()):
            raise ValueError('plugin_qmd_unapproved_physical_collection')
        registry = {n: row for n,row in registry.items() if row['scope'] in approved}
    import config as qmd_config
    import resolve_paths
    scopes = {}
    for name, owner in registry.items():
        scope_value = owner.get('scope')
        path_value = owner.get('path')
        if not isinstance(scope_value, str) or not isinstance(path_value, str):
            raise ValueError('plugin_qmd_revoked_collection_requires_reconciliation')
        scopes.setdefault(scope_value, []).append((name, path_value,
                                                   owner.get('indexed')))
    for scope_value, entries in scopes.items():
        try:
            scope = Path(scope_value).resolve(strict=True)
            found = qmd_config.find_project_config(scope)
            cfg = found['config']
            if (str(scope) != scope_value or found['projectRoot'] != scope_value
                    or cfg.get('indexing') is not True):
                raise ValueError('revoked')
            resolved = resolve_paths.resolve_paths(scope_value, json.dumps(cfg))
            if resolved.get('refused'):
                raise ValueError('revoked')
            current = {row['name']: str((scope / row['path']).resolve(strict=True))
                       for row in resolved.get('entries', [])}
            current_indexed = {row['name'] for row in resolved.get('indexEntries', [])}
            if any(current.get(name) != path or not Path(path).is_dir()
                   or (name in current_indexed) is not was_indexed
                   for name, path, was_indexed in entries):
                raise ValueError('revoked')
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('plugin_qmd_revoked_collection_requires_reconciliation') from None


def stage_empty(config_file, *, home=None):
    """Prepare an inactive, empty plugin DB with the selected local QMD.

    The command is deliberately not called by hooks. Model resolution is
    offline; an unavailable model fails staging rather than downloading one.
    """
    import qmd_route
    import runtime_update
    source = Path(config_file).resolve(strict=True)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 65536:
        raise ValueError('plugin_qmd_config_invalid')
    binary = qmd_route.binary_info()['QMD_BIN']
    base = _unlinked_root(home)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_dir(base)
    generations = base / 'generations'
    generations.mkdir(mode=0o700, exist_ok=True)
    _private_dir(generations)
    generation = generations / ('g-' + uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    config = generation / 'config'; config.mkdir(mode=0o700)
    cache = generation / 'cache'; cache.mkdir(mode=0o700)
    config_target = config / 'index.yml'
    config_target.write_bytes(source.read_bytes()); config_target.chmod(0o600)
    index = generation / 'index.sqlite'
    env = {**os.environ, 'INDEX_PATH': str(index),
           'QMD_CONFIG_DIR': str(config), 'XDG_CACHE_HOME': str(cache),
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    result = subprocess.run([binary, 'update'], env=env,
                            cwd=generation, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=900, check=False)
    if result.returncode:
        raise ValueError('plugin_qmd_empty_stage_failed')
    index.chmod(0o600)
    inspected = runtime_update.inspect_index(index, include_file_hash=False)
    if inspected.get('status') != 'readable' or inspected['activeDocuments'] != 0:
        raise ValueError('plugin_qmd_empty_stage_failed')
    import sqlite_read
    with sqlite_read.connect(index) as db:
        if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
               for table in ('store_collections', 'documents', 'content',
                             'content_vectors')):
            raise ValueError('plugin_qmd_empty_stage_failed')
    prepared = {'schema': SCHEMA, 'generation': generation.name,
                'configSha256': hashlib.sha256(config_target.read_bytes()).hexdigest(),
                'indexSchemaSha256': inspected['schemaSha256']}
    proof = generation / 'prepared.json'
    proof.write_text(json.dumps(prepared, sort_keys=True, separators=(',', ':')))
    proof.chmod(0o600)
    return {'status': 'staged_inactive', 'generation': str(generation),
            'index': str(index)}


def activate_empty(generation, *, home=None):
    """One-time explicit cutover; never overwrite an existing shared pointer."""
    import runtime_update
    import sqlite_read
    base = _private_dir(_unlinked_root(home))
    target = Path(generation)
    if (target.parent != base / 'generations'
            or not re.fullmatch(r'g-[0-9a-f]{32}', target.name)):
        raise ValueError('plugin_qmd_generation_invalid')
    _private_dir(target)
    proof = json.loads(_private_file(target / 'prepared.json').read_text())
    config = _private_file(target / 'config/index.yml')
    index = _private_file(target / 'index.sqlite')
    _private_dir(target / 'cache')
    inspected = runtime_update.inspect_index(index, include_file_hash=False)
    expected={'schema': SCHEMA, 'generation': target.name,
                  'configSha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                  'indexSchemaSha256': inspected.get('schemaSha256')}
    if 'scopePolicySha256' in proof:
        _scope_policy(target,proof);expected['scopePolicySha256']=proof['scopePolicySha256']
    if (proof!=expected or inspected.get('status') != 'readable'
            or inspected['activeDocuments'] != 0):
        raise ValueError('plugin_qmd_generation_invalid')
    with sqlite_read.connect(index) as db:
        if any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
               for table in ('store_collections', 'documents', 'content',
                             'content_vectors')):
            raise ValueError('plugin_qmd_generation_not_empty')
    pointer = base / 'active.json'
    fd,name=tempfile.mkstemp(prefix='.shared-active-',dir=base)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'schema': SCHEMA, 'generation': target.name}, out,
                      sort_keys=True, separators=(',', ':'))
            out.flush(); os.fsync(out.fileno())
        os.link(name,pointer,follow_symlinks=False)  # Atomic, exclusive; never overwrite.
    finally:Path(name).unlink(missing_ok=True)
    directory=os.open(base,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return select(home)
