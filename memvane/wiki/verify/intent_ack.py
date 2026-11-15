"""Supersede one private compile intent after verification."""
import os
import stat


def _supersede_compile_intent(path):
    if path is None:
        return
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                        | getattr(os, 'O_NOFOLLOW', 0))
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            before = os.fstat(fd)
            current = path.lstat()
            parent = os.fstat(parent_fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                    or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                    or not os.path.samestat(parent, path.parent.lstat())):
                raise ValueError('provenance_intent_changed')
            from memvane.kernel.atomic_rename import AtomicRenameUnavailable, rename_noreplace
            try:
                rename_noreplace(parent_fd, path.name, path.stem + '.superseded.json')
            except AtomicRenameUnavailable as exc:
                from memvane.journal.types import JournalEvidenceError
                raise JournalEvidenceError(str(exc)) from exc
            os.fsync(parent_fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _compile_intent_id(result):
    from memvane.store import api as values
    return values.digest('memvane-compile-intent-id-v1', [
        'compile', result['viewId'], result['targetPath'], result['cardSha'],
        result['sourceFingerprints']['scanId']])
