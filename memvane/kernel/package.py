"""Sync owner namespace and read-only Git admission."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


def sync_owner_directory(*, create=False):
    """Stable UID sync exclusion for fixed installs.

    Production uses the account home, never HOME. Fixture roots must be
    canonical private descendants of fixed platform temp roots, never TMPDIR.
    """
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if raw is None:
        if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
            raise ValueError('journal_test_root_missing')
        if create:
            # Taking the live owner lock is a live write: host-installed copies only.
            from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
            require_cached_plugin_for_live_write()
        start = Path(pwd.getpwuid(os.getuid()).pw_dir)
        parts = ('.local', 'state', 'memvane', 'v1', 'sync-owner-locks')
        private_at = len(parts)
    else:
        root = Path(raw)
        roots = [Path('/tmp').resolve(), Path('/var/tmp').resolve()]
        if sys.platform == 'darwin':
            import ctypes
            buffer = ctypes.create_string_buffer(1024)
            libc = ctypes.CDLL(None, use_errno=True)
            size = libc.confstr(65537, buffer, len(buffer))
            if 0 < size <= len(buffer) and buffer.value:
                roots.append(Path(buffer.value.decode()).resolve())
        start = next((parent for parent in root.parents if parent in roots), None)
        if (not root.is_absolute() or root.resolve(strict=False) != root or start is None):
            raise ValueError('journal_test_root_unsafe')
        relative = root.relative_to(start).parts
        parts = (*relative, 'memvane-journal', 'sync-owner-locks')
        private_at = len(relative)
    if not start.is_absolute() or start.resolve(strict=True) != start:
        raise ValueError('journal_root_unsafe')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(start, flags)
    try:
        for position in range(len(parts) + 1):
            info = os.fstat(fd)
            # The fixed temp boundary itself may be root-owned/shared. Every
            # descendant, and the account-home path, must be owned by this UID.
            if raw is None or position:
                mask = 0o077 if position >= private_at else 0o022
                if info.st_uid != os.getuid() or info.st_mode & mask:
                    raise ValueError('journal_root_unsafe')
            if position == len(parts):
                break
            part = parts[position]
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        target = start.joinpath(*parts)
        if (target.resolve(strict=True) != target
                or not os.path.samestat(os.fstat(fd), target.lstat())):
            raise ValueError('journal_root_unsafe')
        return target
    finally:
        os.close(fd)


# Read-only Git admission is part of this existing standalone dependency.
# Maintenance releases that backport GIT_NO_LAZY_FETCH; upstream from 2.46.
NO_LAZY_FETCH_PATCHES = {39: 4, 40: 2, 41: 1, 42: 2, 43: 4, 44: 1, 45: 1}
_RELATED_CONFIG = r'^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$'
_MAX_CONFIG_BYTES = 65536


class GitNoLazyFetchError(ValueError):
    pass


def supports_no_lazy_fetch(version):
    """Whether the reported Git release implements the no-lazy-fetch fence."""
    if isinstance(version, bytes):
        version = version.decode('utf-8', 'replace')
    if not isinstance(version, str):
        return False
    parsed = re.match(r'^git version ([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(?=$|[ .])',
                      version)
    if parsed is None:
        return False
    major, minor, patch = map(int, parsed.groups())
    required = NO_LAZY_FETCH_PATCHES.get(minor)
    return major == 2 and (minor >= 46 or (required is not None and patch >= required))


def _unavailable():
    raise GitNoLazyFetchError('source_git_no_lazy_fetch_unavailable')


def _boolean(value):
    if value is None:
        return True  # A valueless Git boolean is true; an empty value is false.
    lowered = value.lower()
    if lowered in ('true', 'yes', 'on'):
        return True
    if lowered in ('', 'false', 'no', 'off'):
        return False
    if re.fullmatch(r'[+-]?[0-9]+', value):
        try:
            return int(value) != 0
        except ValueError:
            _unavailable()
    _unavailable()


def _partial_config(raw):
    """Validate every relevant row and accumulate Git's registration evidence.

    Each true/valueless promisor or filter registers a remote. A later false
    does not undo that registration. No row is skipped after finding one.
    """
    if not isinstance(raw, bytes) or len(raw) > _MAX_CONFIG_BYTES or not raw.endswith(b'\0'):
        _unavailable()
    partial = False
    for record in raw[:-1].split(b'\0'):
        key, separator, value = record.partition(b'\n')
        key = key.decode('utf-8', 'surrogateescape')
        value = value.decode('utf-8', 'surrogateescape') if separator else None
        if key.lower() == 'extensions.partialclone':
            if value is None:
                _unavailable()
            partial = True
            continue
        section, dot, rest = key.partition('.')
        remote, final_dot, variable = rest.rpartition('.')
        if (not dot or section.lower() != 'remote' or not final_dot or not remote or
                variable.lower() not in ('promisor', 'partialclonefilter')):
            _unavailable()
        # Subsection names stay case-sensitive; rows are never collapsed.
        if variable.lower() == 'partialclonefilter':
            if value is None:
                _unavailable()
            partial = True
        else:
            registered = _boolean(value)  # Validate even if partial is already true.
            partial = registered or partial
    return partial


def require_no_lazy_fetch(root, *, prefix, env, timeout=15):
    """Refuse unsafe Git only when a full repository cannot be established.

    The probes use the caller's executable, prefix, directory and environment.
    Neither probe reads objects or invokes a transport. Output stays private.
    """
    command = (*prefix, '-C', str(root))
    try:
        version = subprocess.run((*command, '--version'), stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, check=False,
                                 timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitNoLazyFetchError('source_git_no_lazy_fetch_unavailable') from exc
    if version.returncode != 0:
        _unavailable()
    if supports_no_lazy_fetch(version.stdout):
        return
    try:
        config = subprocess.run((*command, 'config', '--null', '--includes',
                                 '--get-regexp', _RELATED_CONFIG),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=False, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitNoLazyFetchError('source_git_no_lazy_fetch_unavailable') from exc
    if config.returncode == 1 and config.stdout == b'':
        return
    if config.returncode != 0:
        _unavailable()
    if _partial_config(config.stdout):
        raise GitNoLazyFetchError('source_git_no_lazy_fetch_unsupported')
