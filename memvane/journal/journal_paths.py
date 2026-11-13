"""UID-wide journal coordination, independent of host HOME and learning moves."""
import os
from pathlib import Path
import pwd
import stat

from memvane.kernel import memvane_paths


def check_journal_namespace(state):
    if 'MEMVANE_TEST_RUNTIME_ROOT' in os.environ:
        from runtime_lease import in_platform_temp
        if not in_platform_temp(state):
            raise ValueError('journal_test_state_unsafe')


def _test_root(root, create):
    from runtime_lease import in_platform_temp, platform_temp_roots
    if not root.is_absolute() or root.resolve(strict=False) != root or not in_platform_temp(root):
        raise ValueError('journal_test_root_unsafe')
    boundaries = platform_temp_roots()
    base = next(parent for parent in root.parents if parent in boundaries)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(base, flags)
    try:
        for part in root.relative_to(base).parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                raise ValueError('journal_test_root_unsafe')
        if os.fstat(fd).st_mode & 0o077 or not os.path.samestat(os.fstat(fd), root.lstat()):
            raise ValueError('journal_test_root_unsafe')
    finally:
        os.close(fd)


def journal_directory(*parts, create=False):
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if raw is None:
        if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
            raise ValueError('journal_test_root_missing')
        root = Path(pwd.getpwuid(os.getuid()).pw_dir)
        names = (*memvane_paths.STATE_PARTS, *parts)
    else:
        root = Path(raw)
        _test_root(root, create)
        names = ('memvane-journal', *parts)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('journal_root_unsafe')
    for index in range(len(names) + 1):
        info = root.lstat()
        mask = 0o077 if raw is not None or index > len(memvane_paths.STATE_PARTS) else 0o022
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & mask:
            raise ValueError('journal_root_unsafe')
        if index == len(names):
            return root
        part = names[index]
        if not part or part in ('.', '..') or '/' in part:
            raise ValueError('journal_root_unsafe')
        child = root / part
        if create:
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        root = child
