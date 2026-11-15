"""Preserve the wiki directory across Git checkouts."""
import os
import stat


def ensure_wiki_keeper(wiki):
    directory = os.open(wiki, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open('.memvane-root', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=directory)
        except FileExistsError:
            info = os.stat('.memvane-root', dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('wiki_keeper_unsafe')
        else:
            os.close(fd)
            os.fsync(directory)
    finally:
        os.close(directory)
