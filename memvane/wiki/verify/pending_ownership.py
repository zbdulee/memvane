"""Producer byte proofs and recoverable retirement of unreferenced pending files.

The card/queue locks serialize this private state. Released verifiers see only
the new review job. They never see a cleanup receipt that could unlink the
retired file before the durable audit is recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

from memvane.kernel.atomic_rename import fsync_dir
from memvane.settings import api as settings
from memvane.store.api import regular
from memvane.store.types import MAX_BODY
from memvane.wiki.workspace import api as workspace, types as workspace_types
from memvane.wiki.verify import legacy_handoff, wiki_verify_worker as verify
from memvane.wiki.verify.pending_replacement import job_revisions, pending_matches_job, pending_replacement

NAME = legacy_handoff.NAME
load = legacy_handoff.load
store = legacy_handoff.store
reserved = legacy_handoff.reserved
has_state = legacy_handoff.has_state


def discard_unpublished(queue, state):
    # The queue rename consumes its staging name. A surviving name proves the
    # prepared operation did not publish, even if a released reader drained the queue.
    abandoned = set()
    for rel, plan in list(state['retirements'].items()):
        if legacy_handoff.OLD_SWAP.fullmatch(plan['swap']):
            continue  # The import never consumes an e8 no-commit witness.
        swap = queue.with_name(plan['swap'])
        try:
            info = swap.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('invalid_queue_stage')
        del state['retirements'][rel]
        abandoned.add(swap)
    if abandoned:
        store(queue, state)  # Forget every plan before removing its no-commit witness.
        for swap in abandoned:
            swap.unlink()


def remember(root, state, job, produced_bytes):
    if produced_bytes is not None:
        state['owners'][job['pendingPath']] = hashlib.sha256(produced_bytes).hexdigest()
    # Old verifier versions do not update this private state after publication.
    for rel in list(state['owners']):
        if rel not in state['retirements'] and not (root / rel).exists():
            del state['owners'][rel]


def log(root, job, selected, wiki, result, reason):
    # Diagnostic only; file retirement has its own durable deletion ledger.
    try:
        ledger = settings.ledger(root, workspace_types.VERIFY_LOG)
        if ledger is not None:
            verify.log_verdict(ledger, {**verify.base_record(job), 'result': result, 'reason': reason})
        path = (root / job['pendingPath']).relative_to(wiki)
        workspace.append_activity(root, result, path.as_posix(), '', bound=selected, reason=reason)
    except (OSError, ValueError):
        pass


def prepare(root, config, wiki, state, previous):
    """Return a retirement plan only for exactly the producer's entire byte image."""
    target = root / previous['targetPath']
    try:
        path, _ = pending_replacement(root, wiki, previous, target)
        raw, _, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return 'pending_ownership_unproved'
    sha = hashlib.sha256(raw).hexdigest()
    if sha != state['owners'].get(previous['pendingPath']) or not pending_matches_job(raw.decode(), previous):
        return 'pending_ownership_unproved'
    freshness = verify.source_freshness(root, config, previous) or {}
    reason = ('pending_source_changed' if freshness.get('reason') == 'content_hash_mismatch'
              else 'pending_replacement_superseded')
    state['retirements'][previous['pendingPath']] = {'job': previous, 'sha256': sha, 'reason': reason}
    return None


def _audited(root, job, plan):
    """An operation can resume after audit fsync and before unlink. Write its row once."""
    path = settings.ledger(root, workspace_types.VERIFY_DELETED)
    if path is None:
        return False
    operation = legacy_handoff.operation(plan)
    reasons = [plan['reason'], operation]
    token = operation.encode()
    # Every writer of this ledger holds the card lock. Keep a torn trailing row
    # separate so the retry can still produce a complete audit record.
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return False
        present = False
        last = b''
        for line in stream:
            last = line[-1:]
            if token not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (isinstance(row, dict) and row.get('targetPath') == job['pendingPath']
                    and row.get('verdict') == 'stale_replacement' and row.get('reasons') == reasons):
                present = True
                break
        if not present:
            if last and last != b'\n':
                stream.write(b'\n')
                stream.flush()
            record = {**verify.base_record(job), 'targetPath': job['pendingPath'], 'reasons': reasons}
            # Without a complete audit row the candidate and retirement plan stay
            # intact. The next pass retries without spending a verifier call.
            if not verify.record_verify_deletion(root, {}, job_revisions(job), record, 'stale_replacement'):
                return False
        os.fsync(stream.fileno())
    fsync_dir(path.parent)
    return True


def finish(root, config, wiki, queue, state, referenced, selected):
    """A referenced old job blocks even a prepared plan from an interrupted swap."""
    changed = False
    for rel, plan in list(state['retirements'].items()):
        if rel in referenced:
            continue
        job = plan['job']
        historical = legacy_handoff.OLD_SWAP.fullmatch(plan['swap'])
        if historical:
            # A missing swap and old audit cannot prove that these bytes are stale today.
            if not legacy_handoff.current_retirement(queue, rel, plan):
                continue
            fresh = verify.source_freshness(root, config, job) or {}
            if fresh.get('reason') != 'content_hash_mismatch':
                continue
        try:
            target = root / job['targetPath']
            path, _ = pending_replacement(root, wiki, job, target)
            if path.resolve() != path or any(part.startswith('.') for part in target.relative_to(wiki).parts):
                continue
            raw, node, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError):
            continue
        if historical and (raw is None or hashlib.sha256(raw).hexdigest() != plan['sha256']):
            continue
        if raw is not None and hashlib.sha256(raw).hexdigest() == plan['sha256'] == state['owners'].get(rel):
            try:
                if not _audited(root, job, plan):
                    continue
                if selected is not None and verify.parse_frontmatter(raw.decode())[0].get('status') == 'verified':
                    verify._supersede_job_intent(root, config, job, path, selected[0])
                info = path.lstat()
                if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        != tuple(node[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))):
                    continue
                path.unlink()
                fsync_dir(path.parent)
            except OSError:
                continue
            log(root, job, selected, wiki, 'stale_replacement', plan['reason'])
        elif raw is not None:
            log(root, job, selected, wiki, 'skipped', 'pending_ownership_unproved')
        state['owners'].pop(rel, None)
        del state['retirements'][rel]
        changed = True
    if changed:
        store(queue, state)
