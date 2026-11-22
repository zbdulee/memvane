"""The vector arena: a contiguous float32 copy of embedding sets beside the view store.

The store keeps one vector per SQLite row, so reading a view's sets is one
random page read per row. The arena holds the same bytes in one append-only
file that a recall maps and scans in order. It is derived data in
``<install state root>/vector-cache/``: it can be deleted at any time, and
every byte in it is checked against the store before it is believed.

Files (directory 0700, files 0600, opened ``O_NOFOLLOW``):

    current.json        the active pair and its identity (``identity``)
    arena-<nonce>.vec   one header page, then rows of ``dimension`` float32
    arena-<nonce>.idx   one header, then fixed-length records, append-only

An idx record is ``(blob sha, first row, chunk count, set digest, vec crc,
record crc)``. ``vec crc`` is the crc32 of that set's bytes in the vec file;
``record crc`` is the crc32 of the record's other fields. A reader trusts the
records up to the first one whose record crc fails (a torn append), and of
several records of one blob the last.

A set is *covered* when its last valid record names the store's set digest,
lies inside the vec file and its vec crc matches the mapped bytes. Anything
else is missing and read from the store; a record that is present but wrong
is also reported as inconsistent. The reader takes no lock and never writes.

This module is the format and the reader. It imports neither the store nor
sync: the recall reader (store) and the sync owner's writer both use it.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import mmap
import os
import re
import stat
import struct
import zlib

from memvane.vectorcache.types import CURRENT, DIRECTORY, DTYPE, FORMAT, PAGE

VEC_MAGIC = b'MVARENAV'
IDX_MAGIC = b'MVARENAI'
IDX_HEADER = 32
_RECORD = struct.Struct('<32sQI32sI')
_CRC = struct.Struct('<I')
RECORD_BYTES = _RECORD.size + _CRC.size
IDENTITY_KEYS = ('format', 'nonce', 'profile_id', 'recipe_id', 'dimension', 'dtype', 'store_incarnation')
_NONCE = re.compile(r'[0-9a-f]{32}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MAX_CURRENT = 4096
_MAX_IDX = 256 * 1024 * 1024
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)


def identity(*, nonce, profile_id, recipe_id, dimension, store_incarnation):
    """The identity one arena pair is bound to; ``current.json`` and both headers carry it."""
    return {'format': FORMAT, 'nonce': nonce, 'profile_id': profile_id, 'recipe_id': recipe_id,
            'dimension': dimension, 'dtype': DTYPE, 'store_incarnation': store_incarnation}


def names(nonce):
    return 'arena-' + nonce + '.vec', 'arena-' + nonce + '.idx'


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def vec_header(value):
    """The vec file's header page."""
    body = VEC_MAGIC + _canonical(value)
    if len(body) > PAGE:
        raise ValueError('arena_header_too_long')
    return body + b'\0' * (PAGE - len(body))


def idx_header(value):
    """The idx file's header: the magic and the digest-sized crc pair of the identity."""
    body = _canonical(value)
    return (IDX_MAGIC + bytes.fromhex(value['nonce']) + _CRC.pack(zlib.crc32(body))).ljust(IDX_HEADER, b'\0')


def record(blob_sha, first_row, chunk_count, set_digest, vec_crc):
    """One idx record of a set whose rows start at ``first_row``."""
    body = _RECORD.pack(bytes.fromhex(blob_sha), first_row, chunk_count, bytes.fromhex(set_digest), vec_crc)
    return body + _CRC.pack(zlib.crc32(body))


def records(raw):
    """``{blob sha: (first row, chunk count, set digest, vec crc)}`` of the valid record prefix of idx bytes."""
    answer = {}
    for offset in range(0, len(raw) - RECORD_BYTES + 1, RECORD_BYTES):
        body = raw[offset:offset + _RECORD.size]
        if _CRC.unpack_from(raw, offset + _RECORD.size)[0] != zlib.crc32(body):
            break  # A torn append: nothing after it is believed.
        blob, first_row, chunk_count, digest, vec_crc = _RECORD.unpack(body)
        answer[blob.hex()] = (first_row, chunk_count, digest.hex(), vec_crc)
    return answer


def _open(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=directory_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise OSError('arena_file_unsafe')
    return fd, info.st_size


def _read(fd, size, limit):
    if size > limit:
        raise OSError('arena_file_too_large')
    data = b''
    while len(data) < size:
        part = os.read(fd, size - len(data))
        if not part:
            break
        data += part
    return data


class Arena:
    """One opened arena: its records and the mapped vec file. Close it with ``close``."""

    def __init__(self, value, found, mapped, rows):
        self.identity, self.records, self._mapped, self.rows = value, found, mapped, rows
        self.width = value['dimension'] * 4
        self._view = memoryview(mapped) if mapped is not None else None

    def close(self):
        if self._view is not None:
            self._view.release()
            self._view = None
        if self._mapped is not None:
            self._mapped.close()
            self._mapped = None

    def span(self, first_row, chunk_count):
        """The writable (private) bytes of ``chunk_count`` rows from ``first_row``."""
        begin = PAGE + first_row * self.width
        return self._view[begin:begin + chunk_count * self.width]

    def cover(self, sets):
        """``(covered, missing, inconsistent)`` of ``{blob sha: (chunk count, set digest)}``.

        ``covered`` maps a blob to its ``(first row, chunk count)``; ``missing``
        lists the blobs to read from the store; ``inconsistent`` counts the
        missing ones that had a record which did not hold.
        """
        covered, missing, inconsistent = {}, [], 0
        for blob, (chunk_count, digest) in sets.items():
            found = self.records.get(blob)
            if found is None:
                missing.append(blob)
                continue
            first_row, rows, stored, vec_crc = found
            if (rows != chunk_count or stored != digest or rows < 1 or first_row + rows > self.rows
                    or zlib.crc32(self.span(first_row, rows)) != vec_crc):
                missing.append(blob)
                inconsistent += 1
                continue
            covered[blob] = (first_row, rows)
        return covered, missing, inconsistent


def open_arena(directory, *, profile_id, recipe_id, dimension, store_incarnation):
    """The arena of ``directory`` bound to this store and vector spec, or None.

    None means "no arena": the directory or ``current.json`` is missing or
    unsafe, another format number or identity, a header that does not match,
    or any read failure. Never raises for those; the caller reads the store.
    """
    try:
        return _open_arena(directory, profile_id, recipe_id, dimension, store_incarnation)
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        return None


def _open_arena(directory, profile_id, recipe_id, dimension, store_incarnation):
    directory_fd = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | _NOFOLLOW | _CLOEXEC)
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        fd, size = _open(directory_fd, CURRENT)
        try:
            value = json.loads(_read(fd, size, _MAX_CURRENT).decode('utf-8'))
        finally:
            os.close(fd)
        if not isinstance(value, dict) or not isinstance(value.get('nonce'), str) or not _NONCE.match(value['nonce']):
            return None
        expected = identity(nonce=value['nonce'], profile_id=profile_id, recipe_id=recipe_id,
                            dimension=dimension, store_incarnation=store_incarnation)
        if {key: value.get(key) for key in IDENTITY_KEYS} != expected:
            return None
        vec_name, idx_name = names(value['nonce'])
        fd, size = _open(directory_fd, idx_name)
        try:
            raw = _read(fd, size, _MAX_IDX)
        finally:
            os.close(fd)
        if raw[:IDX_HEADER] != idx_header(expected):
            return None
        found = records(raw[IDX_HEADER:])
        fd, size = _open(directory_fd, vec_name)
        try:
            if size < PAGE or _read(fd, PAGE, PAGE) != vec_header(expected):
                return None
            rows = (size - PAGE) // (dimension * 4)
            # A private copy-on-write map: the kernel's buffer type needs a writable buffer,
            # and nothing is ever written back to the file.
            mapped = mmap.mmap(fd, 0, access=mmap.ACCESS_COPY) if rows else None
        finally:
            os.close(fd)
        return Arena(expected, found, mapped, rows)
    finally:
        os.close(directory_fd)


@contextmanager
def opened(directory, **spec):
    """``open_arena`` as a context: the arena or None, closed on exit."""
    arena = open_arena(directory, **spec) if directory is not None else None
    try:
        yield arena
    finally:
        if arena is not None:
            arena.close()


def merge(spans):
    """``(first row, rows)`` spans sorted with touching neighbours joined: fewer kernel calls."""
    answer = []
    for first, rows in sorted(spans):
        if answer and answer[-1][0] + answer[-1][1] == first:
            answer[-1][1] += rows
        else:
            answer.append([first, rows])
    return [tuple(item) for item in answer]
