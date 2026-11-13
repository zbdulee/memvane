"""Resident service failure classifications."""
from __future__ import annotations

TRANSIENT_FAILURES = frozenset(('startup_failed', 'worker_exited'))
EMBED_POLICY = {'maxRssMib': 2048, 'minAvailableMib': 768,
                'maxLoadPerCpu': 1, 'targetHeadroomFraction': .35}
