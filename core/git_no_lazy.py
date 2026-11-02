"""Read-only Git lazy-fetch admission, independent of checkout captures."""
from __future__ import annotations

import re
import subprocess


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
    """Read only relevant keys, with Git's last-value order and remote case."""
    if not isinstance(raw, bytes) or len(raw) > _MAX_CONFIG_BYTES or not raw.endswith(b'\0'):
        _unavailable()
    effective = {}
    for record in raw[:-1].split(b'\0'):
        key, separator, value = record.partition(b'\n')
        key = key.decode('utf-8', 'surrogateescape')
        value = value.decode('utf-8', 'surrogateescape') if separator else None
        if key.lower() == 'extensions.partialclone':
            identity = ('extensions', 'partialclone')
        else:
            section, dot, rest = key.partition('.')
            remote, final_dot, variable = rest.rpartition('.')
            if (not dot or section.lower() != 'remote' or not final_dot or not remote or
                    variable.lower() not in ('promisor', 'partialclonefilter')):
                _unavailable()
            # The subsection is case-sensitive, unlike section and variable.
            identity = ('remote', remote, variable.lower())
        effective[identity] = value
    for identity, value in effective.items():
        if identity == ('extensions', 'partialclone') or identity[-1] == 'partialclonefilter':
            return True
        if _boolean(value):
            return True
    return False


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
        return  # Successfully read config; no partial-clone settings exist.
    if config.returncode != 0:
        _unavailable()
    if _partial_config(config.stdout):
        raise GitNoLazyFetchError('source_git_no_lazy_fetch_unsupported')
