"""Optional, durable QA ceiling for provider CLI launches across worker runs.

This is inactive unless both QMD_QA_CALL_LEDGER and QMD_QA_CALL_LIMIT are set.
Reservations are made before Popen and never refunded: after a crash or an
uncertain launch, counting the attempt is safer than silently overspending.
"""

import fcntl
import os
from pathlib import Path
import stat
import sys

from resource_deferred import ResourceDeferred


HEADER = 'qmd-qa-provider-calls-v1:'
MAX_LIMIT = 1000
MAX_LEDGER_BYTES = 4096


def _limit(value):
    if isinstance(value, bool) or not str(value).isdecimal():
        raise ValueError('invalid_limit')
    number = int(value)
    if not 1 <= number <= MAX_LIMIT:
        raise ValueError('invalid_limit')
    return number


def _open_owned(path, flags):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('relative_ledger')
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise ValueError('unsafe_ledger')
    return fd


def _read(fd, limit):
    os.lseek(fd, 0, os.SEEK_SET)
    size = os.fstat(fd).st_size
    if size > MAX_LEDGER_BYTES:
        raise ValueError('oversized_ledger')
    parts = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise ValueError('short_ledger_read')
        parts.append(chunk)
        remaining -= len(chunk)
    if os.fstat(fd).st_size != size:
        raise ValueError('changed_ledger')
    data = b''.join(parts)
    expected = f'{HEADER}{limit}\n'.encode('ascii')
    if not data.startswith(expected):
        raise ValueError('ledger_limit_mismatch')
    reservations = data[len(expected):]
    if reservations != b'R\n' * (len(reservations) // 2):
        raise ValueError('malformed_ledger')
    used = len(reservations) // 2
    if used > limit:
        raise ValueError('ledger_exceeds_limit')
    return used


def initialize(path, *, limit, used):
    """Create once with an audited preexisting call count; never reset a ledger."""
    limit = _limit(limit)
    if type(used) is not int or not 0 <= used <= limit:
        raise ValueError('invalid_used')
    fd = _open_owned(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        data = f'{HEADER}{limit}\n'.encode('ascii') + b'R\n' * used
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def status(path, limit):
    fd = _open_owned(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return _read(fd, _limit(limit))
    finally:
        os.close(fd)


def reserve_from_env():
    path = os.environ.get('QMD_QA_CALL_LEDGER')
    raw_limit = os.environ.get('QMD_QA_CALL_LIMIT')
    if path is None and raw_limit is None:
        return
    try:
        if not path or raw_limit is None:
            raise ValueError('incomplete_configuration')
        limit = _limit(raw_limit)
        fd = _open_owned(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            used = _read(fd, limit)
            if used >= limit:
                raise ResourceDeferred('qa_call_budget_exhausted')
            os.lseek(fd, 0, os.SEEK_END)
            if os.write(fd, b'R\n') != 2:
                raise OSError('short_ledger_write')
            os.fsync(fd)
        finally:
            os.close(fd)
    except ResourceDeferred:
        raise
    except (OSError, ValueError) as exc:
        raise ResourceDeferred('qa_call_budget_invalid') from exc


def main(argv=None):
    """Reserve a QA host turn before exec, sharing its ledger with workers."""
    import argparse
    parser = argparse.ArgumentParser(description='Private QA model-call ledger')
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('init', 'status', 'reserve', 'run'):
        action = actions.add_parser(name)
        action.add_argument('path', type=Path)
        action.add_argument('limit', type=int)
        if name == 'init':
            action.add_argument('--used', type=int, required=True)
        elif name == 'run':
            action.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == 'init':
        initialize(args.path, limit=args.limit, used=args.used)
    else:
        if args.action in ('reserve', 'run'):
            os.environ['QMD_QA_CALL_LEDGER'] = str(args.path)
            os.environ['QMD_QA_CALL_LIMIT'] = str(args.limit)
            if args.action == 'run':
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    parser.error('run requires a command after --')
            reserve_from_env()
            if args.action == 'run':
                os.execvpe(command[0], command, os.environ.copy())
        print(status(args.path, args.limit))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ResourceDeferred as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(75)
