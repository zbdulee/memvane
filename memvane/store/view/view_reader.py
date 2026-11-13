"""Pinned lexical reads of immutable candidate generations, never live authority."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import sqlite3
import time
import uuid

from memvane.store.blob import blob_values as v
from memvane.store.blob.blob_values import BlobStoreError
from memvane.store.blob.blob_store import _worktree_record
from memvane.store.checkout import checkout_view as checkout
from memvane.store.view import view_fts as fts
from memvane.store.view import view_values as vals

MAX_PINS = 128


@dataclass(frozen=True)
class ViewPin:
    view_id: str
    gen_id: str
    epoch: int
    incarnation: str
    token: str


def _remaining(deadline):
    if time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')


def _sqlite_interrupted(exc):
    if isinstance(exc, BlobStoreError):
        if exc.reason != 'candidate_io_failed':
            return False
        # _session wraps SQLite failures while preserving their exception link.
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code == getattr(sqlite3, 'SQLITE_INTERRUPT', 9)
    # Python 3.9 does not expose the numeric SQLite error code.
    return exc.args == ('interrupted',)


def _query(query, limit, prefixes, exclusions, seconds):
    if (not isinstance(query, str) or not query or type(limit) is not int
            or not 1 <= limit <= 100 or type(seconds) not in (int, float)):
        v.fail('fts_query_invalid')
    try:
        if not math.isfinite(seconds) or not 0 < seconds <= 30 or len(query.encode('utf8')) > 4096:
            v.fail('fts_query_invalid')
    except (UnicodeError, OverflowError):
        v.fail('fts_query_invalid')
    for items, maximum in ((prefixes, 32), (exclusions, 256)):
        if items is not None:
            if type(items) is not list or len(items) > maximum:
                v.fail('fts_filter_invalid')
            for path in items:
                v.relative(path)
    return time.monotonic() + seconds


def _card_locations(db, gen_id, paths):
    """``{wiki_path: [locator, ...]}`` from one generation's card_sources rows."""
    from memvane.wiki.card import source_spans
    locations = {}
    sql = ('SELECT wiki_path,spans_json FROM card_sources WHERE gen_id=? AND wiki_path IN (' +
           ','.join('?' for _ in paths) + ') ORDER BY wiki_path,ordinal')
    for source in db.execute(sql, (gen_id, *paths)):
        for span in v.parse_canonical(source['spans_json']):
            if span.get('kind') not in (v.LEGACY_LINE_SPAN, v.LINE_SPAN):
                v.fail('legacy_card_trust_invalid')
            locations.setdefault(source['wiki_path'], []).append(span)
    for path, spans in locations.items():
        spans.sort(key=lambda span: (span['path'], span['startLine'], span['endLine']))
        locations[path] = [source_spans.locator(span) for span in spans]
    return locations


class ReaderMixin:
    @contextmanager
    def _reading(self, deadline):
        if self._closed:
            v.fail('view_store_closed')
        _remaining(deadline)
        query_only = self._db.execute('PRAGMA query_only').fetchone()[0]
        self._db.execute('PRAGMA query_only=ON').close()
        self._db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            with self._session() as db:
                yield db
                _remaining(deadline)
        except (BlobStoreError, sqlite3.Error) as exc:
            if _sqlite_interrupted(exc) and time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            raise
        finally:
            self._db.set_progress_handler(None, 0)
            if not query_only:
                self._db.execute('PRAGMA query_only=OFF').close()

    def _sealed_handle(self, db, generation, live_handle=None):
        capture = v.parse_canonical(generation['checkout_evidence'])
        if (not checkout._capture_shape(capture)
                or checkout._digest({k: x for k, x in capture.items() if k != 'checkoutDigest'}) != capture['checkoutDigest']
                or capture['repoId'] != self._repository['repoId']
                or capture['viewId'] != generation['view_id']):
            v.fail('generation_capture_invalid')
        saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (capture['worktreeId'],)).fetchone()
        if saved is None:
            v.fail('generation_capture_invalid')
        saved = dict(saved)
        record = _worktree_record(saved, self._repository, self._policy)
        evidence = capture['evidence']
        if (any(not checkout._persistent_directory_equal(record[k], evidence[k])
                for k in ('project', 'checkout', 'gitDir'))
                or not checkout._persistent_directory_equal(self._repository['commonGitDir'], evidence['commonGitDir'])
                or any(evidence[k] != self._repository[k] for k in ('projectRelative', 'gitObjectFormat'))):
            v.fail('generation_capture_invalid')
        handle = self._handle_type(view_id=capture['viewId'], worktree_id=capture['worktreeId'],
            branch_key=capture['branchKey'], head_oid=capture['headOid'],
            checkout_digest=capture['checkoutDigest'], policy_digest=self._policy,
            incarnation=self._incarnation, token='sealed')
        if live_handle is not None and (
                handle.view_id, handle.worktree_id, handle.branch_key) != (
                live_handle.view_id, live_handle.worktree_id, live_handle.branch_key):
            v.fail('generation_view_mismatch')
        return handle, dict(handle=handle, capture=capture, binding=saved['binding_digest']), saved


    def _commit_stamp(self, db):
        """Changes whenever any commit could have touched the store since the last stamp.

        ``data_version`` moves on another connection's commit, ``total_changes``
        on this connection's own writes and ``schema_version`` on DDL. Read
        inside a read transaction, so it describes that transaction's snapshot.
        """
        return (db.execute('PRAGMA main.data_version').fetchone()[0], self._db.total_changes,
                db.execute('PRAGMA main.schema_version').fetchone()[0])

    def _read_pinned(self, db, pin, deadline):
        """``_read_generation`` for a pin, reusing its last full verification.

        Sealed generation rows are immutable (schema triggers, FTS authorizer).
        While no commit at all reached the store since the pin's last full
        verification (same commit stamp), the database is the one that was
        verified, so its derived stage is reused after re-reading the
        generation and seal rows. Any commit, including an out-of-band one,
        re-runs the full verification and refreshes the reuse.
        """
        return self._rows_read_pinned(db, pin, deadline)

    def pin_view(self, handle, *, deadline=None):
        if self._closed:
            v.fail('view_store_closed')
        now=time.monotonic()
        if deadline is None:
            deadline=now+30
        elif type(deadline) not in (int,float) or not math.isfinite(deadline):
            v.fail('fts_query_invalid')
        else:
            deadline=min(deadline,now+30)
        _remaining(deadline)
        return self._rows_pin(handle, deadline)

    def _pinned(self, pin):
        if self._closed:
            v.fail('view_store_closed')
        record = self._pins.get(pin.token) if type(pin) is ViewPin and isinstance(pin.token, str) else None
        if (record is None or record['pin'] is not pin or pin.incarnation != self._incarnation
                or (pin.view_id, pin.gen_id, pin.epoch, pin.incarnation, pin.token) != record['fields']):
            v.fail('view_pin_invalid')
        return record

    def release(self, pin):
        self._pinned(pin)
        del self._pins[pin.token]

    def _snapshot_state(self, current, original, deadline):
        if current['checkoutDigest'] == original['checkoutDigest']:
            return 'published_snapshot'
        if current['headOid'] == original['headOid']:
            return 'stale_snapshot'
        if current['headOid'] is None or original['headOid'] is None:
            return 'lineage_unverified'
        _remaining(deadline)
        try:
            code, _ = checkout._git(['merge-base', '--is-ancestor', original['headOid'], current['headOid']],
                current['projectRoot'], deadline, ok=(0, 1))
        except checkout.CheckoutUnavailable:
            _remaining(deadline)
            return 'lineage_unverified'
        return 'stale_snapshot' if code == 0 else 'lineage_changed'

    def search(self, pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None,
               deadline_seconds=2):
        return self._rows_search(pin, fts_query, limit, path_prefixes=path_prefixes,
                                 excluded_paths=excluded_paths, deadline_seconds=deadline_seconds)

    def card_locations(self, pin, paths, *, deadline_seconds=2):
        """Source line locators of pinned members, from stored card provenance only.

        Each generation answers from its own ``card_sources`` rows; no source file is opened. Only line spans
        (``legacy-lines-v1`` and the compiled chain's ``lines-v1``) are shown,
        in one locator format; any other kind, byte spans included, refuses
        the read. A path without spans is absent from the result.
        """
        return self._rows_card_locations(pin, paths, deadline_seconds)

    def verify_views(self):
        v.fail('row_format_unsupported')
