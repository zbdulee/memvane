"""Public limits for diagnostic event rendering."""
from __future__ import annotations

PROMPT_CHARS = 1000
WIKI_TAIL_CHARS = 8000

# emit() default for state_root/install/host/session: resolve from the bound
# install, so an omitted field stays distinguishable from an explicit None.
UNRESOLVED = object()

REASON_SPAWN_FAILED = 'runtime_spawn_failed'
REASON_TIMEOUT = 'runtime_timeout'
REASON_RUNTIME_ERROR = 'runtime_error'
REASON_EXIT = 'runtime_exit'
REASON_INPUT_UNENCODABLE = 'input_unencodable'
REASON_OUTPUT_UNDECODABLE = 'output_undecodable'
REASON_OUTPUT_NOT_JSON = 'output_not_json'
REASON_OUTPUT_SHAPE = 'output_shape_invalid'
REASON_COUNT_MISMATCH = 'count_mismatch'
MEASURE_REASONS = (REASON_SPAWN_FAILED, REASON_TIMEOUT, REASON_RUNTIME_ERROR, REASON_EXIT,
                   REASON_INPUT_UNENCODABLE, REASON_OUTPUT_UNDECODABLE,
                   REASON_OUTPUT_NOT_JSON, REASON_OUTPUT_SHAPE, REASON_COUNT_MISMATCH)
