"""The executing plugin version and its ordering."""
import json
from pathlib import Path
import re


def plugin_version():
    path = Path(__file__).resolve().parent.parent / 'plugin.json'
    value = json.loads(path.read_text('utf-8'))['version']
    if not isinstance(value, str) or version_key(value) is None:
        raise ValueError('invalid_plugin_version')
    return value


def version_key(version):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?',
                         str(version))
    if match is None:
        return None
    pre = match.group(4)
    tail = (1,) if pre is None else (0, *[(0, int(part), '') if part.isdigit()
                                          else (1, 0, part) for part in pre.split('.')])
    return tuple(int(match.group(n)) for n in (1, 2, 3)) + (tail,)


def writer_floor_allows(floor):
    """Whether the running version may write at a validated minimum version."""
    required = version_key(floor) if isinstance(floor, str) else None
    running = version_key(plugin_version())
    if required is None or running is None:
        raise ValueError('invalid_writer_floor')
    return running >= required


def same_major(state_format):
    """The on-disk state format is the plugin's major version."""
    if type(state_format) is not int or state_format < 1:
        raise ValueError('invalid_state_format')
    running = version_key(plugin_version())
    if running is None:
        raise ValueError('invalid_plugin_version')
    return running[0] == state_format
