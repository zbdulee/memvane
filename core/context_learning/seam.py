"""Opt-in capture of current source snapshots; never changes recall selection."""
import os
from pathlib import Path
import uuid

from .contracts import digest, request
from .store import capture
import hashlib
import stat

def _snapshot(path):
    # Bound bytes before decoding or hashing, including concurrent growth.
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            return None
        content = handle.read(65537)
        after = os.fstat(handle.fileno())
    if len(content) > 65536 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        return None
    return {"sha256": hashlib.sha256(content).hexdigest()}, content


def _private_state(config, root, *, create=True):
    configured = config.get('contextLearning', {}).get('stateRoot')
    if not isinstance(configured, str):
        return None
    parent = Path(configured)
    if not parent.is_absolute() or not parent.is_dir():
        return None
    # Existing explicit root only; reject symlink components and indexed roots.
    if any(p.is_symlink() for p in (parent, *parent.parents)):
        return None
    parent = parent.resolve(strict=True)
    # A private state directory may live under the project root when every
    # indexed collection is narrower. The checks below still reject any
    # state directory inside a configured collection (including a collection
    # that indexes the entire project root).
    if parent == root:
        return None
    for base in config.get('collectionPaths', {}).values():
        collection_root = (root / base).resolve()
        if parent == collection_root or collection_root in parent.parents:
            return None
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    namespace = digest(str(root))
    state = parent / namespace
    if state.is_symlink():
        return None
    if create:
        state.mkdir(mode=0o700, exist_ok=True)
    elif not state.is_dir():
        return None
    info = state.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return state


def _id(hit):
    file = hit.get('file', '')
    if not isinstance(file,str) or not file:
        raise ValueError('invalid_candidate_id')
    return file[6:] if file.startswith('qmd://') else file


def observe(payload, config, project_root, final_results, *, phases=None, verdict=None,
            candidate_limit=None, qmd_paths=None):
    opts = config.get('contextLearning', {})
    if not isinstance(opts, dict) or opts.get('capture') is not True:
        return 'disabled'
    # An edit may be stored only after shared enqueue atomically consumes its
    # one-use host-edit ticket. A project setting alone is never sufficient.
    is_edit = payload.get('context_origin') == 'tool_edit'
    if is_edit and (not isinstance(payload.get('edit_event'), dict)
                    or not payload['edit_event'].get('ticket_id')):
        return 'edit_ticket_required'
    try:
        root = Path(project_root).resolve(strict=True)
        deferred_allowed = None
        if is_edit and payload['edit_event'].get('deferred_capture') is True:
            from .deferred_edit import pending, allowed_paths
            ticket_id = payload['edit_event']['ticket_id']
            if not pending(root, ticket_id):
                return 'deferred_edit_observation_missing'
            if os.environ.get('QMD_DEFERRED_CAPTURE_REPLAY') != '1':
                return 'deferred_index_pending'
            deferred_allowed = allowed_paths(root, ticket_id)
            if not deferred_allowed:
                return 'deferred_registered_candidates_unavailable'
        state = _private_state(config, root)
        if state is None:
            return 'unsafe_or_missing_state'
        from .corpus import snapshot as corpus_snapshot, reconcile as reconcile_corpus
        current_corpus = corpus_snapshot(root, config, qmd_paths=qmd_paths)
        indexed_hashes = ({(collection, relative): revision
            for collection, relative, revision in current_corpus['material']['activeDocuments']}
            if current_corpus else {})
        # Legacy direct callers retain final-selected-only schema v1.
        pool_mode = phases is not None and verdict is not None
        phases = phases if pool_mode else [{'name':'primary','results':final_results,'wiki_scoped':False,'returned_count':len(final_results)}]
        expanded = pool_mode and candidate_limit is not None
        phase_limit = 30 if expanded else 8
        rows=[]; eligible=[]; excluded=[]; failures=[]; seen=set(); counts={}
        # The daemon may over-return. Rows sliced to the requested bound are
        # safe to inject, but do not prove the complete returned universe for
        # training/gold. pool.expand has its own candidate_budget evidence.
        assessed=not any(p.get('returned_count', 0) > len(p['results']) for p in phases)
        compact_sources={}
        for phase in phases:
            counts[phase['name']]=phase['returned_count']
            for hit in phase['results'][:phase_limit]:
                file=_id(hit)
                if deferred_allowed is not None:
                    from .deferred_edit import candidate_allowed
                    if not candidate_allowed(file, deferred_allowed):
                        excluded.append({'candidate_id':file,'phase':phase['name'],
                                         'reason':'not_from_registered_edit_source'})
                        continue
                decision=verdict(hit,phase['wiki_scoped']) if pool_mode else 'eligible'
                if decision!='eligible':
                    excluded.append({'candidate_id':file,'phase':phase['name'],'reason':decision})
                    if decision in {'observation_budget','eligibility_error'}:
                        assessed=False
                    continue
                if file in seen:
                    continue
                seen.add(file);eligible.append(file)
                collection,sep,relative=file.partition('/')
                base=config.get('collectionPaths',{}).get(collection)
                reason='unresolvable_collection'
                try:
                    if not sep or not isinstance(base,str):
                        raise ValueError(reason)
                    base_path=(root/base).resolve(strict=True)
                    path=(base_path/relative).resolve(strict=True)
                    if root not in path.parents or base_path not in path.parents:
                        raise ValueError('outside_allowed_root')
                    snap=_snapshot(path)
                    if snap is None:
                        raise ValueError('oversize_unstable_or_nonregular')
                    revision,content=snap
                    if current_corpus is not None and indexed_hashes.get((collection, relative)) != revision['sha256']:
                        raise ValueError('candidate_index_stale')
                    source_text=content.decode('utf-8')
                    excerpt=source_text
                    if config.get('collectionRoles',{}).get(collection) == 'wiki':
                        # Store the exact body separately; bounded excerpts still
                        # serve the optional teacher transport contract.
                        from .compact_input import derive
                        try:
                            compact=derive(source_text, revision['sha256'])
                            excerpt=compact['body_text']
                            compact_sources[file]=source_text
                        except ValueError:
                            pass  # Captured for review, excluded from local training.
                    rows.append({'candidate_id':file,'revision_sha256':revision['sha256'],'eligible':True,'excerpt':excerpt})
                except UnicodeError:
                    failures.append({'candidate_id':file,'reason':'invalid_utf8'})
                except ValueError as exc:
                    failures.append({'candidate_id':file,'reason':str(exc)})
                except (OSError,UnicodeError):
                    failures.append({'candidate_id':file,'reason':'missing_unreadable_or_encoding'})
        sampling=None
        if pool_mode:
            captured=[r['candidate_id'] for r in rows]
            truncated=any(v>phase_limit for v in counts.values())
            sampling={'mode':'eligible-policy-pool' if expanded else 'eligible-returned-pool','scope':'queried-phases-only',
                      'phase_counts':counts,'per_phase_limit':phase_limit,'eligible_ids':eligible,'captured_ids':captured,
                      'baseline_selected_ids':list(dict.fromkeys(_id(h) for h in final_results)),
                      'excluded':excluded,'snapshot_failures':failures,'retrieval_truncated':truncated,
                      'assessment_complete':assessed,'complete_within_returned_bound':assessed and not (expanded and any(x['reason']=='candidate_budget' for x in excluded)) and not truncated and not failures and set(eligible)==set(captured),
                      'index_revision':current_corpus['fingerprint'] if current_corpus else 'unavailable'}
            if expanded:
                sampling['candidate_limit'] = candidate_limit
        elif not rows:
            return 'no_snapshot_candidates'
        sample=request(payload,rows,host=os.environ.get('QMD_ENGINE','claude'),request_id=str(uuid.uuid4()),sampling=sampling)
        if is_edit:
            if config.get('sharedLearning', {}).get('enabled') is not True:
                return 'edit_shared_enrollment_required'
            from .shared import enqueue, launch, activate_edit_case
            shared_status = enqueue(config, root, sample, compact_sources)
            if shared_status != 'awaiting_local':
                return shared_status
            status = capture(sample,enabled=True,state_dir=state,compact_sources=compact_sources)
            activated = activate_edit_case(config, root, sample,
                                           captured=status == 'stored')
            if status == 'stored':
                if current_corpus is not None:
                    reconcile_corpus(state, current_corpus)
                if activated == 'queued':
                    launch(config, root)
            return activated
        status = capture(sample,enabled=True,state_dir=state,compact_sources=compact_sources)
        if status == 'stored' and current_corpus is not None:
            reconcile_corpus(state, current_corpus)
        if status == 'stored' and config.get('sharedLearning', {}).get('enabled') is True:
            # The shared path is a separate, explicitly enrolled local queue.
            # It never runs an external teacher in the prompt hook.
            try:
                from .shared import enqueue, launch
                shared_status = enqueue(config, root, sample, compact_sources)
                if shared_status == 'queued':
                    launch(config, root)
            except Exception:
                shared_status = 'unavailable'
            try:
                from .local_cycle import _atomic_json
                _atomic_json(state / 'shared-last-status.json',
                             {'status': shared_status, 'request_id_hash': digest(sample['request_id'])})
            except Exception:
                pass
        return status
    except Exception:
        return 'capture_unavailable'
