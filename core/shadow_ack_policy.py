"""Pure retry decision for a claim whose ACK around the private CAS is uncertain.

Nothing in the operating QMD route calls this module, and it performs no I/O.
`shadow_publish_flow.publish` ACKs only after `publish_candidate` returned the
CAS decision. When the process dies or the ACK callback fails in between, the
claim stays pending and a later run must decide what to do with it. This
module is that decision, given facts the caller observed:

- `receipt`: the caller-retained pre-CAS receipt (`build_receipt`), holding the
  exact claim identity/digest, the expected view, the prepared view (whose
  `indexGeneration` is random) and the prepared candidate's seal.
- `current_view`: the private pointer as read now, or None if unreadable.
- `current_claim`: the pending claim as read now `{id, digest}`, or None.
- `candidate_seal`: proof that the prepared candidate still matches the seal.
  Either the fingerprint observed now (compared here with the receipt seal),
  exactly True/False, or one of SEAL_MATCH/SEAL_MISMATCH/SEAL_UNKNOWN.

Outcomes are only ACK (the exact claim), REPROCESS (prepare again from the
current pointer) or DEFER (stop; investigation needed). Every result carries
an explicit reason.

Guarantees, exactly:
- ACK requires a well-formed receipt, the same claim id and digest, a
  readable pointer equal to the receipt's prepared view, and a seal match.
  The receipt alone never proves the CAS landed; the random indexGeneration
  in an exact current==prepared match does, and the seal proves the published
  candidate bytes are the ones prepared.
- current==expected means the CAS did not land (or was never attempted):
  REPROCESS, never ACK.
- A pointer that moved elsewhere within the same owner/registration is
  REPROCESS: "ours landed, then was superseded" and "another publisher won
  first" are indistinguishable without a durable journal, and both are safe to
  re-prepare from the current view. Anomalies (foreign identity, reused
  generation, colliding contentRevision) are DEFER.
- The verdict is a snapshot. The caller's ACK must itself be conditional on
  the exact claim id and digest returned here, and REPROCESS must be
  idempotent against a view that may already contain this claim's effect.

Unavoidable case: this is not a durable journal. If the process dies before
the receipt is durably retained, no later run can tell whether its CAS landed;
`decide(None, ...)` therefore DEFERs (`receipt_missing`) and operational
recovery remains blocked until a durable pre-CAS receipt (or a claim/manifest
transaction) exists. Retaining the receipt durably is the caller's job.
"""
from __future__ import annotations

import copy
import re

import published_view
import shadow_index


RECEIPT_SCHEMA = 'qmd-shadow-ack-receipt-v1'
SEAL_SCHEMA = 'qmd-source-snapshot-v1'
ACK, REPROCESS, DEFER = 'ack', 'reprocess', 'defer'
SEAL_MATCH, SEAL_MISMATCH, SEAL_UNKNOWN = 'match', 'mismatch', 'unknown'
RECEIPT_KEYS = frozenset(('schema', 'claim', 'expected', 'prepared', 'seal'))
CLAIM_KEYS = frozenset(('id', 'digest'))
SEAL_KEYS = frozenset(('schema', 'identity', 'snapshotSha256'))
# Owner/registration fields a retry may not cross; contentRevision and
# indexGeneration are what a publication advances.
IDENTITY_KEYS = ('owner', 'incarnation', 'worktree', 'scopeRevision', 'storeGeneration')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
MAX_CLAIM_ID = 256


def _result(decision, reason, claim=None, current=None):
    return {'decision': decision, 'reason': reason,
            'ack': dict(claim) if decision == ACK else None,
            'current': copy.deepcopy(current)}


def _valid_claim(claim):
    return (isinstance(claim, dict) and set(claim) == CLAIM_KEYS
            and isinstance(claim['id'], str) and 0 < len(claim['id']) <= MAX_CLAIM_ID
            and isinstance(claim['digest'], str) and SHA256.fullmatch(claim['digest']) is not None)


def _valid_view(view):
    return (isinstance(view, dict) and set(view) == shadow_index.VIEW_KEYS
            and all(isinstance(view[key], str) and view[key] for key in view)
            and shadow_index.GENERATION.fullmatch(view['indexGeneration']) is not None)


def _valid_seal(seal):
    return (isinstance(seal, dict) and set(seal) == SEAL_KEYS
            and seal['schema'] == SEAL_SCHEMA
            and isinstance(seal['identity'], list) and len(seal['identity']) == 2
            and all(type(part) is int for part in seal['identity'])
            and isinstance(seal['snapshotSha256'], str)
            and SHA256.fullmatch(seal['snapshotSha256']) is not None)


def valid_receipt(receipt):
    """True only for a complete receipt whose prepared view is a legal CAS."""
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        return False
    if receipt['schema'] != RECEIPT_SCHEMA or not _valid_claim(receipt['claim']):
        return False
    expected, prepared = receipt['expected'], receipt['prepared']
    if not _valid_view(expected) or not _valid_view(prepared) or not _valid_seal(receipt['seal']):
        return False
    if expected['indexGeneration'] == prepared['indexGeneration']:
        return False
    # Same comparison publish_candidate applies under its lock, with
    # current==expected: the prepared view must be a legal successor.
    return published_view.publication_decision(expected, expected, prepared) == prepared


def build_receipt(claim, prepared):
    """Build the pre-CAS receipt from a `shadow_publish_flow.prepare` result.

    Pure: the caller must durably retain the returned value *before* calling
    `publish`; this function does not write it anywhere.
    """
    if not isinstance(prepared, dict) or prepared.get('outcome') != 'prepared':
        raise ValueError('invalid_shadow_ack_receipt')
    receipt = copy.deepcopy({'schema': RECEIPT_SCHEMA, 'claim': claim,
                             'expected': prepared.get('expected'),
                             'prepared': prepared.get('view'),
                             'seal': prepared.get('sealed')})
    candidate = prepared.get('candidate')
    if (not valid_receipt(receipt) or not isinstance(candidate, dict)
            or candidate.get('generation') != receipt['prepared']['indexGeneration']):
        raise ValueError('invalid_shadow_ack_receipt')
    return receipt


def _seal_status(candidate_seal, receipt_seal):
    if candidate_seal is True or candidate_seal == SEAL_MATCH:
        return SEAL_MATCH
    if candidate_seal is False or candidate_seal == SEAL_MISMATCH:
        return SEAL_MISMATCH
    if isinstance(candidate_seal, dict):
        if not _valid_seal(candidate_seal):
            return SEAL_UNKNOWN
        return SEAL_MATCH if candidate_seal == receipt_seal else SEAL_MISMATCH
    # None, SEAL_UNKNOWN, 1, 'yes' and anything else: not proved.
    return SEAL_UNKNOWN


def decide(receipt, current_view, current_claim, candidate_seal):
    """Return {decision, reason, ack, current}; ACK only with full proof."""
    if receipt is None:
        return _result(DEFER, 'receipt_missing')
    if not valid_receipt(receipt):
        return _result(DEFER, 'receipt_malformed')
    claim = receipt['claim']
    if current_claim is None:
        # Already ACKed elsewhere, expired or unreadable: nothing to ACK safely.
        return _result(DEFER, 'claim_unavailable')
    if not _valid_claim(current_claim):
        return _result(DEFER, 'claim_malformed')
    if current_claim['id'] != claim['id']:
        return _result(DEFER, 'claim_identity_changed')
    if current_claim['digest'] != claim['digest']:
        # The same claim now carries different work; this receipt no longer
        # describes it. Never ACK the new digest on the old publication.
        return _result(REPROCESS, 'claim_digest_changed')
    if not _valid_view(current_view):
        return _result(DEFER, 'pointer_unreadable')
    expected, prepared = receipt['expected'], receipt['prepared']
    if any(current_view[key] != expected[key] for key in IDENTITY_KEYS):
        return _result(DEFER, 'pointer_identity_changed', current=current_view)
    if current_view == prepared:
        status = _seal_status(candidate_seal, receipt['seal'])
        if status == SEAL_MATCH:
            return _result(ACK, 'cas_landed_sealed', claim=claim, current=current_view)
        if status == SEAL_MISMATCH:
            return _result(DEFER, 'published_candidate_seal_mismatch', current=current_view)
        return _result(DEFER, 'published_candidate_unproved', current=current_view)
    if current_view == expected:
        return _result(REPROCESS, 'cas_not_landed', current=current_view)
    if current_view['indexGeneration'] in (expected['indexGeneration'],
                                           prepared['indexGeneration']):
        # A known generation under a different view: not produced by the CAS
        # contract (random generations are never reused). Investigate.
        return _result(DEFER, 'pointer_generation_reused', current=current_view)
    if current_view['contentRevision'] in (expected['contentRevision'],
                                           prepared['contentRevision']):
        # Another run may be publishing this same revision (duplicate worker,
        # or a revert to expected). Re-preparing would collide; investigate.
        return _result(DEFER, 'pointer_content_revision_collision', current=current_view)
    # Advanced by someone: ours may have landed and been superseded, or
    # another publisher won first. Indistinguishable; never ACK, re-prepare.
    return _result(REPROCESS, 'pointer_advanced_ownership_unproven', current=current_view)
