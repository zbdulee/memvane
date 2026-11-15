"""Selected Laya source-folder exclusions; legacy QMD recall is unchanged.

The legacy skipPaths strings are substrings.  Apply them only to the parent
directory of a raw source here, so ``build`` excludes ``docs/build/a.md`` but
does not accidentally exclude ``docs/build-notes.md``.  This deliberately does
not interpret glob syntax or inspect a wiki card's own output path.
"""

from pathlib import PurePosixPath


def excluded_source(path: str, skip_paths) -> bool:
    if not isinstance(path, str) or not isinstance(skip_paths, (list, tuple)):
        return False
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return False
    directory = '/'.join(parts[:-1]) + '/'
    return any(isinstance(skip, str) and skip and skip in directory
               for skip in skip_paths)
