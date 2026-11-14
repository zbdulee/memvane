"""Compile intents consumed into compiled attestations during a schema3 view pass.

The verify worker leaves one private intent per verified card in
``<install state root>/provenance-intents/``. A view pass records the
intent's observed fingerprints, ``lines-v1`` spans and receipt in the store's
evidence tables (``card_attestations``, ``attestation_sources``) and renames
the file to ``.consumed``, ``.rejected`` or ``.superseded``. Member trust is
never changed: the compiled attestation is not a generation member.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from memvane.journal.types import CutoverBlocked, JournalEvidenceError
from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.kernel.errors import IndexContractError
from memvane.store import types as store_types
from memvane.sync.intent_ack import _ack_compile_intent

# Files a view pass opens. Witness tries and the supersede sweep share it.
INTENT_SCAN_LIMIT = 512
# Waiting intents compared with one consumed witness in a pass.
SWEEP_LIMIT = 255
# Consumed witnesses tried in a pass to find one that is this view's.
WITNESS_TRIES = 16
# Cursor views kept; a dropped view only restarts its rotation.
CURSOR_VIEWS = 256
CONFLICT_REASONS = frozenset(('card_attestation_conflict', 'source_fingerprint_conflict'))
# Store reasons that judge the intent's own values. Any other store failure
# (full disk, I/O error, binding or file change, busy) ends the pass and
# leaves the intent pending for a later pass.
VALUE_REASONS = frozenset((
    'invalid_fields', 'invalid_compile_result', 'invalid_text', 'invalid_identifier',
    'invalid_integer', 'invalid_relative_path', 'invalid_canonical_value',
    'invalid_canonical_json', 'noncanonical_json', 'invalid_repository_binding',
    'source_spans_invalid', 'source_branch_invalid', 'source_state_invalid',
    'source_head_invalid', 'duplicate_source_fingerprint', 'attestation_sources_invalid',
    'verification_kind_invalid', 'receipt_invalid', 'receipt_prior_invalid', 'invalid_verdict'))
# Keys every compile intent carries (1.x contract). A later 1.x producer may
# add keys; they are ignored and never enter the digest or the store.
REQUIRED_KEYS = ('kind', 'viewId', 'targetPath', 'cardSha', 'expectedTargetHash',
                 'sourceFingerprints', 'sourceSpans', 'verifiedMode', 'verifyEngine',
                 'producingEngine', 'verdict_digest', 'result_digest')
_PENDING = re.compile(r'compile-[0-9a-f]{64}\.json')
_CONSUMED = re.compile(r'compile-[0-9a-f]{64}\.consumed\.json')


def _private(fd):
    """The descriptor's file, if it is a settled private intent file."""
    info = os.fstat(fd)
    # A writer links its temp file into place (nlink 2 until it unlinks the
    # temp): such a file is not settled yet and is left for a later pass.
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 65536):
        raise ValueError('provenance_intent_unsafe')
    return info


def _read(fd):
    """One canonical intent from the descriptor that will be acknowledged."""
    info = _private(fd)
    raw = os.read(fd, 65537)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('provenance_intent_duplicate')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except RecursionError as exc:
        raise ValueError('provenance_intent_invalid') from exc
    if len(raw) != info.st_size or not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError('provenance_intent_invalid')
    return value, info


def _required(intent):
    """The intent's 1.x keys (KeyError when one is missing); extra keys are dropped."""
    return {key: intent[key] for key in REQUIRED_KEYS}


def _fingerprints(intent):
    rows = intent['sourceFingerprints']['fingerprints']
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError('provenance_value_invalid')
    return rows


def _named(name, intent, suffix):
    from memvane.wiki.verify import api as wiki_verify
    return name == 'compile-' + wiki_verify.compile_intent_id(intent) + suffix


def _stored(store, handle, intent):
    """A consumed file is a witness only if its exact evidence is stored for this view."""
    from memvane.store import api as values
    intent = _required(intent)
    result = {key: value for key, value in intent.items()
              if key not in ('kind', 'verdict_digest', 'result_digest')}
    if (intent.get('kind') != 'compile' or intent['viewId'] != handle.view_id
            or values.compile_result_digest(result, intent['verdict_digest']) != intent['result_digest']):
        return False
    sources = []
    for fp in intent['sourceFingerprints']['fingerprints']:
        if (fp['worktree_id'], fp['branch_key']) != (handle.worktree_id, handle.branch_key):
            return False
        fingerprint_id = values.digest('memvane-source-fingerprint-v1',
                                       values.fingerprint(fp, store._repository['gitObjectFormat']))
        spans = [dict(span, kind='lines-v1') for span in intent['sourceSpans']
                 if span['path'] == fp['path'] and span['sourceSha256'] == fp['source_sha']]
        sources.append(dict(fingerprint_id=fingerprint_id, spans=spans))
    spec = values.attestation(dict(blob_sha=intent['cardSha'], verification_kind='compiled',
        engine_identity=intent['producingEngine'], policy_digest=handle.policy_digest,
        receipt=dict(schema='memvane-card-evidence-v2', verdict='verified',
                     result_digest=intent['result_digest'], prior_trust=None,
                     verdict_digest=intent['verdict_digest'], verified_mode=intent['verifiedMode'],
                     verify_engine=intent['verifyEngine']), sources=sources))
    try:
        return store.attestation(values.digest('memvane-card-attestation-v1', spec)) == spec
    except store_types.BlobStoreError:
        return False


def _open(path):
    return os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))


def _ack(path, fd, initial, outcome):
    """Rename one intent; a file replaced or settled meanwhile is left to a later pass."""
    try:
        _ack_compile_intent(path, fd, initial, outcome)
        return True
    except IndexContractError as exc:
        if str(exc) != 'provenance_intent_changed':
            raise
    except (FileExistsError, FileNotFoundError):
        # Settled by another writer (a verify supersede renamed it away).
        pass
    return False


def _witness(store, handle, directory, name):
    try:
        fd = _open(directory / name)
        try:
            intent, _ = _read(fd)
        finally:
            os.close(fd)
        if _named(name, intent, '.consumed.json') and _stored(store, handle, intent):
            return intent
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _sweep(store, handle, directory, names, entry, result, members):
    """Supersede waiting intents not newer than one consumed intent for the same view path.

    Returns the files opened. ``entry`` keeps this view's witness and the
    last waiting name compared with it, so a large queue is swept over passes.
    An intent for the card bytes this pass publishes is never superseded: it
    is consumable now, whatever the wall clock said when each was observed.
    """
    witnesses = sorted(item.name for item in os.scandir(directory) if _CONSUMED.fullmatch(item.name))
    if not witnesses:
        return 0
    name, after = entry.get('witness'), entry.get('after')
    if not isinstance(after, str) or name not in witnesses or entry.get('done') is True:
        name = next((item for item in witnesses if item > str(name or '')), witnesses[0])
        after = ''
    start = witnesses.index(name)
    opened, witness = 0, None
    for offset in range(min(WITNESS_TRIES, len(witnesses))):
        name = witnesses[(start + offset) % len(witnesses)]
        opened += 1
        witness = _witness(store, handle, directory, name)
        if witness is not None:
            break
        after = ''
    if witness is None:
        entry.update(witness=name, after='', done=True)
        return opened
    candidates = [item for item in names if item > after][:SWEEP_LIMIT]
    for candidate in candidates:
        path = directory / candidate
        opened += 1
        try:
            fd = _open(path)
            try:
                waiting, initial = _read(fd)
                member = members.get(waiting.get('targetPath'))
                if (not _named(candidate, waiting, '.json')
                        or waiting.get('viewId') != witness['viewId']
                        or waiting.get('targetPath') != witness['targetPath']
                        or waiting.get('cardSha') == witness['cardSha']
                        or (member is not None and member['blob_sha'] == waiting.get('cardSha'))
                        or waiting['sourceFingerprints']['observedNs']
                        > witness['sourceFingerprints']['observedNs']):
                    continue
                if _ack(path, fd, initial, 'superseded'):
                    result['superseded'] += 1
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    after = candidates[-1] if candidates else after
    entry.update(witness=name, after=after, done=not any(item > after for item in names))
    return opened


def _parity(intent, image):
    """The card's frontmatter claims exactly the intent's fingerprints and spans."""
    from memvane.wiki.card import api as wiki_card
    from memvane.wiki.card.api import card_support
    spans_api = card_support.source_spans
    block = spans_api._frontmatter_block(image.decode('utf8'))
    if block is None:
        return False
    revisions = wiki_card.frontmatter_source_revisions(block)
    span_state, spans = spans_api.read_card_spans(block)
    found = {(r['path'], r['sha256'], r['size']) for r in revisions}
    expected = {(fp['path'], fp['source_sha'], fp['byte_length'])
                for fp in intent['sourceFingerprints']['fingerprints']}
    return (found == expected and not spans_api.is_invalid(span_state)
            and spans == sorted(intent['sourceSpans'], key=lambda s: (s['path'], s['startLine'], s['endLine'])))


def consume(store, decision, handle, members, current, *, mark, validate, ingest_limit):
    """Consume this view's compile intents; return ``{consumed, rejected, waiting, superseded, unsafe}``.

    ``members`` and ``current`` are the pass's member rows and card bytes by
    path. ``mark`` is the pass's journal fence (called before the first store
    write) and ``validate`` rechecks the writer after each write; their
    failures and every store failure that does not judge the intent's values
    end the pass with the intent pending. At most ``ingest_limit`` intents try
    a store write; waiting ones do not count. ``unsafe`` counts files (or the
    directory) that are not settled private intents: they are never opened
    again in the pass and never renamed.
    """
    from memvane.journal import api as ledger
    from memvane.store import api as values

    result = {'consumed': 0, 'rejected': {}, 'waiting': 0, 'superseded': 0, 'unsafe': 0}
    state = ledger.reader_state(Path(decision['journalPath']).parent)
    directory = state / 'provenance-intents'
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return result
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        # An unsafe intent directory is never read; card publication goes on.
        result['unsafe'] += 1
        return result
    names = sorted(item.name for item in os.scandir(directory) if _PENDING.fullmatch(item.name))
    if not names:
        return result
    cursor_path = state / 'provenance-intents.cursor'
    try:
        cursor = ledger.ledger_read_json(cursor_path)
    except (FileNotFoundError, CutoverBlocked):
        # The cursor is only a rotation position: an unreadable one restarts it.
        cursor = {}
    views = cursor.get('views') if isinstance(cursor, dict) and isinstance(cursor.get('views'), dict) else {}
    saved = views.get(handle.view_id)
    entry = dict(saved) if isinstance(saved, dict) else {}
    budget = INTENT_SCAN_LIMIT - _sweep(store, handle, directory, names, entry, result, members)
    last = entry.get('last') if isinstance(entry.get('last'), str) else ''
    ordered = [name for name in names if name > last] + [name for name in names if name <= last]
    attempted, last_scanned = 0, None
    for name in ordered[:max(budget, 0)]:
        if attempted >= ingest_limit:
            break
        last_scanned = name
        path = directory / name
        try:
            fd = _open(path)
        except FileNotFoundError:
            continue
        except OSError:
            result['unsafe'] += 1  # a symlink or unreadable file under an intent name
            continue
        try:
            try:
                initial = _private(fd)
            except ValueError:
                result['unsafe'] += 1  # not settled or not private: a later pass looks again
                continue
            outcome = reason = None
            # Only the intent's own content is judged here; a failure rejects it.
            try:
                intent, initial = _read(fd)
                if not _named(name, intent, '.json'):
                    raise ValueError('provenance_intent_name_mismatch')
                if intent.get('viewId') != handle.view_id:
                    continue
                if any((fp.get('worktree_id'), fp.get('branch_key'))
                       != (handle.worktree_id, handle.branch_key) for fp in _fingerprints(intent)):
                    continue
                target = intent['targetPath']
                values.relative(target)
                member, image = members.get(target), current.get(target)
                if (member is None or image is None or member['blob_sha'] != intent['cardSha']
                        or image[0] != intent['cardSha']):
                    result['waiting'] += 1
                    continue
                if not _parity(intent, image[1]):
                    raise ValueError('provenance_parity_mismatch')
                required = _required(intent)
            except (AttributeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
                outcome, reason = 'rejected', ('provenance_parity_mismatch'
                                               if str(exc) == 'provenance_parity_mismatch'
                                               else 'provenance_value_invalid')
            if outcome is None:
                attempted += 1
                mark()  # fence failure ends the pass; the intent stays pending
                try:
                    store.put_compiled_attestation(handle.worktree_id, required,
                                                   policy_digest=handle.policy_digest)
                except store_types.BlobStoreError as exc:
                    if exc.reason in CONFLICT_REASONS:
                        outcome, reason = 'rejected', 'provenance_conflict'
                    elif exc.reason in VALUE_REASONS:
                        outcome, reason = 'rejected', 'provenance_value_invalid'
                    else:
                        raise
                else:
                    validate()
                    outcome = 'consumed'
            if not _ack(path, fd, initial, outcome):
                continue
            if outcome == 'consumed':
                result['consumed'] += 1
            else:
                result['rejected'][reason] = result['rejected'].get(reason, 0) + 1
        finally:
            os.close(fd)
    if last_scanned is not None:
        entry['last'] = last_scanned
    if entry != saved:
        kept = {key: value for key, value in views.items() if key != handle.view_id}
        kept = dict(sorted(kept.items())[:CURSOR_VIEWS - 1])
        try:
            ledger.ledger_write(cursor_path, {'views': {**kept, handle.view_id: entry}}, replace=True)
        except (OSError, ValueError, CutoverBlocked):
            pass  # rotation position only: a failed write repeats this window next pass
    return result
