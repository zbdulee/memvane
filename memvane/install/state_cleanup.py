"""Preview and approved removal of per-user MemVane state.

Default scope: install state roots no project selects any more (their
``vector-cache/`` goes with them). ``everything`` is the explicit "delete it
all" request: the state, support and cache roots of this user. A project's
``.memvane/`` folder (wiki, pointers, settings) and ``~/.config/memvane`` are
never touched.

The preview writes nothing. A removal holds the install lock and the sync
owner of that install without waiting, classifies it again, renames the
directory aside and deletes the renamed tree; a tree that could not be deleted
stays under its ``.removing-`` name for the next run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

from memvane.install.locks import install_lock
from memvane.install.types import InstallBlocked
from memvane.kernel import memvane_paths, safe_rmtree

INSTALL = re.compile(r'[0-9a-f]{32}\Z')
REMOVING = '.removing-'
POINTERS = 'initial-builds'
RECORD_BYTES = 64 * 1024
SIZE_ENTRIES = 500_000


def _record(path):
    """A bounded private JSON object, None when absent; ValueError when it cannot be trusted."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError('state_record_unreadable') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > RECORD_BYTES:
            raise ValueError('state_record_unreadable')
        value = json.loads(os.read(fd, RECORD_BYTES + 1).decode('utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('state_record_unreadable') from exc
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise ValueError('state_record_unreadable')
    return value


def _own_directory(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def _size(path):
    """``(bytes of regular files, complete)`` without following links."""
    total, seen, stack = 0, 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > SIZE_ENTRIES:
                        return total, False
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def _building(base):
    """State roots an initial-build pointer names; None when a pointer cannot be read."""
    named = set()
    try:
        names = os.listdir(base / POINTERS)
    except FileNotFoundError:
        return named
    except OSError:
        return None
    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            record = _record(base / POINTERS / name)
        except ValueError:
            return None
        if record is not None and isinstance(record.get('stateRoot'), str):
            named.add(record['stateRoot'])
    return named


def _classify(directory, building):
    """``(action, reason, project)`` of one entry of ``installs/``."""
    if not INSTALL.match(directory.name) or not _own_directory(directory):
        return 'keep', 'unrecognized', None
    if building is None:
        return 'keep', 'build_pointer_unreadable', None
    if str(directory) in building:
        return 'keep', 'building', None
    try:
        route = _record(directory / 'route.json')
    except ValueError:
        return 'keep', 'project_unreachable', None
    project = route.get('projectRoot') if route else None
    if (not isinstance(project, str) or not os.path.isabs(project)
            or route.get('stateRoot') != str(directory)):
        return 'keep', 'project_unreachable', None
    # A project on a volume that is not mounted looks the same as a deleted one: keep it.
    if not os.path.isdir(project):
        return 'keep', 'project_unreachable', project
    for name in ('route.json', 'selection.json'):
        try:
            pointer = _record(Path(project) / '.memvane' / name)
        except ValueError:
            return 'keep', 'pointer_unreadable', project
        if pointer is not None and pointer.get('stateRoot') == str(directory):
            return 'keep', 'selected', project
    return 'remove', 'released', project


def _rows(base):
    installs = base / 'installs'
    rows = []
    try:
        names = sorted(os.listdir(installs))
    except FileNotFoundError:
        return rows
    building = _building(base)
    for name in names:
        directory = installs / name
        if name.startswith(REMOVING):
            action, reason, project = 'remove', 'interrupted_removal', None
        else:
            action, reason, project = _classify(directory, building)
        size, complete = _size(directory) if action == 'remove' else (None, True)
        row = {'install': name, 'action': action, 'reason': reason}
        if project is not None:
            row['project'] = project
        if size is not None:
            row['bytes'] = size
            if not complete:
                row['bytesTruncated'] = True
        rows.append(row)
    return rows


def _delete(path):
    """Delete a tree already renamed aside; the reason when something is left.

    ``safe_rmtree`` walks from the parent's descriptor without following links
    and refuses a tree that holds a mount point before removing anything.
    """
    path = Path(path)
    try:
        parent = safe_rmtree.open_root(path.parent, uid=os.getuid())
        try:
            safe_rmtree.rmtree_at(parent, path.name, uid=os.getuid(), path=path)
        finally:
            os.close(parent)
    except safe_rmtree.UnsafeTree as exc:
        return str(exc)
    except OSError as exc:
        return type(exc).__name__
    return None


def _owner(store):
    from memvane.runtime import api as runtime
    return runtime.sync_owner(store, wait=False)


def _remove_install(base, name, owner):
    """Remove one released install under its locks: ``(removed, reason)``."""
    directory = base / 'installs' / name
    aside = directory.with_name(REMOVING + name)
    outcome = None
    try:
        with install_lock(directory):
            store = directory / 'store.sqlite'
            if os.path.lexists(store):
                # The sync owner keeps the arena under vector-cache/: it leaves with the install, under that owner.
                with owner(store) as claim:
                    if claim is None:
                        return False, 'busy'
                    outcome = _rename_and_delete(base, directory, aside)
            else:
                outcome = _rename_and_delete(base, directory, aside)
    except InstallBlocked as exc:
        if outcome is None:
            return False, 'busy' if str(exc) == 'install_busy' else str(exc)
    except Exception as exc:
        # An owner that finds its store gone on release does not undo the removal it covered.
        if outcome is None:
            return False, type(exc).__name__
    return outcome


def _rename_and_delete(base, directory, aside):
    if _classify(directory, _building(base)) != ('remove', 'released', _project(directory)):
        return False, 'changed'
    os.rename(directory, aside)
    reason = _delete(aside)
    with_socket = None
    try:
        with_socket = memvane_paths.install_socket(directory)
    except (OSError, ValueError):
        pass
    if with_socket is not None:
        for path in (with_socket, Path(str(with_socket) + '.lock')):
            try:
                os.unlink(path)
            except OSError:
                pass
    return reason is None, reason


def _project(directory):
    try:
        route = _record(directory / 'route.json')
    except ValueError:
        return None
    return route.get('projectRoot') if route else None


def cleanup(home=None, *, approve=False, owner=_owner):
    """Released installs: the preview rows, and with ``approve`` their removal."""
    base = memvane_paths.state_base(home)
    rows = _rows(base)
    if not approve:
        return {'status': 'planned', 'scope': 'released', 'installs': rows, 'changesApplied': 0}
    removed, freed, failed = 0, 0, []
    for row in rows:
        if row['action'] != 'remove':
            continue
        name = row['install']
        if name.startswith(REMOVING):
            reason = _delete(base / 'installs' / name)
            done = reason is None
        else:
            done, reason = _remove_install(base, name, owner)
        if done:
            removed += 1
            freed += row.get('bytes', 0)
            row['action'] = 'removed'
        else:
            row['action'] = 'kept'
            failed.append({'install': name, 'reason': reason})
    # The removal is this command's only effect: anything left is reported, never a silent success.
    return {'status': 'partial' if failed else 'collected', 'scope': 'released', 'installs': rows,
            'removed': removed, 'bytesFreed': freed, 'failed': failed, 'changesApplied': removed}


def _roots(home=None):
    """The per-user roots "delete everything" covers (the same places ``memvane_paths`` documents)."""
    home = Path(home) if home is not None else Path.home()
    name = memvane_paths.NAME
    darwin = sys.platform == 'darwin'
    return [('state', memvane_paths.state_base(home).parent),
            ('support', home.joinpath('Library', 'Application Support', name) if darwin
             else home.joinpath('.local', 'share', name)),
            ('cache', home.joinpath('Library', 'Caches', name) if darwin else home.joinpath('.cache', name))]


def cleanup_everything(home=None, *, approve=False):
    """Every per-user root: the preview rows, and with ``approve`` their removal."""
    rows = []
    for kind, path in _roots(home):
        if not os.path.lexists(path):
            continue
        # Only a real directory of this user at the documented place is ever a target.
        safe = _own_directory(path) and path.name == memvane_paths.NAME and Path(os.path.realpath(path)) == path
        row = {'root': kind, 'path': str(path), 'action': 'remove' if safe else 'keep'}
        if safe:
            row['bytes'], complete = _size(path)
            if not complete:
                row['bytesTruncated'] = True
        else:
            row['reason'] = 'unsafe_root'
        rows.append(row)
    if not approve:
        return {'status': 'planned', 'scope': 'everything', 'roots': rows, 'changesApplied': 0}
    removed, freed, failed = 0, 0, []
    for row in rows:
        if row['action'] != 'remove':
            if row.get('reason') == 'unsafe_root':
                failed.append({'root': row['root'], 'reason': 'unsafe_root'})
            continue
        path = Path(row['path'])
        aside = path.with_name(path.name + REMOVING + str(os.getpid()))
        try:
            # Renamed first: a running service sees its root gone and drains instead of writing into the tree.
            os.rename(path, aside)
            reason = _delete(aside)
        except OSError as exc:
            reason = type(exc).__name__
        if reason is None:
            removed += 1
            freed += row.get('bytes', 0)
            row['action'] = 'removed'
        else:
            row['action'] = 'kept'
            failed.append({'root': row['root'], 'reason': reason})
    return {'status': 'partial' if failed else 'collected', 'scope': 'everything', 'roots': rows,
            'removed': removed, 'bytesFreed': freed, 'failed': failed, 'changesApplied': removed}
