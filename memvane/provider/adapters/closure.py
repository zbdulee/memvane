"""Provider dependency closure layout."""
from __future__ import annotations

import os
from pathlib import Path

from memvane.provider.types import BundleEvidenceError


def _fail(reason):
    raise BundleEvidenceError(reason)


def closure_layout(closure):
    """Path-independent content of a native closure: relative layout and bytes.

    Each row becomes ``[path relative to the rows' common directory, sha256,
    bytes]``. The same package moved to another directory has the same layout.
    """
    if (type(closure) is not list or not closure or
            any(type(row) is not dict or set(row) != {'path', 'sha256', 'bytes'} or
                type(row['path']) is not str or not Path(row['path']).is_absolute()
                for row in closure)):
        _fail('production_dependency_closure_invalid')
    base = os.path.commonpath([os.path.dirname(row['path']) for row in closure])
    return sorted([os.path.relpath(row['path'], base), row['sha256'], row['bytes']]
                  for row in closure)
