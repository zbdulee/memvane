"""The vector arena: the reader and scan a recall uses, and the format its writer shares."""
from memvane.vectorcache.types import CACHE_STATES, DIRECTORY, FORMAT  # noqa: F401


def opened(directory, *, profile_id, recipe_id, dimension, store_incarnation):
    """Context: the arena of ``directory`` bound to this store and vector spec, or None (no arena)."""
    from memvane.vectorcache.arena import opened as implementation
    return implementation(directory, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                          store_incarnation=store_incarnation)


def scan(arena, covered, *, dimension, queries, native, deadline, stats):
    """``(scores, unreadable)`` of the covered sets, scanned from the mapped arena."""
    from memvane.vectorcache.scan import scan as implementation
    return implementation(arena, covered, dimension=dimension, queries=queries, native=native,
                          deadline=deadline, stats=stats)
