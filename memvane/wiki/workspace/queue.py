"""Queue claim and read over the ledger queues (source, verify)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    force_dead = os.environ.get("QMD_FORCE_DEAD_PIDS", "").split(",")
    if str(pid) in force_dead:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def read_queue(path: Path):
    """Queued and claimed rows of a ledger queue (source or verify), or its claim's rows."""
    from memvane.wiki.workspace import ledger_queue
    if isinstance(path, ledger_queue.LedgerClaim):
        return ledger_queue.LedgerRows(path)
    if path is None or ledger_queue.kind(path) is None:
        return []
    return ledger_queue.live(path)


def claim_queue(path: Path, *, receipt_writer=None):
    from memvane.wiki.workspace import ledger_queue
    if ledger_queue.kind(path) is None:
        raise ValueError('not_a_ledger_queue')
    return ledger_queue.claim(path, receipt_writer=receipt_writer)


def _parse_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
