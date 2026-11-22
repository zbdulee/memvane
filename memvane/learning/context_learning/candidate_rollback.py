"""Go back to the model an application replaced: the application's commit, in reverse.

A maintenance command the owner asks for; nothing runs it on its own. It
undoes the newest application only: the journal that is ``activated`` (or
stuck at ``pointer_committed``) while the pointer is that journal's
``new_pointer``. Under the application's two locks it writes, in the reverse
order of ``candidate_apply.apply_candidate``: the receipt for the previous
pointer, its binding, the previous pointer, then the journal as ``rejected``
with reason ``rolled_back`` so the same candidate is never applied again.

No record of its own: an interrupted run is recognised by the journal still
being ``activated`` or ``pointer_committed`` while the pointer is already the
``old_pointer`` (an application never leaves that), and the same command
finishes it. Between an interruption and that rerun the receipt, binding and
pointer may disagree, and selector inference refuses (fail closed).

The resident is not asked to prepare and activate: it loads the generation
the pointer names on its next ordinary request, as after any change of the
active checkpoint.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import time

from . import candidate_apply as application
from . import inference_identity as identity, resident
from .dataset import read_private_json
from .learning_profile import digest
from .local_cycle import _hash
from memvane.runtime.api import coordination_lock as _lock

REASON = 'rolled_back'
POINTER = 'active-checkpoint.json'
PHASES = ('activated', 'pointer_committed')


def _binding(receipt):
    return {'schema': identity.SCHEMA, 'receipt_sha256': digest(receipt)}


def _state(root):
    """``(status, journal row or None)`` of what a restore would do; reads only."""
    path = root / application.JOURNAL
    if not path.exists() and not path.is_symlink():
        return 'nothing_to_restore', None
    row = application.read_journal(root)
    pointer = read_private_json(root / POINTER)
    if row['phase'] == 'rejected' and row.get('reason') == REASON and pointer == row['old_pointer']:
        return 'already_restored', row
    if row['phase'] not in PHASES or pointer not in (row['new_pointer'], row['old_pointer']):
        return 'nothing_to_restore', None
    return 'rollback_planned', row


def _target_receipt(root, row):
    """The receipt that binds the previous pointer, from the journal or from the one bound now."""
    old, new = digest(row['old_pointer']), digest(row['new_pointer'])
    receipt = read_private_json(root / identity.RECEIPT)
    if receipt in (row['old_receipt'], row['new_receipt']):
        return row['old_receipt']
    if not isinstance(receipt, dict) or receipt.get('checkpoint_sha256') not in (old, new):
        raise ValueError('rollback_evidence_changed')
    if receipt['checkpoint_sha256'] == old:
        return receipt  # Written by an interrupted run.
    # Prepared again for the applied pointer since: the same change an application makes, reversed.
    return {**receipt, 'checkpoint_sha256': old, 'createdAt': time.time()}


def _check(root, row):
    previous = row['old_pointer']
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('rollback_unsafe_path')
    artifact = resident._safe_file(previous['artifact_path'])
    if _hash(artifact) != previous['artifact_sha256']:
        raise ValueError('rollback_previous_artifact_changed')
    return _target_receipt(root, row)


def _summary(row, status):
    return {'status': status, 'cycle_id': row['cycle_id'], 'phase': row['phase'],
            'appliedArtifactSha256': row['new_pointer']['artifact_sha256'],
            'previousArtifactSha256': row['old_pointer']['artifact_sha256'],
            'previousCycle': row['old_pointer'].get('cycle_id'),
            'appliedAtUnixMs': int(row['new_receipt'].get('createdAt', 0) * 1000)}


def plan(root):
    """What ``restore`` would do; writes nothing."""
    root = Path(root)
    try:
        status, row = _state(root)
        if row is None:
            return {'status': status, 'changesApplied': 0}
        if status == 'rollback_planned':
            _check(root, row)
        return {**_summary(row, status), 'changesApplied': 0}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'rollback_blocked', 'reason': str(exc)[:80], 'changesApplied': 0}


def restore(root, *, fault=lambda _point: None):
    """Put the previous pointer, receipt and binding back and close the journal."""
    root = resident._private_state(Path(root))
    fd = _lock(root / 'candidate-apply.lock')
    pointer_fd = None
    written = 0
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pointer_fd = _lock(root / 'cycles' / 'cycle.lock')
            fcntl.flock(pointer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'rollback_deferred', 'reason': 'apply_busy', 'changesApplied': 0}
        status, row = _state(root)
        if row is None or status == 'already_restored':
            return {**(_summary(row, status) if row else {'status': status}), 'changesApplied': 0}
        receipt = _check(root, row)
        summary = _summary(row, 'restored')
        # Each write is the only durable effect of its step and raises on failure, so a
        # step that did not happen ends the command before the next one.
        application.put(root / identity.RECEIPT, receipt)
        written += 1
        fault('receipt_restored')
        application.put(root / identity.BINDING, _binding(receipt))
        written += 1
        fault('binding_restored')
        application.put(root / POINTER, row['old_pointer'])
        written += 1
        fault('pointer_restored')
        row['phase'] = 'rejected'
        row['reason'] = REASON
        application.put(root / application.JOURNAL, row)
        fault('journal_closed')
        _history(root, row['old_pointer'])
        return {**summary, 'changesApplied': 1}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Stopped between its writes: the same request finishes it; nothing is reported as done.
        return {'status': 'rollback_interrupted' if written else 'rollback_blocked', 'reason': str(exc)[:80],
                'changesApplied': 0}
    finally:
        if pointer_fd is not None:
            os.close(pointer_fd)
        os.close(fd)


def _history(root, pointer):
    """The previous checkpoint is the active one from now on, for the operating score's intervals."""
    from . import prequential
    from .local_cycle import _atomic_json
    try:
        entries = prequential.read_history(root)
        cycle = pointer.get('cycle_id') if isinstance(pointer.get('cycle_id'), str) else prequential.BASE
        if entries and entries[-1]['cycle_id'] != cycle:
            entries.append({'cycle_id': cycle, 'appliedAtUnixMs': int(time.time() * 1000)})
            # Ignored on failure: only the attribution of later scores to a checkpoint depends on it.
            _atomic_json(root / prequential.HISTORY, {'schema': prequential.HISTORY_SCHEMA, 'entries': entries})
    except (OSError, ValueError, KeyError, TypeError):
        pass


def restore_project(project, *, plan_only=False):
    """``plan`` or ``restore`` for the selector namespace of ``project``; then asks the resident's upkeep."""
    from memvane.learning import selector_maintenance
    project = Path(project).resolve(strict=True)
    root = selector_maintenance._selector_state(project)
    if not os.path.lexists(root / POINTER):
        return {'status': 'nothing_to_restore', 'changesApplied': 0}
    if plan_only:
        return plan(root)
    result = restore(root)
    if result.get('status') == 'restored':
        from . import prequential
        prequential._emit(project, 'learning.promotion', outcome=REASON, cycle=result['cycle_id'],
                          previousCycle=result['previousCycle'])
        try:
            # The resident loads the previous head on its next request; this asks for that now.
            decision = selector_maintenance._selected_route(project)
            if decision is not None:
                selector_maintenance.request(decision, force=True)
        except Exception:
            pass  # A recall or the next session asks again; the files already name the previous model.
    return result


def restore_command(name, project, *, plan_only=False):
    """The two ``restore-`` setup commands of the learning CLI."""
    if name == 'restore-teacher-enable':
        from memvane.learning.default_teacher_enable import restore_project as teacher
        return teacher(project, plan_only=plan_only)
    return restore_project(project, plan_only=plan_only)
