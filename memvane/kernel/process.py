"""Stop only the process group owned by a spawned child."""
import os
import signal
import subprocess
import time

STOP_WAIT = .5


def _refused_while_running(process):
    """True when a refused group signal means a member that cannot be signalled.

    macOS also refuses a group whose members are exiting or have exited and are
    not reaped yet: then the leader's exit status arrives within moments.
    """
    try:
        process.wait(timeout=STOP_WAIT)
    except subprocess.TimeoutExpired:
        return True
    return False


def _group_gone(pid):
    """Kill the group once more; False when it stays refused past ``STOP_WAIT``
    (exited descendants of an exited leader are reaped within moments)."""
    deadline = time.monotonic() + STOP_WAIT
    while True:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)
        else:
            return True


def stop_owned(process):
    # Popen creates a new group. Never discover/kill unrelated system processes.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        if _refused_while_running(process):
            return False
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    # A descendant may keep pipes open even after the group leader exits.
    return _group_gone(process.pid)


def stop_owned_checked(process,*,emergency=False):
    """Reap the owned leader AND prove its new process group is absent.

    Exited leaders can leave descendants behind. Denied identity or a lingering
    group is unknown, never successful cleanup. No foreign PID discovery.
    """
    if type(process.pid)is not int or process.pid<=1:raise ValueError('invalid_owned_trainer')
    try:
        try:os.killpg(process.pid,signal.SIGKILL if emergency else signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=STOP_WAIT);return True
        except PermissionError:
            if _refused_while_running(process):return False
        try:process.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            if emergency: return False
        # Leader exit alone says nothing about the remaining group members.
        if not emergency:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                process.wait(timeout=STOP_WAIT);return True
            except PermissionError:
                if _refused_while_running(process):return False
        process.wait(timeout=STOP_WAIT)
        deadline=time.monotonic()+STOP_WAIT
        while True:
            # A refusal here is a group of exited members not reaped yet: asked again.
            try:os.killpg(process.pid,0)
            except ProcessLookupError:return True
            except PermissionError:pass
            if time.monotonic()>=deadline:return False
            time.sleep(.01)
    except (OSError,subprocess.TimeoutExpired):return False
