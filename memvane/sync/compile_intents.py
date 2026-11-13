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
CONFLICT_REASONS = frozenset(('card_attestation_conflict', 'source_fingerprint_conflict'))
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


def _named(name, intent, suffix):
    from memvane.wiki.verify import api as wiki_verify
    return name == 'compile-' + wiki_verify.compile_intent_id(intent) + suffix


def _stored(store, handle, intent):
    """A consumed file is a witness only if its exact evidence is stored for this view."""
    from memvane.store import api as values
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
    except FileExistsError:
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
    except (OSError, ValueError, TypeError, KeyError, store_types.BlobStoreError):
        pass
    return None


def _sweep(store, handle, directory, names, entry, result):
    """Supersede waiting intents older than one consumed intent for the same view path.

    Returns the files opened. ``entry`` keeps this view's witness and the
    last waiting name compared with it, so a large queue is swept over passes.
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
                if (not _named(candidate, waiting, '.json')
                        or waiting.get('viewId') != witness['viewId']
                        or waiting.get('targetPath') != witness['targetPath']
                        or waiting.get('cardSha') == witness['cardSha']
                        or waiting['sourceFingerprints']['observedNs']
                        >= witness['sourceFingerprints']['observedNs']):
                    continue
                if _ack(path, fd, initial, 'superseded'):
                    result['superseded'] += 1
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, KeyError):
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


def consume(store, decision, handle, members, current, *, mark, validate, passthrough, ingest_limit):
    """Consume this view's compile intents; return ``{consumed, rejected, waiting, superseded}``.

    ``members`` and ``current`` are the pass's member rows and card bytes by
    path. ``mark`` is the pass's journal fence (called before the first store
    write), ``validate`` rechecks the writer after each write and
    ``passthrough`` names store reasons that end the pass instead of
    rejecting an intent (binding changes, transient conflicts). At most
    ``ingest_limit`` intents try a store write; waiting ones do not count.
    """
    from memvane.journal import api as ledger
    from memvane.journal.api import _reader as reader
    from memvane.store import api as values

    result = {'consumed': 0, 'rejected': {}, 'waiting': 0, 'superseded': 0}
    state = reader._state(Path(decision['journalPath']).parent)
    directory = state / 'provenance-intents'
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return result
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise IndexContractError('provenance_intent_directory_unsafe')
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
    budget = INTENT_SCAN_LIMIT - _sweep(store, handle, directory, names, entry, result)
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
        try:
            try:
                initial = _private(fd)
            except ValueError:
                continue
            try:
                intent, initial = _read(fd)
                if not _named(name, intent, '.json'):
                    raise ValueError('provenance_intent_name_mismatch')
                if intent.get('viewId') != handle.view_id:
                    continue
                fingerprints = intent['sourceFingerprints']['fingerprints']
                if (not fingerprints or any((fp.get('worktree_id'), fp.get('branch_key'))
                                            != (handle.worktree_id, handle.branch_key) for fp in fingerprints)):
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
                attempted += 1
                mark()
                store.put_compiled_attestation(handle.worktree_id, intent, policy_digest=handle.policy_digest)
                validate()
                outcome = 'consumed'
            except (JournalEvidenceError, IndexContractError):
                # Fence or writer proof: the pass ends, the intent stays pending.
                raise
            except store_types.BlobStoreError as exc:
                if exc.reason in passthrough:
                    raise
                outcome, reason = 'rejected', ('provenance_conflict' if exc.reason in CONFLICT_REASONS
                                               else 'provenance_value_invalid')
            except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                outcome, reason = 'rejected', ('provenance_parity_mismatch'
                                               if str(exc) == 'provenance_parity_mismatch'
                                               else 'provenance_value_invalid')
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
        # Rotation position only: a failed write repeats this window next pass.
        ledger.ledger_write(cursor_path, {'views': {**views, handle.view_id: entry}}, replace=True)
    return result
