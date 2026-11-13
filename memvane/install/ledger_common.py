"""Selection ledger reads, writes and locking primitives."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from memvane.kernel.atomic_rename import fsync_dir as _fsync_dir, canonical_json_bytes as _canon
from memvane.journal.types import CutoverBlocked
from memvane.journal.api import (ledger_private_file as _private_file,
    ledger_read_json as _read_json, ledger_write as _write)
import stat

# One project-wide lock name shared by every origin and by older installed
# bundle copies; renaming it would stop cross-version mutual exclusion.
OPERATION_LOCK = 'laya-qmd-cutover-operation.lock'
# Ledger field -> ``laya_initial_build.evidence`` field.
BOUND_EVIDENCE = (('indexGeneration', 'generation'), ('bindingDigest', 'bindingDigest'),
                  ('sourceManifest', 'sourceManifest'), ('catalogScanId', 'catalogScanId'),
                  ('catalogRawDigest', 'catalogRawDigest'),
                  ('vectorProfileId', 'vectorProfileId'), ('vectorRows', 'vectorRows'),
                  ('candidateIncarnation', 'dbIncarnation'))




def _fail(reason):
    raise CutoverBlocked(reason)




def _sha(raw):
    return hashlib.sha256(raw).hexdigest()










def _preflight_multi_settings(route, project_root):
    """Refuse a route the selected host cannot serve before publication."""
    from memvane.settings import api as config
    from memvane.route.api import (multi_compile_supported as _multi_compile_supported,
                                       multi_settings_supported as _multi_settings_supported,
                                       pairs as _pairs, read_regular as _read_regular,
                                       source_for_collection as _source_for_collection,
                                       settings_identity_digest)
    from memvane.settings import api as project_dir, types as project_types
    settings = (project_dir.settings_path(project_root) or
                project_dir.folder(project_root) / project_types.SETTINGS)
    if settings.is_symlink():
        _fail('cutover_settings_unsafe')
    raw = _read_regular(settings, 2 * 1024 * 1024)
    # The route binds the resolved settings identity, proved below against
    # the settings file itself.
    if route['settingsSha256'] != settings_identity_digest(project_root,
                                                           route['collections']):
        _fail('cutover_settings_changed')
    parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    if not isinstance(parsed, dict):
        _fail('cutover_settings_unsupported')
    folder = project_dir.active_name(project_root)
    parsed = config.expand_minimal_settings(parsed, folder)
    cfg = config.normalize_config(parsed, folder)
    declared = route['collections']
    names = tuple(item['name'] for item in declared)
    roles = config.role_map(cfg)
    if (cfg.get('indexing') is not True or
            sorted(cfg.get('collections', [])) != list(names) or
            not _multi_settings_supported(parsed, cfg, names) or
            set(cfg.get('collectionPaths', {})) != set(names)):
        _fail('cutover_settings_unsupported')
    wiki_root = next((item['sourceRoot'] for item in declared
                      if item['role'] == 'wiki'), None)
    if (wiki_root is None or not _multi_compile_supported(
            parsed, cfg, Path(project_root), wiki_root)):
        _fail('cutover_settings_unsupported')
    for item in declared:
        name = item['name']
        if (item['role'] != roles.get(name) or
                item['sourceRoot'] != _source_for_collection(
                    Path(project_root), cfg['collectionPaths'][name])):
            _fail('cutover_settings_mismatch')


def _identity(path):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve(strict=True) != path):
        _fail('cutover_bound_file_unavailable')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail('cutover_bound_file_unsafe')
    return [info.st_dev, info.st_ino]


@contextmanager
def _ledger_lock(ledger_path):
    lock_path = Path(ledger_path).with_suffix('.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('cutover_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _operation_lock(marker_path):
    """Serialize route mutations for one project across different state roots.

    A separate lock avoids nesting ``_ledger_lock`` inside ``_change``. The
    installed product lock, when present, is acquired first by its caller.
    Readers may observe intermediate phases and must classify them blocked.
    """
    parent = Path(marker_path).parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o022):
        _fail('cutover_operation_parent_unsafe')
    path = parent / OPERATION_LOCK
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError as exc:
        raise CutoverBlocked('cutover_operation_lock_unavailable') from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077):
            _fail('cutover_operation_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CutoverBlocked('cutover_operation_busy') from exc
        after = path.lstat()
        if (not stat.S_ISREG(after.st_mode) or
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            _fail('cutover_operation_lock_changed')
        yield
    finally:
        os.close(fd)




