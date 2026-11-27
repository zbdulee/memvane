"""Service-level lines of the logs skill: the names of ``memvane.diagnostics.slo``.

The lines are computed by the diagnostics domain (``diagnostics.api.slo_lines``);
this module keeps the skill-side names that scripts and tests import.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memvane.diagnostics.slo import (  # noqa: E402,F401
    ARENA_SHARE, CYCLE_OUTCOMES, FLOOR, HOOK, LARGE_CARDS, LEARNING_WAIT_SECONDS, LEXICAL_LARGE,
    REFUSED, REPEAT_SHARE, REPEAT_WINDOW, SELECTOR, SELECTOR_OVER_BUDGET, SEMANTIC_COMPLETE,
    SEMANTIC_COUNTS, SEMANTIC_USED, SESSION_IDLE, SESSION_PAIRS, SIGNAL_HOSTS, STAGES, SUMMARY_LABELS,
    SYNC_FLOOR, SYNC_OK, SYNC_SECONDS, SYNC_STREAK, VECTOR_CACHE, full_coverage, install_size,
    learning_codes, lines, render, summary_lines, sync_runs)
