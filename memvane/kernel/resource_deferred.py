"""Shared retryable resource-deferral signal.

Runtime, endpoint, install, wiki and extractor paths raise or catch this
without depending on the learning cycle. ``context_learning.local_cycle``
re-exports the same class object for existing callers.
"""


class ResourceDeferred(ValueError):
    def __init__(self, reason, report=None):
        super().__init__(reason)
        self.report = report or {}
