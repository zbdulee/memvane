"""Public host operations."""
from __future__ import annotations


def run(*args, **kwargs):
    from memvane.host.hook_main import run as implementation
    return implementation(*args, **kwargs)


def never(*args, **kwargs):
    from memvane.host.onboarding_choice import never as implementation
    return implementation(*args, **kwargs)


def resume(*args, **kwargs):
    from memvane.host.onboarding_choice import resume as implementation
    return implementation(*args, **kwargs)


def skip(*args, **kwargs):
    from memvane.host.onboarding_choice import skip as implementation
    return implementation(*args, **kwargs)


def status(*args, **kwargs):
    from memvane.host.onboarding_choice import status as implementation
    return implementation(*args, **kwargs)


def suppressed(*args, **kwargs):
    from memvane.host.onboarding_choice import suppressed as implementation
    return implementation(*args, **kwargs)


def valid_session(*args, **kwargs):
    from memvane.host.onboarding_choice import valid_session as implementation
    return implementation(*args, **kwargs)
