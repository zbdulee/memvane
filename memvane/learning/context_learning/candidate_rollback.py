"""Go back to the model an application replaced: the application's commit, in reverse.

A maintenance command the owner asks for. It undoes the newest application:
the journal the active pointer's marker names (the live journal, or the one
archived when a later candidate formed its intent) while that journal is
``activated`` or stuck at ``pointer_committed``.

The owner's decision is written first: the journal becomes ``rejected`` with
reason ``rolled_back`` (an existing phase, so the candidate is never applied
again and no worker pass resumes its application). Then, under the
application's two locks, the receipt for the previous pointer, its binding
and the previous pointer are written. No record of its own: a run that
stopped after the journal is recognised by that journal (``rolled_back``
while the pointer is still its ``new_pointer``) and finished by the same
request or by the next worker pass (``resume``). Until then the receipt,
binding and pointer may disagree and selector inference refuses (fail closed).

A later candidate's journal that committed nothing (``intent``, ``prepared``)
was formed against the pointer that is going away: it is removed, and that
candidate is evaluated again against the restored model.

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


def _journals(root):
    """``[(path, row)]``: the live journal first, then every archived one that can be read."""
    found = []
    live = root / application.JOURNAL
    if live.exists() or live.is_symlink():
        found.append((live, application.read_journal(root)))
    folder = root / application.ARCHIVE
    if folder.is_dir() and not folder.is_symlink():
        for path in sorted(folder.glob('*.json')):
            try:
                row = read_private_json(path)
            except (OSError, ValueError):
                continue
            if (isinstance(row, dict) and row.get('schema') == application.SCHEMA
                    and path.name == str(row.get('apply_id')) + '.json'):
                found.append((path, row))
    return found


def _rolled_back(row):
    return row.get('phase') == 'rejected' and row.get('reason') == REASON


def _state(root):
    """``(status, path, row)`` of what a restore would do; reads only.

    ``rollback_resumed``: a restore already wrote its decision and stopped before the pointer.
    """
    pointer = read_private_json(root / POINTER)
    journals = _journals(root)
    for path, row in journals:
        if _rolled_back(row) and row.get('new_pointer') == pointer:
            return 'rollback_resumed', path, row
    marker = pointer.get('application') if isinstance(pointer, dict) else None
    if isinstance(marker, dict):
        for path, row in journals:
            if (row.get('apply_id') == marker.get('id') and row.get('phase') in PHASES
                    and row.get('new_pointer') == pointer):
                return 'rollback_planned', path, row
    for path, row in journals:
        if _rolled_back(row) and row.get('old_pointer') == pointer:
            return 'already_restored', path, row
    return 'nothing_to_restore', None, None


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
    return {'status': status, 'cycle_id': row['cycle_id'],
            'appliedArtifactSha256': row['new_pointer']['artifact_sha256'],
            'previousArtifactSha256': row['old_pointer']['artifact_sha256'],
            'previousCycle': row['old_pointer'].get('cycle_id'),
            'appliedAtUnixMs': int(row['new_receipt'].get('createdAt', 0) * 1000)}


def plan(root):
    """What ``restore`` would do; writes nothing."""
    root = Path(root)
    try:
        status, _path, row = _state(root)
        if row is None:
            return {'status': status, 'changesApplied': 0}
        if status != 'already_restored':
            _check(root, row)
        return {**_summary(row, 'rollback_planned' if status == 'rollback_resumed' else status),
                'phase': 'rolling_back' if status == 'rollback_resumed' else row['phase'], 'changesApplied': 0}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'rollback_blocked', 'reason': str(exc)[:80], 'changesApplied': 0}


def _newer_uncommitted(root, row):
    """The live journal of a later candidate that committed nothing under the pointer going away."""
    live = root / application.JOURNAL
    if not (live.exists() or live.is_symlink()):
        return None
    other = application.read_journal(root)
    if (other['apply_id'] != row['apply_id'] and other['phase'] in ('intent', 'prepared')
            and other.get('old_pointer') == row['new_pointer']):
        return live
    return None


def restore(root, *, fault=lambda _point: None, resume_only=False):
    """Close the journal as rolled back, then put the previous receipt, binding and pointer back.

    ``resume_only``: finish a restore that already wrote its decision, start none.
    """
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
        status, path, row = _state(root)
        if row is None:
            return {'status': status, 'changesApplied': 0}
        if status == 'already_restored':
            _history(root, row['old_pointer'])
            return {**_summary(row, status), 'changesApplied': 0}
        if resume_only and status != 'rollback_resumed':
            return {'status': 'nothing_to_resume', 'changesApplied': 0}
        receipt = _check(root, row)
        stale = _newer_uncommitted(root, row)
        summary = _summary(row, 'restored')
        # Each write is the only durable effect of its step and raises on failure, so a
        # step that did not happen ends the command before the next one.
        if status == 'rollback_planned':
            # The owner's decision first: from here no worker pass resumes this application.
            row['phase'] = 'rejected'
            row['reason'] = REASON
            application.put(path, row)
            written += 1
        fault('journal_closed')
        application.put(root / identity.RECEIPT, receipt)
        written += 1
        fault('receipt_restored')
        application.put(root / identity.BINDING, _binding(receipt))
        fault('binding_restored')
        application.put(root / POINTER, row['old_pointer'])
        fault('pointer_restored')
        if stale is not None:
            # Ignored when it fails: the journal names a pointer that is no longer active and
            # commits nothing; the next restore or a newer candidate's intent replaces it.
            try:
                os.unlink(stale)
            except OSError:
                pass
        _history(root, row['old_pointer'])
        return {**summary, 'changesApplied': 1}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Stopped between its writes: the same request or the next worker pass finishes it.
        return {'status': 'rollback_interrupted' if written else 'rollback_blocked', 'reason': str(exc)[:80],
                'changesApplied': 0}
    finally:
        if pointer_fd is not None:
            os.close(pointer_fd)
        os.close(fd)


def resume(root):
    """The worker pass's part: finish a restore that stopped after its decision; never raises."""
    try:
        root = Path(root)
        if _state(root)[0] == 'rollback_resumed':
            return restore(root, resume_only=True)
    except Exception:  # noqa: BLE001 - the pass goes on; the journal still says what is left
        pass
    return None


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
