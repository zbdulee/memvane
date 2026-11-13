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
