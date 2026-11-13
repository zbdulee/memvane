"""Recall result limits and fixed diagnostic errors."""

MAX_ROWS = 5
HYBRID_LIST_LIMIT = 20
RRF_K = 60
ROW_KEYS = frozenset({"scope", "generation", "path", "hash", "body", "score"})


class HostResponseError(ValueError):
    """Fixed diagnostic code; never includes document body or source path."""
