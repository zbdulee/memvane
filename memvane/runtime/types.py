"""Value types for runtime resource admission."""
from __future__ import annotations

from typing import Protocol

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


RETRY = {'busy', 'deferred_resource', 'deferred_quota', 'staged_evaluation_deferred', 'apply_deferred',
         'setup_required', 'setup_in_progress'}

DISPATCH_REASONS = frozenset((
    'kind_invalid', 'target_invalid', 'target_unavailable',
    'target_journal_changed', 'target_identity_invalid',
    'target_entry_unavailable', 'target_entry_invalid',
    'target_launch_failed', 'target_child_failed', 'target_result_invalid',
    'target_deferred', 'target_preparing', 'writer_floor', 'state_format_mismatch'))


class DispatchFailure(ValueError):
    """A bounded, non-sensitive Laya wake failure for the durable ACK ledger."""

    def __init__(self, code, *, detail=None):
        if code not in DISPATCH_REASONS:
            raise ValueError('invalid_dispatch_reason')
        if detail is not None and not isinstance(detail, str):
            raise ValueError('invalid_dispatch_detail')
        self.code = code
        # Bounded local diagnostics (child stderr tail); never in the ledger
        # reason, which stays a fixed non-sensitive shape.
        self.detail = detail
        super().__init__(code)


class ServiceFailure(ValueError):
    def __init__(self, code, timing=None):
        super().__init__('resident_service_' + code)
        self.code = code
        self.timing = timing or {}


# Resource handles. These objects hold owner-private lock files, the wake
# queue database or a monitor child, so they cross the domain boundary as
# handles. Only the members below may be used outside ``memvane.runtime``;
# ``_`` members never. Method signatures are the implementing classes' own
# (the structure guard compares them).

class CoordinatorHandle(Protocol):
    """Public members of a resource coordinator (``api.coordinator``, ``Coordinator``)."""

    root: object

    def cleanup_unknown(self, owned_pid): ...

    def check(self, policy, report): ...

    def demand(self): ...

    def priority_work(self): ...

    def inference(self, deadline, namespace): ...

    def inference_compute(self, deadline): ...

    def training(self, policy, report): ...

    def step(self, policy, report, *, resident_credit=0, with_fd=False): ...

    def activity(self, lane, policy, report): ...


class WakeQueueHandle(Protocol):
    """Public members of the runtime wake queue (``api.wake_queue``, ``WakeQueue``)."""

    def offer(self, kind, target, *, now=None, pull_due=False, delay=0): ...

    def pull_first(self, kind, target): ...

    def pending(self): ...


class SupervisorHandle(Protocol):
    """Public members of the resource monitor supervisor (``api.supervisor``, ``Supervisor``)."""

    status: str

    def tick(self): ...

    def close(self): ...


class TrainingGuardHandle(Protocol):
    """Public members of a coordinated trainer guard (``api.training_guard``, ``TrainingGuard``)."""

    def poll(self): ...

    def mark_loaded(self, before, after): ...

    def step(self): ...
