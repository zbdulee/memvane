"""Owner-private root selection for runtime coordination and inference."""
import os
from pathlib import Path
import sys

from memvane.kernel import memvane_paths
from memvane.runtime.runtime_lease import private_state
from memvane.runtime.types import ServiceFailure

TEST_ROOT_ENV = 'MEMVANE_TEST_RUNTIME_ROOT'


def _check_root(root, home):
    for part in (root, *root.parents):
        if part == home:
            break
        if part.is_symlink():
            raise ServiceFailure('unsafe_root')
        if part.exists():
            info = part.stat()
            if not part.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ServiceFailure('unsafe_root')


def _test_root(create):
    """The test-only runtime root, or None; unsafe values never fall back."""
    raw = os.environ.get(TEST_ROOT_ENV)
    if not raw:
        return None
    from memvane.runtime.api import in_platform_temp
    base = Path(raw)
    if not base.is_absolute() or not in_platform_temp(base):
        raise ServiceFailure('unsafe_test_root')
    base = Path(os.path.realpath(base))
    root = base / 'laya-inference'
    if create:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.mkdir(mode=0o700, exist_ok=True)
    return private_state(root)


def service_root(*, create=False, home=None):
    # Host HOME/CODEX_HOME overrides must not create a second user service.
    if home is None:
        test_root = _test_root(create)
        if test_root is not None:
            return test_root
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    home = Path(home).resolve()
    root = memvane_paths.state_base(home) / 'wake'
    _check_root(root, home)
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return private_state(root)
