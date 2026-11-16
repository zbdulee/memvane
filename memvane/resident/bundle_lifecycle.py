"""Resident bundle version ordering and successor wait."""
from memvane.kernel.memvane_version import version_key


def newer(candidate, current):
    """An equal version never replaces a service from another host cache."""
    if not isinstance(current, dict):
        return True
    left, right = version_key(candidate.get('version')), version_key(current.get('version'))
    return left is not None and (right is None or left > right)


def wait_for_successor(stopped, spawn):
    """Keep the old service lease until a successor is installed or already waiting."""
    for _attempt in range(120):
        if stopped.is_set():
            return
        try:
            spawn()
            return
        except (OSError, ValueError):
            stopped.wait(5)
