"""The vector arena's writer: the sync owner keeps the arena equal to the store's live sets.

One ``Writer`` serves one sync pass, under the sync owner (the only writer
of an install's arena). It never reads the store itself: the caller hands it
the live sets (``{blob sha: (chunk count, set digest)}``) and a ``read(blob)``
that returns one set's float32 bytes from the store, or None.

    start(live, read)   prepare, self-check, compact, catch up (bounded)
    add(live, read)     append what a publication is about to make visible
    summary()           what the pass did and what is left
    close()

Self-check: every record of a live set is compared with the store's digest
and chunk count, its range and its vec crc. A record that does not hold is
appended again from the store (``repaired``); the old rows become dead rows.
Nothing a recall reported is needed for that, and a recall never writes here.

Appends write the vec rows, fsync, then the idx records, fsync: a record
never names rows that are not on disk. A torn idx tail is padded up to the
next record boundary before the next append (the reader skips the record the
padding completes). Vec rows that no record names (a torn append) are never
appended after: the live sets are copied into a new pair. A vec file is never
truncated or rewritten in place; a recall that mapped it reads it to its end.
The same holds for rows whose bytes no longer match their record's crc: the
set is read again from the store and the pair is replaced in that pass.

A compaction first asks the disk for the bytes of the copy (not the quota:
the copy is never larger than the pair it replaces). A pair that does not
become the active one is closed and removed, so a pass that fails leaves no
file and no descriptor behind; an old pair left by a pass that failed after
its switch is removed when the next pass opens the arena.

The whole arena is replaced only when its identity is not this store's and
vector spec's (or its format number is lower). A higher format number is
another bundle's: it is left alone and the pass is ``skipped``.

Every failure here is the arena's alone: the caller publishes regardless.
"""
from __future__ import annotations

import errno
import json
import mmap
import os
import re
import stat
import time
import zlib

from memvane.vectorcache import arena as fmt
from memvane.vectorcache import types
from memvane.vectorcache.types import CURRENT, FORMAT, PAGE

_COMMIT_SETS = 256
_MAX_IDX = 256 * 1024 * 1024
_ARENA_FILE = re.compile(r'arena-[0-9a-f]{32}\.(vec|idx)\Z')
_OWNED_FILE = re.compile(r'(current\.json(\.[0-9a-f]{32}\.tmp)?|arena-[0-9a-f]{32}\.(vec|idx))\Z')
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)


def _write(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _private_directory(path):
    fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise OSError('arena_directory_unsafe')
    return fd


def _owned_files(directory_fd):
    """``[(name, size, mtime)]`` of the arena files in an arena directory."""
    answer = []
    for name in os.listdir(directory_fd):
        if not _OWNED_FILE.match(name):
            continue
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            answer.append((name, info.st_size, info.st_mtime))
    return answer


class Writer:
    """One sync pass's writer of the arena in ``directory`` (see the module text)."""

    def __init__(self, directory, *, profile_id, recipe_id, dimension, store_incarnation,
                 clock=time.monotonic, now=time.time):
        self._directory = str(directory)
        self._spec = dict(profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                          store_incarnation=store_incarnation)
        self._width = dimension * 4
        self._clock, self._now = clock, now
        self._dfd = self._vec = self._idx = None
        self._records = {}   # The live sets whose record held: blob -> (first row, rows, digest, vec crc).
        self._rows = 0       # Rows in the vec file.
        self._wanted = set()
        self._suspect = set()
        self._quota = False
        # Rows that may hold anything: vec rows after the last record (a torn append), or the rows
        # of a record whose bytes no longer match its crc. Nothing is appended beside them.
        self._untracked = False
        self._others = None  # Bytes of the other installs' arenas, counted once per pass.
        self._add_rows, self._add_deadline = types.PASS_VECTORS, None
        self._report = dict(rebuilt=False, compacted=False, repaired=0, catchupRows=0, appendRows=0,
                            unreadable=0, skipped=None, ms=0.0)

    # ------------------------------------------------------------------ the pass

    def start(self, live, read):
        """Prepare the arena, check it against ``live``, compact it and catch up; never raises OSError."""
        self._wanted, began = set(live), self._clock()
        try:
            if not self._prepare():
                return
            self._check(live)
            if self._untracked or self._dead_rows() * self._width >= types.COMPACT_BYTES or (
                    self._rows and self._dead_rows() >= self._rows * types.COMPACT_SHARE):
                self._compact()
            self._append(live, read, types.PASS_VECTORS, self._clock() + types.PASS_SECONDS, 'catchupRows')
            os.utime(CURRENT, dir_fd=self._dfd)  # The install's last use, read by other installs' quota.
        except OSError:
            self._report['skipped'] = 'write_unavailable'
        finally:
            self._report['ms'] += (self._clock() - began) * 1000

    def add(self, live, read):
        """Append the live sets the arena does not hold yet, inside the pass's own bound."""
        if self._vec is None or self._report['skipped'] in ('format_newer', 'write_unavailable'):
            return  # Also after a compaction that failed: nothing is appended beside rows no record names.
        began = self._clock()
        if self._add_deadline is None:
            self._add_deadline = began + types.PASS_SECONDS
        # What this publication makes visible is wanted too: sets left here are left to the catch-up.
        self._wanted |= set(live)
        try:
            self._add_rows -= self._append(live, read, max(self._add_rows, 0), self._add_deadline, 'appendRows')
        except OSError:
            self._report['skipped'] = 'write_unavailable'
        finally:
            self._report['ms'] += (self._clock() - began) * 1000

    def summary(self):
        """The pass's report: counts, what is left (``remaining`` sets) and why nothing moved (``skipped``)."""
        answer = dict(self._report, remaining=len(self._wanted - set(self._records)), ms=round(self._report['ms'], 3),
                      deadRows=self._dead_rows(), bytes=PAGE + self._rows * self._width if self._vec is not None else 0)
        moved = answer['catchupRows'] or answer['appendRows']
        if answer['skipped'] is None and not moved:
            # The quota is a reason whatever is left; an unreadable set only while sets are left.
            answer['skipped'] = ('quota' if self._quota
                                 else 'set_unreadable' if answer['unreadable'] and answer['remaining'] else None)
        return answer

    def close(self):
        for name in ('_vec', '_idx', '_dfd'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    # ------------------------------------------------------------------ files

    def _prepare(self):
        try:
            os.mkdir(self._directory, 0o700)
        except FileExistsError:
            pass
        self._dfd = _private_directory(self._directory)
        value = self._current()
        if isinstance(value, dict) and type(value.get('format')) is int and value['format'] > FORMAT:
            self._report['skipped'] = 'format_newer'
            return False
        nonce = value.get('nonce') if isinstance(value, dict) else None
        if (isinstance(nonce, str) and fmt._NONCE.match(nonce)
                and {key: value.get(key) for key in fmt.IDENTITY_KEYS} == fmt.identity(nonce=nonce, **self._spec)
                and self._open_pair(nonce)):
            self._sweep(nonce)
            return True
        nonce, vec, idx = self._new_pair()
        try:
            self._vec, self._idx = self._activate(nonce, vec, idx)
        except OSError:
            self._discard(nonce, vec, idx)
            raise
        self._records, self._rows = {}, 0
        self._report['rebuilt'] = value is not None
        return True

    def _current(self):
        """The parsed ``current.json``, ``False`` when it cannot be read as one, None when there is none."""
        try:
            fd = os.open(CURRENT, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=self._dfd)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        try:
            return json.loads(os.read(fd, 4096).decode('utf-8'))
        except (OSError, ValueError):
            return False
        finally:
            os.close(fd)

    def _open_pair(self, nonce):
        """Open the active pair for appending, aligned to whole rows and records; False when it is not usable."""
        value, opened = fmt.identity(nonce=nonce, **self._spec), []
        try:
            for name in fmt.names(nonce):
                fd = os.open(name, os.O_RDWR | os.O_APPEND | _NOFOLLOW | _CLOEXEC, dir_fd=self._dfd)
                opened.append(fd)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                        or info.st_mode & 0o077):
                    raise OSError('arena_file_unsafe')
            vec, idx = opened
            vec_size, idx_size = os.fstat(vec).st_size, os.fstat(idx).st_size
            if (os.pread(vec, PAGE, 0) != fmt.vec_header(value) or idx_size > _MAX_IDX
                    or os.pread(idx, fmt.IDX_HEADER, 0) != fmt.idx_header(value)):
                raise OSError('arena_header_mismatch')
            # A torn append: pad to the next boundary. The padded row is dead, the padded record fails its crc.
            _write(vec, b'\0' * (-(vec_size - PAGE) % self._width))
            _write(idx, b'\0' * (-(idx_size - fmt.IDX_HEADER) % fmt.RECORD_BYTES))
            self._rows = (os.fstat(vec).st_size - PAGE) // self._width
            self._records = fmt.records(os.pread(idx, os.fstat(idx).st_size, fmt.IDX_HEADER))
            # Rows no record names may hold anything (a zero row makes every recall scan its
            # neighbours set by set): nothing is appended after them, the pair is replaced.
            self._untracked = self._rows > max((first + rows for first, rows, _d, _c in self._records.values()),
                                               default=0)
            self._vec, self._idx = vec, idx
            return True
        except OSError:
            for fd in opened:
                os.close(fd)
            return False

    def _sweep(self, nonce):
        """Remove every arena file but the pair ``nonce`` and ``current.json``.

        A pass that failed after its switch left the old pair behind; nothing here raises.
        """
        keep = set(fmt.names(nonce)) | {CURRENT}
        try:
            found = _owned_files(self._dfd)
        except OSError:
            return
        for name, _size, _mtime in found:
            if name not in keep:
                try:
                    os.unlink(name, dir_fd=self._dfd)  # A recall that mapped it reads it to its end.
                except OSError:
                    pass  # Unchecked: an orphan is removed by the next pass that opens the pair.

    def _new_pair(self):
        nonce = os.urandom(16).hex()
        value, opened = fmt.identity(nonce=nonce, **self._spec), []
        try:
            for name, header in zip(fmt.names(nonce), (fmt.vec_header(value), fmt.idx_header(value))):
                fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                             0o600, dir_fd=self._dfd)
                opened.append(fd)
                _write(fd, header)
        except OSError:
            self._discard(nonce, *opened)
            raise
        return (nonce, *opened)

    def _discard(self, nonce, *opened):
        """Close and remove a pair that did not become the active one: a failed pass leaves nothing behind."""
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass  # Unchecked: the descriptor is gone either way.
        value = self._current()
        if isinstance(value, dict) and value.get('nonce') == nonce:
            return  # The rename happened: this pair is the arena, whatever failed after it.
        for name in (*fmt.names(nonce), CURRENT + '.' + nonce + '.tmp'):
            try:
                os.unlink(name, dir_fd=self._dfd)
            except OSError:
                pass  # Unchecked: absent, or removed by the next activation.

    def _activate(self, nonce, vec, idx):
        """Make the pair ``nonce`` the active one and remove every other arena file."""
        os.fsync(vec)
        os.fsync(idx)
        body = json.dumps(fmt.identity(nonce=nonce, **self._spec), sort_keys=True).encode('ascii')
        temporary = CURRENT + '.' + nonce + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=self._dfd)
        try:
            _write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, CURRENT, src_dir_fd=self._dfd, dst_dir_fd=self._dfd)
        os.fsync(self._dfd)
        self._sweep(nonce)
        return vec, idx

    # ------------------------------------------------------------------ check, compact, append

    def _dead_rows(self):
        return self._rows - sum(rows for _first, rows, _digest, _crc in self._records.values())

    def _check(self, live):
        """Keep the records of live sets that hold; a live set whose record does not is ``suspect``."""
        mapped = mmap.mmap(self._vec, PAGE + self._rows * self._width, access=mmap.ACCESS_READ) if self._rows else None
        view = memoryview(mapped) if mapped is not None else None
        try:
            kept = {}
            for blob, (first, rows, digest, crc) in self._records.items():
                begin = PAGE + first * self._width
                if blob not in live:
                    # A dead set's rows stay between member sets until a compaction: they must be what was written.
                    self._untracked = self._untracked or (rows >= 1 and first + rows <= self._rows and zlib.crc32(
                        view[begin:begin + rows * self._width]) != crc)
                    continue
                if ((rows, digest) == tuple(live[blob]) and rows >= 1 and first + rows <= self._rows
                        and zlib.crc32(view[begin:begin + rows * self._width]) == crc):
                    kept[blob] = (first, rows, digest, crc)
                else:
                    self._suspect.add(blob)
                    # Rows inside the file that are not what was written (zeros after a power cut):
                    # left between member sets, every recall would scan its neighbours set by set.
                    self._untracked = self._untracked or (rows >= 1 and first + rows <= self._rows
                                                          and (rows, digest) == tuple(live[blob]))
            self._records = kept
        finally:
            if view is not None:
                view.release()
                mapped.close()

    def _compact(self):
        """Copy the sets that hold into a new pair; a set whose bytes changed since the check is left to catch-up."""
        # The copy needs its bytes beside the old pair until the switch: the disk is asked first. The
        # quota is not: the copy is never larger than the pair it replaces, so the switch only frees bytes.
        need = PAGE + fmt.IDX_HEADER + sum(count * self._width + fmt.RECORD_BYTES
                                           for _first, count, _digest, _crc in self._records.values())
        space = os.fstatvfs(self._dfd)
        if space.f_bavail * space.f_frsize < need:
            raise OSError(errno.ENOSPC, 'arena_compaction_space')
        nonce, vec, idx = self._new_pair()
        kept, rows, entries = {}, 0, []
        try:
            for blob, (first, count, digest, crc) in sorted(self._records.items(), key=lambda item: item[1][0]):
                data = os.pread(self._vec, count * self._width, PAGE + first * self._width)
                if len(data) != count * self._width or zlib.crc32(data) != crc:
                    self._suspect.add(blob)
                    continue
                _write(vec, data)
                kept[blob] = (rows, count, digest, crc)
                entries.append(fmt.record(blob, rows, count, digest, crc))
                rows += count
            os.fsync(vec)
            _write(idx, b''.join(entries))
            old = (self._vec, self._idx)
            self._vec, self._idx = self._activate(nonce, vec, idx)
        except OSError:
            self._discard(nonce, vec, idx)
            raise
        for fd in old:
            os.close(fd)
        self._records, self._rows, self._untracked = kept, rows, False
        self._report['compacted'] = True

    def _append(self, live, read, max_rows, deadline, counter):
        """Append the live sets without a record that holds, up to ``max_rows`` and ``deadline``; rows appended."""
        cap = max(2 * self._width * sum(count for count, _digest in live.values()), types.INSTALL_FLOOR_BYTES)
        done, asked, batch = 0, 0, []
        try:
            for blob in sorted(live):
                if blob in self._records:
                    continue
                count, digest = live[blob]
                # The first set of a call is always tried: a bound never stops all progress. Every
                # set asked of the store counts, also one the store could not read.
                if asked and (asked + count > max_rows or self._clock() >= deadline):
                    break
                asked += count
                size = count * self._width
                if PAGE + (self._rows + count) * self._width > cap or not self._room(size):
                    self._quota = True
                    break
                data = read(blob)
                if not isinstance(data, (bytes, bytearray)) or len(data) != size:
                    self._report['unreadable'] += 1
                    continue
                _write(self._vec, data)
                batch.append((blob, self._rows, count, digest, zlib.crc32(data)))
                self._rows += count
                done += count
                if len(batch) >= _COMMIT_SETS:
                    self._commit(batch, counter)
                    batch = []
        finally:
            if batch:
                self._commit(batch, counter)
        return done

    def _commit(self, batch, counter):
        os.fsync(self._vec)  # The rows are on disk before any record names them.
        _write(self._idx, b''.join(fmt.record(*entry) for entry in batch))
        os.fsync(self._idx)
        for blob, first, count, digest, crc in batch:
            self._records[blob] = (first, count, digest, crc)
            if blob in self._suspect:
                self._suspect.discard(blob)
                self._report['repaired'] += 1
            else:
                self._report[counter] += count

    # ------------------------------------------------------------------ quota

    def _room(self, size):
        """Whether ``size`` more bytes fit under the bound of all installs' arenas, reclaiming idle ones first."""
        if self._others is None:
            self._others = self._other_arenas()
        own = PAGE + self._rows * self._width
        total = own + sum(used for _path, used, _seen in self._others)
        if total + size > types.GLOBAL_BYTES:
            idle = sorted((item for item in self._others if self._now() - item[2] > types.IDLE_SECONDS),
                          key=lambda item: item[2])
            for item in idle:
                if total + size <= types.GLOBAL_BYTES:
                    break
                _remove_arena(item[0])
                self._others.remove(item)
                total -= item[1]
        return total + size <= types.GLOBAL_BYTES

    def _other_arenas(self):
        """``[(arena directory, bytes, last use)]`` of the other installs beside this one's state root."""
        own = os.path.realpath(self._directory)
        installs = os.path.dirname(os.path.dirname(own))
        answer = []
        try:
            entries = list(os.scandir(installs))
        except OSError:
            return answer
        for entry in entries:
            path = os.path.join(entry.path, os.path.basename(own))
            if path == own or entry.is_symlink():
                continue
            try:
                fd = _private_directory(path)
            except OSError:
                continue
            try:
                files = _owned_files(fd)
                seen = max([mtime for name, _size, mtime in files if name == CURRENT] or [os.fstat(fd).st_mtime])
                answer.append((path, sum(size for _name, size, _mtime in files), seen))
            except OSError:
                continue
            finally:
                os.close(fd)
        return answer


def _remove_arena(path):
    """Remove another install's idle arena: ``current.json`` first, so its next recall reads "no arena"."""
    try:
        fd = _private_directory(path)
    except OSError:
        return
    try:
        names = [name for name, _size, _mtime in _owned_files(fd)]
        for name in sorted(names, key=lambda item: item != CURRENT):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass  # Unchecked: what stays is counted and tried again by the next pass over the bound.
    finally:
        os.close(fd)
