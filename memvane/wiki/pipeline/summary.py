"""Read-only counts of the wiki pipeline ledger and the hook edit inbox (``pipeline_summary``).

Nothing is created, locked or written: the ledger opens through
``open_pipeline(readonly=True)`` (``errors._query_only_uri`` and ``PRAGMA
query_only=ON``) only when the file exists, and the inbox is read without its
lock, so only newline-terminated lines count (a line an append is still writing
is left for the next read). No generation, deletion or resume decision reads
this summary; it is shown, never judged.

Writers of what is counted (every one is the real writer the tests drive):

- ``compile_jobs``/``verify_jobs``: ``queues.plan_jobs``/``_insert_job``,
  ``claim_*``, ``finish_claim``/``finish_verify_job``, ``recover_dead_claims``
  and retention (``JOB_STATES``).
- ``publish_txns``: ``publish.publish_card`` and ``recover_publishes``
  (``PUBLISH_STATES``; ``publishing`` is a publish stopped between its
  ledger row and its rename).
- ``refresh_events``: ``record_refresh``, ``absorb_refresh_events``, settle and
  resolve (``REFRESH_STATES``).
- ``dedup_pairs``: ``queue_dedup_pairs``/``finish_dedup_pair`` (``DEDUP_STATES``).
- ``suppressions``: ``set_suppression`` (one row per ``(kind, key)``).
- ``audit_events``: ``AuditLog.append``, ``finish_publish_txn`` and the 1.x
  migration (``AUDIT_KINDS``).
- ``edit-inbox.jsonl``: ``wiki/card`` ``record_pending_refresh`` (hooks); the
  compile worker renames it aside (``.consuming-*``) and absorbs it into
  ``refresh_events``.

Released ``v1.1.4`` and ``v1.1.5`` write the same schema (``user_version`` 1);
earlier releases have no ledger and read as ``absent``.

State table (every cell: zero paid calls, zero files created, ledger bytes unchanged):

| ledger                                   | result                                         |
|------------------------------------------|------------------------------------------------|
| no ``.memvane``/``compile`` directory    | ``ledger: absent``, every count 0              |
| directory without the database           | ``ledger: absent``, every count 0              |
| database a writer created, no rows       | ``ledger: ok``, every count 0                  |
| rows in any state (queued..dropped, ...) | ``ledger: ok``, counts per state/kind          |
| a writer holds an open write transaction | ``ledger: ok``, the committed counts           |
| publish stopped after its ledger row     | ``publishTxns.byState.publishing`` counts it   |
| foreign file (application id/version)    | ``ledger: unreadable``, ``pipeline_schema_invalid`` |
| symlinked ``.memvane``/``compile``/file  | ``ledger: unreadable``, ``unsafe_pipeline_path`` |

| inbox                                    | result                                         |
|------------------------------------------|------------------------------------------------|
| no file                                  | ``inbox: absent``, ``pending`` 0               |
| hook lines (blank lines ignored)         | ``pending`` = complete lines                   |
| last line without its newline            | not counted                                    |
| aside file of an interrupted absorb      | ``consuming`` = its complete lines             |
| over the writer's 8 MiB cap              | counted up to the cap, ``truncated``           |
| symlink, not a regular file, other owner | ``inbox: unreadable``                          |
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import sqlite3
import stat

from memvane.wiki.pipeline.types import SUMMARY_GROUPS, PipelineError

# The hook inbox of ``wiki/card`` (``memvane-edit-inbox-v1`` rows) and its writer's size cap.
_INBOX = 'edit-inbox.jsonl'
_CONSUMING = _INBOX + '.consuming-'
_INBOX_MAX_BYTES = 8 * 1024 * 1024
_MAX_ASIDE_FILES = 16
_KEYS = {'compile_jobs': 'compileJobs', 'verify_jobs': 'verifyJobs', 'publish_txns': 'publishTxns',
         'refresh_events': 'refreshEvents', 'dedup_pairs': 'dedupPairs', 'suppressions': 'suppressions',
         'audit_events': 'auditEvents'}


def _code(exc):
    text = str(exc)
    if isinstance(exc, PipelineError) and text.replace('_', '').isalnum():
        return text
    if isinstance(exc, OSError) and exc.errno in errno.errorcode:
        return errno.errorcode[exc.errno]
    return type(exc).__name__


def _table(table, found):
    column, known = SUMMARY_GROUPS[table]
    counts = {value: 0 for value in known}
    counts.update(found)
    return {'total': sum(found.values()), 'by' + column.capitalize(): counts}


def _ledger(project_root):
    from memvane.wiki.pipeline.store import open_pipeline
    found, status, reason = {}, 'ok', None
    try:
        with open_pipeline(project_root, readonly=True) as store:
            found = {table: store.group_counts(table) for table in SUMMARY_GROUPS}
    except FileNotFoundError:
        status = 'absent'
    except (PipelineError, OSError, sqlite3.Error) as exc:
        found, status, reason = {}, 'unreadable', _code(exc)
    out = {'ledger': status, **({'reason': reason} if reason else {})}
    out['tables'] = {_KEYS[table]: _table(table, found.get(table, {})) for table in SUMMARY_GROUPS}
    return out


def _lines(path):
    """``(complete non-blank lines, truncated)`` of one regular file this user owns, opened no-follow."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PipelineError('unsafe_inbox')
        chunks, left = [], _INBOX_MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    data = b''.join(chunks)
    truncated = len(data) > _INBOX_MAX_BYTES
    data = data[:_INBOX_MAX_BYTES]
    # The piece after the last newline is a line still being written (or nothing).
    return sum(1 for line in data.split(b'\n')[:-1] if line.strip()), truncated


def _inbox(project_root):
    out = {'inbox': 'absent', 'pending': 0, 'consuming': 0, 'files': 0, 'truncated': False}
    try:
        from memvane.wiki.pipeline.store import pipeline_db_path
        compile_dir = pipeline_db_path(project_root).parent
        if not compile_dir.is_dir():
            return out
        names = []
        with os.scandir(compile_dir) as entries:
            for entry in entries:
                if entry.name == _INBOX or entry.name.startswith(_CONSUMING):
                    names.append(entry.name)
        for name in sorted(names)[:_MAX_ASIDE_FILES + 1]:
            lines, truncated = _lines(compile_dir / name)
            out['pending' if name == _INBOX else 'consuming'] += lines
            out['files'] += 1
            out['truncated'] = out['truncated'] or truncated
        if len(names) > _MAX_ASIDE_FILES + 1:
            out['truncated'] = True
        if out['files']:
            out['inbox'] = 'ok'
    except FileNotFoundError:
        pass
    except (PipelineError, OSError) as exc:
        out = {'inbox': 'unreadable', 'reason': _code(exc), 'pending': 0, 'consuming': 0, 'files': 0,
               'truncated': False}
    return out


def pipeline_summary(project_root: Path | str) -> dict:
    """``{ledger, [reason], tables, inbox}`` counts of ``project_root``'s compile state; never writes.

    ``ledger`` and ``inbox.inbox`` are ``ok``, ``absent`` or ``unreadable`` (with a
    fixed ``reason`` code). ``tables`` always lists every table, with every known
    state (or kind) at 0 when nothing is counted.
    """
    summary = _ledger(project_root)
    summary['inbox'] = _inbox(project_root)
    return summary
