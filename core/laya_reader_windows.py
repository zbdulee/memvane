"""Reader lifetime descriptor held by a writer."""
import fcntl
import math
import os
import laya_journal_reader as R
import laya_reader_lease as L


class ReaderWindowBusy(ValueError):
    pass


def _deadline(value):
    R._need(type(value) in (float, int) and math.isfinite(value),
            'reader_window_deadline_invalid')
    return value


class _Lifetime:
    """An independent SH descriptor, never borrowed from a caller context."""
    def __init__(self, install_id):
        self.install_id = install_id
        self.parent, self.parent_fd, self.parent_info = L._parent(install_id, create=False)
        self.fd = None
        try:
            self.fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NONBLOCK |
                              os.O_NOFOLLOW, dir_fd=self.parent_fd)
            L._private_file(os.fstat(self.fd))
            try:
                fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReaderWindowBusy('reader_lifetime_busy') from exc
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        L._named(self.install_id, self.parent, self.parent_fd, self.parent_info, self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None
