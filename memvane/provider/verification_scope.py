"""A runtime proof cache scoped to one hook invocation."""
from contextlib import contextmanager
from contextvars import ContextVar

_VERIFIED = ContextVar('verified_runtime_generations', default=None)


@contextmanager
def verification_scope():
    if _VERIFIED.get() is not None:
        yield
        return
    token = _VERIFIED.set({})
    try:
        yield
    finally:
        _VERIFIED.reset(token)


def current():
    return _VERIFIED.get()
