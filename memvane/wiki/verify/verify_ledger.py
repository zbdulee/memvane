"""The verify suppression body hash."""
from __future__ import annotations

import hashlib


def source_body_hash(text: str) -> str:
    """Content hash of the *bounded* source text that was fed to the extractor.

    The ledger's ``verify`` suppressions key on this, so both sides must hash the
    same bytes: wiki_compile_worker hashes read_source_bounded() output and
    wiki_verify_worker hashes the same maxSourceChars-bounded slice. Exact
    content, no normalization — any real source edit must retry, and whitespace
    is cheap to hash correctly here (unlike wiki_dedup_scan.body_hash, which
    normalizes because it compares two independently authored pages).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
