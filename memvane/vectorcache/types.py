"""Values of the vector arena shared by its reader, its writer and their callers."""

# The arena format this bundle reads and writes; another number reads as "no arena".
FORMAT = 1
# The arena directory beside the view store, in the install state root.
DIRECTORY = 'vector-cache'
CURRENT = 'current.json'
PAGE = 4096
DTYPE = 'float32'
# Member spans this many rows apart or closer are scanned in one kernel call: a call costs
# about as much as scanning 190 rows, and the scores of the rows between are dropped.
GAP_ROWS = 256
# What a recall reports about the arena (``vectorCache``).
CACHE_STATES = ('ok', 'building', 'absent', 'disabled', 'inconsistent')
# Kernel refusals that name the scanned bytes, not the caller's budget.
VALUE_REASONS = frozenset(('embedding_values_invalid', 'embedding_set_invalid'))

# The writer's bounds (``writer`` reads them at each use).
# One pass's catch-up, and one pass's appends ahead of its publications.
PASS_VECTORS = 4096
PASS_SECONDS = 2.0
# Dead rows (replaced or no longer live sets) that start a compaction.
COMPACT_SHARE = 0.5
COMPACT_BYTES = 64 * 1024 * 1024
# An install's arena may grow to twice its live vectors, and at least this.
INSTALL_FLOOR_BYTES = 256 * 1024 * 1024
# All installs' arenas together; over it, arenas unused this long are removed, oldest first.
GLOBAL_BYTES = 2 * 1024 * 1024 * 1024
IDLE_SECONDS = 14 * 24 * 3600
# Why a pass's arena did not move although sets are left (``vector.cache`` ``skipped``).
SKIP_REASONS = ('quota', 'format_newer', 'write_unavailable', 'set_unreadable')
