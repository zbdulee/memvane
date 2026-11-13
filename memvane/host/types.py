"""Shared host types."""
from __future__ import annotations

class VectorUnavailable(Exception):
    """No query vector for this prompt; serve the published lexical lists."""
    # ``laya_incremental_docs_query`` recognizes this marker, not the class:
    # the hook runs this file as __main__, a second copy of every class here.
    no_query_vector = True
