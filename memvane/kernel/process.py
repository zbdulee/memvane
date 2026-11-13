"""Stop only the process group owned by a spawned child."""
import os
import signal
import subprocess


def stop_owned(process):
    # Popen creates a new group. Never discover/kill unrelated system processes.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
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
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # The leader and its process group have already exited.
        pass
    except PermissionError:
        return False
    return True
