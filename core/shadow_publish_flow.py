"""Compose shadow_index backup/seal/publish with an injected claim ACK.

Nothing in the operating QMD route calls this module. The caller supplies an
explicit owner-private root that already holds `published-view.json`, an
explicit SQLite source, a candidate-only edit callback and an ACK callback.
This never selects an installed store, endpoint, queue or the shared-store
activation pointer, and never creates a pointer.

Guarantees, exactly:
- ACK runs only after `publish_candidate` returned the CAS decision, i.e. the
  private pointer was atomically replaced under its lock and the sealed
  candidate was rechecked under SQLite's writer reservation.
- Candidate failure, stale expected view, changed seal, busy lock and any
  exception (including a simulated crash, which propagates) never ACK.
- No journal is written. A crash or ACK failure after the CAS leaves the
  pointer advanced and the claim pending. A retry sees the view as superseded;
  `is_own_publication` proves ownership only when the caller kept the exact
  published view (its indexGeneration is random). Without it the caller
  cannot distinguish "ours landed, then was replaced" from "ours never
  landed" and must reprocess the claim instead of ACKing.
- After a private candidate is published, the next backup must use that
  candidate as its source. The initial pointer's source is caller-provided;
  operational source selection and provenance remain outside this adapter.
"""
from __future__ import annotations

import json
from pathlib import Path

import shadow_index
import shadow_ack_policy


POINTER = 'published-view.json'
PREPARED, PUBLISHED, REJECTED = 'prepared', 'published', 'rejected'
# The CAS may have replaced the pointer before publish_candidate raised (its
# post-rename seal recheck). The claim is not ACKed; the caller decides.
INDETERMINATE = 'indeterminate'


def _result(outcome, reason=None, **fields):
    row = {'outcome': outcome, 'reason': reason, 'view': None, 'candidate': None,
           'sealed': None, 'current': None, 'ack': 'not_called'}
    row.update(fields)
    return row


def read_pointer(root):
    """Read the explicit private pointer. Missing raises; nothing is created."""
    root = shadow_index._private_dir(root)
    pointer = shadow_index._private_file(root / POINTER)
    if pointer.stat().st_size > 65536:
        raise ValueError('invalid_shadow_index_pointer')
    current = json.loads(pointer.read_text())
    if not isinstance(current, dict) or set(current) != shadow_index.VIEW_KEYS:
        raise ValueError('invalid_shadow_index_pointer')
    return current


def _current_or_none(root):
    try:
        return read_pointer(root)
    except Exception:
        return None


def is_own_publication(current, published_view):
    """True only for the exact view this caller published (random generation)."""
    return (isinstance(current, dict) and isinstance(published_view, dict)
            and current == published_view)


def prepare(root, source, expected, content_revision, edit_candidate):
    """Back up `source`, let the caller edit only the candidate, then seal it.

    The new view is `expected` with the caller's contentRevision and the new
    candidate generation; owner identity is never invented here.
    """
    if (not isinstance(expected, dict) or set(expected) != shadow_index.VIEW_KEYS
            or not isinstance(content_revision, str) or not content_revision
            or content_revision == expected.get('contentRevision')
            or not callable(edit_candidate)):
        raise ValueError('invalid_shadow_publish_request')
    try:
        current = read_pointer(root)
    except FileNotFoundError:
        return _result(REJECTED, 'pointer_missing')
    except Exception as exc:
        return _result(REJECTED, 'pointer_unreadable:' + str(exc))
    if current != expected:
        # Advisory early exit; publish_candidate rechecks under its lock.
        return _result(REJECTED, 'stale_expected_view', current=current)
    # Once this private flow has published a candidate, the next backup must
    # start from exactly that published DB. Backing up the original source
    # again could pass CAS while silently losing an earlier publication.
    published_index = (Path(root) / 'candidates' /
                       expected['indexGeneration'] / 'index.sqlite')
    if published_index.exists() or published_index.is_symlink():
        try:
            shadow_index._private_file(published_index)
            if Path(source).resolve(strict=True) != published_index.resolve(strict=True):
                return _result(REJECTED, 'published_source_mismatch', current=current)
            shadow_index._private_file(Path(source))
        except (OSError, ValueError):
            return _result(REJECTED, 'published_source_unavailable', current=current)
    try:
        candidate = shadow_index.backup_inactive(source, root)
    except Exception as exc:
        return _result(REJECTED, 'candidate_backup_failed:' + str(exc), current=current)
    try:
        if edit_candidate(candidate['index']) is False:
            return _result(REJECTED, 'candidate_preparation_failed',
                           candidate=candidate, current=current)
    except Exception as exc:
        return _result(REJECTED, 'candidate_preparation_failed:' + str(exc),
                       candidate=candidate, current=current)
    try:
        sealed = shadow_index.seal_candidate(root, candidate['generation'])
    except Exception as exc:
        return _result(REJECTED, 'candidate_seal_failed:' + str(exc),
                       candidate=candidate, current=current)
    view = dict(expected, contentRevision=content_revision,
                indexGeneration=candidate['generation'])
    return _result(PREPARED, view=view, candidate=candidate, sealed=sealed,
                   expected=dict(expected), current=current)


def publish(root, prepared, acknowledge):
    """CAS a prepared view, then ACK. ACK failure keeps the publication."""
    if (not isinstance(prepared, dict) or prepared.get('outcome') != PREPARED
            or not callable(acknowledge)
            or prepared['view']['indexGeneration'] != prepared['candidate']['generation']):
        raise ValueError('invalid_shadow_publish_request')
    view, expected = prepared['view'], prepared['expected']
    base = {'view': view, 'candidate': prepared['candidate'], 'sealed': prepared['sealed']}
    try:
        decision = shadow_index.publish_candidate(root, expected, view, prepared['sealed'])
    except Exception as exc:
        current = _current_or_none(root)
        if current == view:
            return _result(INDETERMINATE, 'pointer_advanced_then_failed:' + str(exc),
                           current=current, **base)
        return _result(REJECTED, str(exc), current=current, **base)
    if decision is None:
        return _result(REJECTED, 'stale_expected_view',
                       current=_current_or_none(root), **base)
    try:
        acknowledge(dict(decision))
    except Exception as exc:
        return _result(PUBLISHED, current=decision, ack='failed',
                       ackError=str(exc), **base)
    return _result(PUBLISHED, current=decision, ack='acknowledged', **base)


def run(root, source, expected, content_revision, edit_candidate, acknowledge,
        *, claim=None, retain_receipt=None):
    """Prepare then publish; optionally retain an exact claim receipt first.

    A caller enabling recovery supplies both ``claim`` and a callback that
    durably stores the receipt before returning. If retention fails, the CAS
    is not attempted. This adapter never chooses a queue or receipt location.
    """
    if (claim is None) != (retain_receipt is None) or (
            retain_receipt is not None and not callable(retain_receipt)):
        raise ValueError('invalid_shadow_publish_receipt_callback')
    prepared = prepare(root, source, expected, content_revision, edit_candidate)
    if prepared['outcome'] != PREPARED:
        return prepared
    if claim is not None:
        receipt = shadow_ack_policy.build_receipt(claim, prepared)
        try:
            retain_receipt(receipt)
        except Exception as exc:
            return _result(REJECTED, 'receipt_retention_failed:' + str(exc),
                           view=prepared['view'], candidate=prepared['candidate'],
                           sealed=prepared['sealed'], current=prepared['current'])
    return publish(root, prepared, acknowledge)
