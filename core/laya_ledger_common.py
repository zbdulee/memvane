"""Selection-ledger primitives shared by the Laya selection and its upgrades.

The fresh-wiki ledger, the selected-route classifier and the
active/bound/inactive bundle upgrades all read, write and lock their records
through these functions. This module imports only the standard library at
module level.

A QMD-origin selection (``qmd-cutover.json`` and its marker schema) is no
longer served or upgraded. Every writer refuses a state root that still
holds that ledger as ``qmd_origin_detach_required``, and the route selector
classifies its marker ``blocked`` with the same reason.

``CutoverBlocked`` keeps its name: callers match it by class name.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

QMD_LEDGER = 'qmd-cutover.json'
QMD_MARKER_SCHEMA = 'laya-qmd-selection-marker-v1'
QMD_ORIGIN_DETACH_REQUIRED = 'qmd_origin_detach_required'
# One project-wide lock name shared by every origin and by older installed
# bundle copies; renaming it would stop cross-version mutual exclusion.
OPERATION_LOCK = 'laya-qmd-cutover-operation.lock'
# Ledger field -> ``laya_initial_build.evidence`` field.
BOUND_EVIDENCE = (('indexGeneration', 'generation'), ('bindingDigest', 'bindingDigest'),
                  ('sourceManifest', 'sourceManifest'), ('catalogScanId', 'catalogScanId'),
                  ('catalogRawDigest', 'catalogRawDigest'),
                  ('vectorProfileId', 'vectorProfileId'), ('vectorRows', 'vectorRows'),
                  ('candidateIncarnation', 'dbIncarnation'))


class CutoverBlocked(RuntimeError):
    pass


def _fail(reason):
    raise CutoverBlocked(reason)


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _private_file(path, limit=65536):
    path = Path(path)
    if path.is_symlink():
        _fail('cutover_symlink')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > limit):
            _fail('cutover_file_unsafe')
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or
                (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            _fail('cutover_file_changed')
        return raw
    except OSError as exc:
        raise CutoverBlocked('cutover_file_unavailable') from exc
    finally:
        os.close(fd)


def _read_json(path):
    try:
        raw = _private_file(path)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    _fail('cutover_record_duplicate_key')
                value[key] = item
            return value
        def invalid_constant(_value):
            _fail('cutover_record_nonfinite')
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                            parse_constant=invalid_constant)
        if raw != _canon(parsed):
            _fail('cutover_record_noncanonical')
        return parsed
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CutoverBlocked('cutover_record_invalid') from exc


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value, *, replace):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.laya-cutover-', dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_canon(value) if not isinstance(value, bytes) else value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                _fail('cutover_symlink')
            os.replace(temp, path)
        else:
            os.link(temp, path)
        _fsync_dir(path.parent)
    except FileExistsError as exc:
        raise CutoverBlocked('cutover_record_exists') from exc
    finally:
        temp.unlink(missing_ok=True)


def _preflight_multi_settings(route, project_root):
    """Refuse a route the selected host cannot serve before publication."""
    import config
    from laya_index_host_route import (_multi_compile_supported,
                                       _multi_settings_supported, _pairs,
                                       _read_regular, _source_for_collection,
                                       settings_identity_digest)
    settings = Path(project_root) / '.auto-context' / 'settings.json'
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
    parsed = config.expand_minimal_settings(parsed)
    cfg = config.normalize_config(parsed)
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


def qmd_ledger_present(state_root):
    """True when ``state_root`` still holds a QMD-origin selection ledger."""
    path = Path(state_root) / QMD_LEDGER
    return path.exists() or path.is_symlink()


def refuse_qmd_origin(state_root):
    """Refuse a state root still bound to a QMD-origin ledger.

    Only the detach step of releases 0.37.8 to 0.37.10 converts such a
    ledger to the fresh-wiki ledger; this code never activates, upgrades or
    restores it.
    """
    if qmd_ledger_present(state_root):
        _fail(QMD_ORIGIN_DETACH_REQUIRED)
