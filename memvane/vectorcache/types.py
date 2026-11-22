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
