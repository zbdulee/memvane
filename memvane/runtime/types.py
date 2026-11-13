"""Value types for runtime resource admission."""
from __future__ import annotations

from memvane.kernel.resource_deferred import ResourceDeferred

DROP_REASONS = frozenset(('target_missing', 'retry_cap'))


class TargetMissing(ValueError):
    """A Laya wake's project directory does not exist (yet): back off."""

    def __init__(self):
        super().__init__('target_missing')


class Dropped:
    """Truthy handler answer: the wake is ACKed as void and recorded as a drop.

    A handler returns it when its target no longer exists, so the row is
    removed once instead of retrying a vanished directory forever.
    """

    __slots__ = ('reason',)

    def __init__(self, reason):
        if reason not in DROP_REASONS:
            raise ValueError('invalid_drop_reason')
        self.reason = reason

    def __bool__(self):
        return True

    def __eq__(self, other):
        return isinstance(other, Dropped) and other.reason == self.reason

    def __hash__(self):
        return hash(('Dropped', self.reason))

    def __repr__(self):
        return 'Dropped(%r)' % self.reason


class PreSendDeferred(ResourceDeferred):
    """A provider launch was deferred before a child was sent."""
