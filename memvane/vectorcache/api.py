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


def score_sets(directory, sets, *, profile_id, recipe_id, dimension, store_incarnation,
               queries, native, deadline, stats, clock):
    from memvane.vectorcache.scan import score_sets as implementation
    return implementation(directory, sets, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                          store_incarnation=store_incarnation, queries=queries, native=native,
                          deadline=deadline, stats=stats, clock=clock, open_arena=opened)


def identity(*, nonce, profile_id, recipe_id, dimension, store_incarnation):
    """The identity one arena pair is bound to."""
    from memvane.vectorcache.arena import identity as implementation
    return implementation(nonce=nonce, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                          store_incarnation=store_incarnation)


def names(nonce):
    """``(vec file name, idx file name)`` of the arena pair ``nonce``."""
    from memvane.vectorcache.arena import names as implementation
    return implementation(nonce)


def vec_header(value):
    from memvane.vectorcache.arena import vec_header as implementation
    return implementation(value)


def idx_header(value):
    from memvane.vectorcache.arena import idx_header as implementation
    return implementation(value)


def record(blob_sha, first_row, chunk_count, set_digest, vec_crc):
    """One idx record of a set whose rows start at ``first_row``."""
    from memvane.vectorcache.arena import record as implementation
    return implementation(blob_sha, first_row, chunk_count, set_digest, vec_crc)


def writer(directory, *, profile_id, recipe_id, dimension, store_incarnation):
    """One sync pass's writer of the arena in ``directory`` (``start``, ``add``, ``summary``, ``close``)."""
    from memvane.vectorcache.writer import Writer
    return Writer(directory, profile_id=profile_id, recipe_id=recipe_id, dimension=dimension,
                  store_incarnation=store_incarnation)
