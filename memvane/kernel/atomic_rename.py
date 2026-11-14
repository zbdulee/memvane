"""Atomic file writes and renames within a directory."""
import ctypes
import json
from pathlib import Path
import tempfile
import os
import shutil
import sys


class AtomicRenameUnavailable(ValueError):
    """The OS has no atomic no-replace rename primitive."""


def _native_renamer():
    library = ctypes.CDLL(None, use_errno=True)
    # macOS SDK sys/stdio.h: RENAME_EXCL = 0x00000004.
    # Linux UAPI include/uapi/linux/fs.h: RENAME_NOREPLACE = (1 << 0).
    if sys.platform == 'darwin':
        name, flag = 'renameatx_np', 4
    elif sys.platform.startswith('linux'):
        name, flag = 'renameat2', 1
    else:
        raise ValueError('reader_atomic_create_unavailable')
    function = getattr(library, name, None)
    if function is None:
        raise AtomicRenameUnavailable('reader_atomic_create_unavailable')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    return function, flag


def rename_noreplace(parent_fd, source, target):
    function, flag = _native_renamer()
    if function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def partial_path(target):
    """Where a new ``target`` is assembled before it is published."""
    target = Path(target)
    return target.with_name('.' + target.name + '.partial')


def discard_partial(path, *suffixes):
    """Remove a partial file a killed writer left (and its named siblings)."""
    for name in (str(path), *(str(path) + suffix for suffix in suffixes)):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def publish_new(temp, target):
    """Rename a complete ``temp`` onto ``target``, which must not exist.

    Raises FileExistsError when ``target`` exists; the directory entry is
    durable on return.
    """
    temp, target = Path(temp), Path(target)
    if temp.parent != target.parent:
        raise ValueError('publish_new_parent_mismatch')
    fd = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        rename_noreplace(fd, temp.name, target.name)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new_file(target, raw, mode=0o600):
    """Create ``target`` with exactly ``raw``: never a partial or replaced file.

    The bytes are written and fsynced beside it (``partial_path``), then
    published with a no-replace rename. The caller holds the lock that
    serializes writers of ``target``, so a partial file found there was left
    by a killed writer and is removed first. Raises FileExistsError when
    ``target`` exists.
    """
    target = Path(target)
    temp = partial_path(target)
    discard_partial(temp)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        publish_new(temp, target)
    except BaseException:
        discard_partial(temp)
        raise


MAX_FILE = 16 * 1024 * 1024


def atomic_json(path, value):
    path = Path(path)
    # Same bytes as context_learning.store.canonical plus a trailing newline.
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'))+'\n').encode()
    if len(data) > MAX_FILE: raise ValueError('output_budget_exceeded')
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def write_text_atomic(path: Path, text: str) -> bool:
    """Replace text atomically in the same directory, preserving existing mode."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            # 원본이 없거나(신규 생성) 권한을 못 읽는 경우 — 기본 권한으로 진행한다.
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
