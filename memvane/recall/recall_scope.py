"""Per-recall state, its proof-epoch floor and sync wakes, and the provider owner wake."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import os

from memvane.recall.types import HostResponseError

_DURABLE_WAKE_STATES = frozenset(('queued', 'busy',
                                  'deferred_queue_capacity_retained'))


def _fail(code: str):
    raise HostResponseError(code)


# Per-recall state shared by both attempts: the highest journal epoch seen
# by any proof or recheck, and the sync wake recorded per project root.
_RECALL_STATE: contextvars.ContextVar = contextvars.ContextVar(
    'memvane_recall_state', default=None)


@contextmanager
def recall_scope():
    """Fresh state for one recall and its retry, bound to this thread/context only.

    An in-process host (Hermes) may run recalls concurrently or nested.
    """
    token = _RECALL_STATE.set({'epoch': None, 'wakes': {}})
    try:
        yield
    finally:
        _RECALL_STATE.reset(token)


def set_writer_allowed(allowed) -> None:
    """Record whether this recall's proof lets it record sync wakes."""
    state = _RECALL_STATE.get()
    if state is not None:
        state['writer_allowed'] = allowed


def _recall_open() -> bool:
    return _RECALL_STATE.get() is not None


def _recall_epoch(proof) -> None:
    """Raise when a proof's journal epoch is below the recall's highest one.

    A journal restore moves the epoch back; neither attempt may accept it.
    Outside a recall, or for a proof without an epoch, nothing is recorded.
    """
    state = _RECALL_STATE.get()
    if state is None or not isinstance(proof, dict):
        return
    epoch = proof.get('selectionEpoch')
    if type(epoch) is not int:
        return
    floor = state['epoch']
    if floor is not None and epoch < floor:
        _fail('selected_proof_rolled_back')
    state['epoch'] = epoch if floor is None else max(floor, epoch)


def _recall_sync_wake(project_root: str, **kwargs):
    """Record the selected sync wake at most once per project root and recall.

    Each request bumps the wake revision; a retried recall reuses the first
    recorded result instead of asking for another sync run.
    """
    from memvane.runtime import api as runtime_work
    state = _RECALL_STATE.get()
    if state is not None and state.get('writer_allowed') is False:
        return None
    if state is None:
        return runtime_work.request('laya-sync', project_root, **kwargs)
    wakes = state['wakes']
    if project_root not in wakes:
        wakes[project_root] = runtime_work.request('laya-sync', project_root, **kwargs)
    return wakes[project_root]


def _proof_still_current(refreshed: dict, proof: dict) -> bool:
    """Same route proof, allowing only the journal epoch to move forward.

    A sync owner marks its first publish of a wake with one epoch advance
    (cas_mark_new_write). The served rows stay bound to their pinned head,
    which the selected reader checks separately. An epoch below the
    highest one this recall has seen raises ``selected_proof_rolled_back``.
    """
    _recall_epoch(refreshed)
    if refreshed == proof:
        return True
    stable = lambda row: {key: value for key, value in row.items()
                          if key != 'selectionEpoch'}
    return (isinstance(refreshed, dict) and stable(refreshed) == stable(proof) and
            refreshed.get('selectionEpoch', -1) >= proof.get('selectionEpoch', 0))


def _writer_floor_allowed(proof):
    from memvane.kernel.memvane_version import writer_floor_allows
    try:
        return writer_floor_allows(proof['writerFloor'])
    except (OSError, KeyError, TypeError, ValueError):
        _fail('writer_floor_invalid')


def _wake_provider(decision: dict, proof: dict, *, transition=False) -> str:
    """Wake the off-hook provider owner and return its vector-side state code.

    Proof, owner-state integrity, terminal start failure and an identity
    transition raise. The returned codes (resource_deferred, retry_wait,
    preparing, wake_failed) leave recall serving its lexical lists.
    """
    if not _writer_floor_allowed(proof):
        return 'writer_floor'
    from memvane.journal.api import verify_selected_route
    from memvane.resident.types import TRANSIENT_FAILURES as _TRANSIENT_FAILURES
    from memvane.resident.api import failure_decision as _failure_decision, read_owner_state
    current = verify_selected_route(decision)
    _recall_epoch(current)
    if current != proof:
        _fail('selected_proof_changed')
    try:
        _failure_decision(proof['providerManifest'])
    except Exception as exc:
        from memvane.kernel.errors import IndexContractError
        from memvane.kernel.resource_deferred import ResourceDeferred
        if type(exc) is IndexContractError and str(exc) == 'provider_start_failed':
            _fail('selected_provider_start_failed')
        if not isinstance(exc, ResourceDeferred):
            raise HostResponseError('selected_provider_state_invalid') from exc
    try:
        state = read_owner_state(proof['providerManifest'])
    except Exception as exc:
        raise HostResponseError('selected_provider_state_invalid') from exc
    if (state is not None and state['terminal']
            and state['reason'] not in _TRANSIENT_FAILURES):
        _fail('selected_provider_start_failed')
    try:
        # A prompt that finds no vector owner brings a backed-off sync wake
        # forward; the recorded attempts and reason stay unchanged.
        result = _recall_sync_wake(decision['projectRoot'], pull_due=True)
    except Exception:
        result = None
    refreshed = verify_selected_route(decision)
    _recall_epoch(refreshed)
    stable = lambda row: {key: value for key, value in row.items()
                          if key != 'selectionEpoch'}
    if (stable(refreshed) != stable(proof) or
            refreshed.get('selectionEpoch', -1) < proof.get('selectionEpoch', 0)):
        _fail('selected_proof_changed')
    restart = None
    if not transition:
        # Independent of the wake queue: it also helps when that queue fails.
        restart = _restart_withdrawn_owner(decision, proof, state)
    if not isinstance(result, dict) or result.get('status') not in _DURABLE_WAKE_STATES:
        if transition:
            _fail('selected_provider_wake_failed')
        return 'selected_provider_wake_failed'
    if transition:
        _fail('selected_provider_identity_transition')
    if restart == 'laya_service_protocol_mismatch':
        # A newer per-user service speaks another protocol: lexical until
        # this installation's bundle upgrade catches up.
        return 'selected_provider_protocol_mismatch'
    if state is not None and state['reason'] == 'resource_deferred':
        return 'selected_provider_resource_deferred'
    if state is not None and state['reason'] in (
            'startup_failed', 'worker_exited', 'unexpected'):
        return 'selected_provider_retry_wait'
    return 'selected_provider_preparing'


def _restart_withdrawn_owner(decision: dict, proof: dict, state):
    """Ask the per-user service to adopt a withdrawn owner without the sync wake.

    After a bundle upgrade, a service replacement or a resource deferral the
    owner state is starting/resource_deferred and its socket is gone. The
    request returns at once; the owner state's 5s backoff bounds repeats.
    Only the executing bundle that proved this route may ask; the service
    proves the route again before it binds the socket. Returns the deferral
    code, or None.
    """
    if state is None or state['reason'] not in (
            'starting', 'resource_deferred', 'startup_failed', 'worker_exited'):
        return None
    manifest = proof['providerManifest']
    path = manifest.get('serviceSocketPath')
    if not isinstance(path, str) or os.path.lexists(path):
        return None
    from memvane.resident.api import ensure_running
    from memvane.kernel.errors import IndexContractError
    from memvane.kernel.resource_deferred import ResourceDeferred
    try:
        ensure_running(decision['projectRoot'], manifest)
    except ResourceDeferred as exc:
        # Best effort: the durable sync wake still retries the start.
        return str(exc)
    except OSError:
        return None
    except IndexContractError as exc:
        if str(exc) == 'provider_start_failed':
            _fail('selected_provider_start_failed')
        raise HostResponseError('selected_provider_state_invalid') from exc
    except Exception as exc:
        raise HostResponseError('selected_provider_state_invalid') from exc
