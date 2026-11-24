"""One project invocation's allowance, inherited only by its synchronous compile children."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import mmap
import hashlib
import os
from pathlib import Path
import secrets
import stat
import struct
import tempfile

MIB = 1024 ** 2
LIMITS = {'source': 8*MIB, 'verify': 8*MIB, 'private': 8*MIB, 'evidence': 24*MIB, 'commits': 32,
          'source_rows': 4096, 'verify_rows': 4096, 'private_rows': 4096}
_INDEX = {key: i for i, key in enumerate(LIMITS)}
_CURRENT = ContextVar('wiki_queue_pass', default=None)
_FD_KEY = '_MEMVANE_HANDOFF_PASS'
_HEADER = b'MV.queue.budget.v1:'
_START = len(_HEADER) + 16
_NUMBER = struct.Struct('!Q')
_SIZE = _START + _NUMBER.size * len(LIMITS)


def current():
    return _CURRENT.get()


class Budget:
    def __init__(self, fd, cookie):
        self.fd, self.cookie = fd, cookie
        self.mapping = mmap.mmap(fd, _SIZE)
        self.snapshots = {}
        self.rows = set()
        self.deadline = None

    def available(self, key):
        return _NUMBER.unpack_from(self.mapping, _START + _INDEX[key] * _NUMBER.size)[0]

    def debit(self, key, count):
        available = self.available(key)
        if type(count) is not int or not 0 <= count <= available:
            raise BlockingIOError('handoff_' + key + '_pass_limit')
        _NUMBER.pack_into(self.mapping, _START + _INDEX[key] * _NUMBER.size, available - count)


def charge(channel, amount):
    ledger = current()
    if ledger:
        ledger.debit(channel, amount)


def channel(path):
    path = Path(path)
    for name in ('source', 'verify'):
        if path.name.startswith(name + '-queue.jsonl'):
            return name
    if path.name.startswith(('native.claimed.', 'ready-')):
        for name in ('source', 'verify'):
            if path.parent.name == hashlib.sha256((name + '-queue.jsonl').encode()).hexdigest()[:24]:
                return name
    return 'private'


def observe(path, inode, offset, raw):
    ledger = current()
    if ledger is None:
        return
    lane = channel(path)
    identity = (lane, inode, offset, hashlib.sha256(raw).digest())
    if identity not in ledger.rows:
        ledger.debit(lane + '_rows', 1)
        ledger.rows.add(identity)


def child_options(env=None):
    ledger = current()
    if ledger is None:
        return {} if env is None else {'env': env}
    environment = dict(os.environ if env is None else env)
    environment[_FD_KEY] = f'{ledger.fd},{ledger.cookie.hex()}'
    return {'env': environment, 'pass_fds': (ledger.fd,)}


@contextmanager
def scope():
    if current():
        yield current()
        return
    inherited = os.environ.get(_FD_KEY)
    stream = None
    if inherited:
        descriptor, token = inherited.split(',')
        fd, cookie = int(descriptor), bytes.fromhex(token)
        info = os.fstat(fd)
        if (len(cookie) != 16 or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 0 or info.st_size != _SIZE or info.st_mode & 0o077 or
                os.pread(fd, _START, 0) != _HEADER + cookie):
            raise OSError('invalid_handoff_pass')
    else:
        stream = tempfile.TemporaryFile()
        fd, cookie = stream.fileno(), secrets.token_bytes(16)
        stream.write(_HEADER + cookie + b''.join(_NUMBER.pack(n) for n in LIMITS.values()))
        stream.flush()
    ledger = Budget(fd, cookie)
    token = _CURRENT.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT.reset(token)
        ledger.mapping.close()
        if stream is not None:
            stream.close()


def bounded(method):
    @wraps(method)
    def run(*args, **kwargs):
        with scope():
            return method(*args, **kwargs)
    return run
