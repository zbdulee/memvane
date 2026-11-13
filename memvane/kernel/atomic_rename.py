"""Atomic rename within a directory without replacing an existing name."""
import ctypes
import os
import sys


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
        from memvane.journal.install_journal import JournalEvidenceError
        raise JournalEvidenceError('reader_atomic_create_unavailable')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    return function, flag


def rename_noreplace(parent_fd, source, target):
    function, flag = _native_renamer()
    if function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(target), flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)
