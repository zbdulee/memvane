"""Public diagnostic operations with lazy implementation loading."""
from __future__ import annotations

from memvane.diagnostics.types import PROMPT_CHARS, WIKI_TAIL_CHARS


def bind(*args, **kwargs):
    from memvane.diagnostics.event_log import bind as implementation
    return implementation(*args, **kwargs)


def bound(*args, **kwargs):
    from memvane.diagnostics.event_log import bound as implementation
    return implementation(*args, **kwargs)


def code(*args, **kwargs):
    from memvane.diagnostics.event_log import code as implementation
    return implementation(*args, **kwargs)


def elapsed_ms(*args, **kwargs):
    from memvane.diagnostics.event_log import elapsed_ms as implementation
    return implementation(*args, **kwargs)


def emit(*args, **kwargs):
    from memvane.diagnostics.event_log import emit as implementation
    return implementation(*args, **kwargs)


def install_of(*args, **kwargs):
    from memvane.diagnostics.event_log import install_of as implementation
    return implementation(*args, **kwargs)


def prompt_fields(*args, **kwargs):
    from memvane.diagnostics.event_log import prompt_fields as implementation
    return implementation(*args, **kwargs)


def session_id(*args, **kwargs):
    from memvane.diagnostics.event_log import session_id as implementation
    return implementation(*args, **kwargs)


def state_root_for_decision(*args, **kwargs):
    from memvane.diagnostics.event_log import state_root_for_decision as implementation
    return implementation(*args, **kwargs)


def state_root_of(*args, **kwargs):
    from memvane.diagnostics.event_log import state_root_of as implementation
    return implementation(*args, **kwargs)


def unbind(*args, **kwargs):
    from memvane.diagnostics.event_log import unbind as implementation
    return implementation(*args, **kwargs)


def wiki(*args, **kwargs):
    from memvane.diagnostics.event_log import wiki as implementation
    return implementation(*args, **kwargs)


def wiki_tail(*args, **kwargs):
    from memvane.diagnostics.event_log import wiki_tail as implementation
    return implementation(*args, **kwargs)


def capture(*args, **kwargs):
    from memvane.diagnostics.diagnostic_trace import capture as implementation
    return implementation(*args, **kwargs)


def telemetry_emit(*args, **kwargs):
    from memvane.diagnostics.job_telemetry import emit as implementation
    return implementation(*args, **kwargs)
