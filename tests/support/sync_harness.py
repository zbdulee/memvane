"""S9: sync passes without the runtime coordinator or a warm embedding service.

``coordinator()`` admits every provider lane and resource check; ``start_service``
is a no-op warm service. ``run_pass`` and ``passes_until`` drive
``sync.api.run`` for an ``install_builder.Install``.
"""
from __future__ import annotations

from contextlib import contextmanager


class _Coordinator:
    # None: the wake queue the priority probe reads resolves under the test runtime root.
    root = None

    @contextmanager
    def activity(self, lane, policy, report):
        yield 1

    def check(self, policy, report):
        return {'ready': True}


class _Service:
    def stop(self):
        pass


def coordinator():
    return _Coordinator()


def start_service(manifest, *, admit=None):
    return _Service()


def run_pass(install):
    """One ``sync.api.run`` pass of the install's project."""
    from memvane.sync import api as sync
    provider = getattr(install, 'provider', None)
    starter = provider.start_service if provider is not None else start_service
    return sync.run(install.project.root, coordinator=coordinator(), report={}, start_service=starter)


def passes_until(install, predicate, limit=8):
    """Run passes until ``predicate(result)``; returns every result, raises after ``limit``."""
    results = []
    for _ in range(limit):
        results.append(run_pass(install))
        if predicate(results[-1]):
            return results
    raise AssertionError('sync passes did not reach the condition: %r' % (results,))
