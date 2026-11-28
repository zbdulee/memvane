"""UI-only operating system boundary: state paths, private files, lock, processes, browser.

The kernel stays unchanged: every OS call the dashboard needs that is not
already a kernel helper lives here, so a Windows port replaces one module.
A test process (``QMD_SETUP_GUARD_FIXTURE=1`` or a test runtime root) never
reads or writes the account's real home and never starts a browser.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

from memvane.kernel import memvane_paths
from memvane.ui.types import UI_DIR

MAX_PRIVATE_JSON = 64 * 1024
SANDBOX_ENV = ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CODEX_SANDBOX', 'GEMINI_SANDBOX', 'HERMES_SANDBOX')
HEADLESS_ENV = ('CLAUDE_HEADLESS', 'HERMES_HEADLESS')
TEXT_BROWSERS = ('lynx', 'w3m', 'links', 'elinks', 'browsh')
# A URL handed to an OS opener: only the characters the dashboard itself builds.
SAFE_URL = re.compile('(?:%s|%s)' % (re.escape('http://127.0.0.1'), re.escape('http://localhost'))
                      + r':[0-9]{1,5}/[A-Za-z0-9._~/#?&=%:-]*')


class UiStateRefused(ValueError):
    """The UI state directory cannot be used; the message is a fixed code."""


def test_process():
    return (os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
            or bool(os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')))


def account_home():
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError, OSError, RuntimeError):
        return None


def fixture_live_home(home=None):
    """A test process whose HOME is the account's real home: refused, never touched."""
    if not test_process():
        return False
    account = account_home()
    if account is None:
        return True
    try:
        return (Path(home) if home is not None else Path.home()).resolve() == account
    except (OSError, RuntimeError):
        return True


def ui_dir(home=None, *, create=False):
    """``<state base>/ui`` (0700 components); raises ``UiStateRefused`` or ``FileNotFoundError``."""
    if fixture_live_home(home):
        raise UiStateRefused('ui_fixture_live_home')
    try:
        return memvane_paths.ensure_state_dir(UI_DIR, home=home, create=create)
    except ValueError as exc:
        raise UiStateRefused('ui_state_unsafe') from exc


def read_private_json(path, *, max_bytes=MAX_PRIVATE_JSON):
    """The JSON object of an owner-only regular file (no symlink), or None when absent.

    Raises ``UiStateRefused`` when the file is unsafe or not a JSON object.
    """
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UiStateRefused('ui_file_unsafe') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > max_bytes):
            raise UiStateRefused('ui_file_unsafe')
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UiStateRefused('ui_file_malformed') from exc
    if not isinstance(value, dict):
        raise UiStateRefused('ui_file_malformed')
    return value


def write_private_json(path, value):
    """Atomically replace ``path`` with ``value`` as a 0600 file in its own directory."""
    path = Path(path)
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def remove_file(path):
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


@contextmanager
def server_lock(path, *, wait_seconds=15.0):
    """Exclusive ``flock`` of ``path`` (0600, created); raises ``TimeoutError`` after ``wait_seconds``."""
    import fcntl
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise UiStateRefused('ui_lock_unsafe')
        end = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError('ui_lock_busy') from None
                time.sleep(0.05)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_command(pid):
    """The command line of a live process, or None."""
    if not pid_alive(pid):
        return None
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def is_ui_server(pid, bundle_root):
    """A live process that runs ``entry/run.py ui serve`` of ``bundle_root``."""
    command = process_command(pid)
    if not command or not isinstance(bundle_root, str):
        return False
    return ' ui serve' in command and str(Path(bundle_root) / 'entry' / 'run.py') in command


def terminate(pid):
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def spawn_detached(argv, *, env=None):
    """Start ``argv`` detached from this process; returns the child pid without waiting."""
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'close_fds': True, 'env': env, 'shell': False}
    if os.name == 'nt':
        options['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                                    | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    else:
        options['start_new_session'] = True
    return subprocess.Popen(list(argv), **options).pid


def python_argv(script, *args):
    """``sys.executable -B <script> <args>``: no bytecode in a host plugin cache."""
    return [sys.executable, '-B', str(script), *map(str, args)]


def browser_blocker(environ=None):
    """Why a browser must not be started here (a fixed code), or None."""
    env = os.environ if environ is None else environ
    if env.get('QMD_SETUP_GUARD_FIXTURE') == '1' or env.get('MEMVANE_TEST_RUNTIME_ROOT'):
        return 'test_process'
    if any(env.get(name) for name in SANDBOX_ENV):
        return 'sandbox'
    if any(env.get(name) == '1' for name in HEADLESS_ENV) or env.get('CI'):
        return 'headless'
    if env.get('SSH_CONNECTION') or env.get('SSH_TTY'):
        return 'ssh'
    if env.get('TERM') == 'dumb' and not sys.platform.startswith('darwin'):
        return 'headless'
    browser = Path(env.get('BROWSER', '').split(os.pathsep)[0].split(' ')[0]).name
    if browser in TEXT_BROWSERS:
        return 'text_browser'
    if (sys.platform.startswith('linux') and not is_wsl()
            and not env.get('DISPLAY') and not env.get('WAYLAND_DISPLAY')):
        return 'no_display'
    return None


def is_wsl():
    if not sys.platform.startswith('linux'):
        return False
    try:
        with open('/proc/version', 'rb') as handle:
            return b'microsoft' in handle.read(4096).lower()
    except OSError:
        return False


def open_browser(url):
    """``(opened, reason)``: the default browser opens ``url``; never through a shell."""
    blocked = browser_blocker()
    if blocked:
        return False, blocked
    if not SAFE_URL.fullmatch(url):
        return False, 'unsafe_url'
    if is_wsl():
        for argv in (['wslview', url],
                     ['powershell.exe', '-NoProfile', '-Command', 'Start-Process', url]):
            try:
                spawn_detached(argv)
                return True, None
            except OSError:
                continue
        return False, 'open_failed'
    try:
        import webbrowser
        opened = webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001 - any opener failure falls back to the manual URL
        return False, 'open_failed'
    return (True, None) if opened else (False, 'open_failed')
