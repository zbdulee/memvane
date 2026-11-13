"""Acknowledge a private provenance intent by atomic rename."""
import os

from memvane.kernel.errors import IndexContractError
from memvane.journal.install_journal import JournalEvidenceError


def _ack_compile_intent(path, fd, initial, outcome):
    """Rename only the exact private file read by this process, without replacement."""
    from memvane.kernel.atomic_rename import AtomicRenameUnavailable, rename_noreplace
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        parent = os.fstat(directory_fd)
        now, after = path.lstat(), os.fstat(fd)
        identity = lambda node: (node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns, node.st_ctime_ns)
        if (not os.path.samestat(parent, path.parent.lstat()) or identity(initial) != identity(now)
                or identity(initial) != identity(after)):
            raise IndexContractError('provenance_intent_changed')
        try:
            rename_noreplace(directory_fd, path.name, path.stem + '.' + outcome + '.json')
        except AtomicRenameUnavailable as exc:
            raise JournalEvidenceError(str(exc)) from exc
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
