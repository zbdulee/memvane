"""Compare persisted filesystem evidence with live observations.

APFS device numbers are boot-local. Only the explicitly named device fields
are masked; callers retain the original records and their content digests.
Live fd/path and before/after observations must use their full identities.
"""
from __future__ import annotations

from collections.abc import Mapping


def persistent_identity_equal(stored, live, *, device_index=None, device_fields=('dev',)):
    if device_index is not None:
        return (isinstance(stored, (list, tuple)) and isinstance(live, (list, tuple))
                and len(stored) == len(live) and 0 <= device_index < len(stored)
                and all(a == b for i, (a, b) in enumerate(zip(stored, live))
                        if i != device_index))
    return (isinstance(stored, Mapping) and isinstance(live, Mapping)
            and stored.keys() == live.keys()
            and all(stored[key] == live[key] for key in stored if key not in device_fields))


def persistent_node_equal(stored, live):
    return (isinstance(stored, (list, tuple)) and len(stored) == 2
            and persistent_identity_equal(stored, live, device_index=0))


def persistent_provider_equal(stored, live):
    return (isinstance(stored, (list, tuple)) and isinstance(live, (list, tuple))
            and len(stored) == len(live)
            and all(isinstance(a, (list, tuple)) and len(a) == 6
                    and persistent_identity_equal(a, b, device_index=1)
                    for a, b in zip(stored, live)))
