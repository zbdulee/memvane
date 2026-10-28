"""Opt-in capture of current source snapshots; never changes recall selection."""
import os
from pathlib import Path
import uuid

from .contracts import digest, request
from .store import capture
import hashlib
import stat

_SHARED_FAILURE_CODES = frozenset({'preparation_budget', 'invalid_preparation_budget',
    'learning_deferred_resource', 'learning_compute_busy', 'learning_deferred_inference',
    'project_scope_code_changed', 'project_scope_binding_changed', 'project_scope_model_changed',
    'project_scope_installation_invalid', 'project_scope_installation_changed'})

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
    options = config.get('contextLearning', {})
    if 'stateRoot' not in options:
        from .runtime_setup import default_managed_root
        parent = default_managed_root().parent.parent / 'live-selection'
    else:
        configured = options['stateRoot']
        if not isinstance(configured, str):
            return None
        parent = Path(configured)
    if not parent.is_absolute() or not parent.is_dir():
        return None
    # Existing prepared parent only; reject symlink components and indexed roots.
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


def local_checkpoint_prepared(config, root):
    """True only when a prepared local selector checkpoint exists; read only.

    Recall routes (legacy QMD and selected Laya) keep their baseline ranked
    pool until this holds, so a project without a trained selector never
    loses search results.
    """
    try:
        state = _private_state(config, Path(root), create=False)
        pointer = state / 'active-checkpoint.json' if state is not None else None
        return (pointer is not None and not pointer.is_symlink()
                and pointer.is_file())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _id(hit):
    file = hit.get('file', '')
    if not isinstance(file,str) or not file:
        raise ValueError('invalid_candidate_id')
    return file[6:] if file.startswith('qmd://') else file


def observe(payload, config, project_root, final_results, *, phases=None, verdict=None,
            candidate_limit=None):
    opts = config.get('contextLearning', {})
    if not isinstance(opts, dict) or opts.get('capture') is not True:
        return 'disabled'
    gate = _edit_gate(payload)
    if gate is not None:
        return gate
    try:
        root = Path(project_root).resolve(strict=True)
        state = _private_state(config, root)
        if state is None:
            return 'unsafe_or_missing_state'
        from .corpus import snapshot as corpus_snapshot
        current_corpus = corpus_snapshot(root, config)
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
        return _store(sample, config, root, state, compact_sources, current_corpus)
    except Exception:
        return 'capture_unavailable'


def _edit_gate(payload):
    # Captures come from served recall prompts only. A host edit carries no
    # selection evidence of its own and is never stored.
    if payload.get('context_origin') == 'tool_edit':
        return 'edit_capture_unsupported'
    return None


def _store(sample, config, root, state, compact_sources, current_corpus):
    """Private capture plus the enrolled shared queue; shared by QMD and Laya."""
    from .corpus import reconcile as reconcile_corpus
    status = capture(sample,enabled=True,state_dir=state,compact_sources=compact_sources)
    if status == 'stored' and current_corpus is not None:
        reconcile_corpus(state, current_corpus)
    if status == 'stored' and config.get('sharedLearning', {}).get('enabled') is True:
        # The shared path is a separate, explicitly enrolled local queue.
        # It never runs an external teacher in the prompt hook.
        failure = {}; stage = 'enqueue'
        try:
            from .shared import enqueue, launch
            shared_status = enqueue(config, root, sample, compact_sources)
            if shared_status == 'queued':
                stage = 'launch'
                launch(config, root)
        except Exception as error:
            shared_status = 'unavailable'
            # Fixed codes only: exception messages may contain source text
            # or private paths. Keep the existing status/return behavior.
            code = error.args[0] if error.args and type(error.args[0]) is str else None
            failure = {'stage': stage, 'errorType': type(error).__name__[:80],
                       'reason': code if code in _SHARED_FAILURE_CODES else stage+'_unavailable'}
        try:
            from .local_cycle import _atomic_json
            _atomic_json(state / 'shared-last-status.json',
                         {'status': shared_status, 'request_id_hash': digest(sample['request_id']), **failure})
        except Exception:
            pass
    return status


def observe_published(payload, config, project_root, pool, selected, *, host, decision):
    """Capture the verified Laya pool the local selector scored; no source reads.

    ``pool`` holds the rows the selected route already verified (path, hash,
    body, generation, scope), bounded to the selector's own candidate limit.
    Candidate IDs are ``<wikiName>/<relative>``, as in ``select_published``.
    A row must match the sealed head membership of the same corpus snapshot;
    a newer incremental row is recorded as a snapshot failure, which keeps
    the sample private (not complete for the shared queue).
    """
    opts = config.get('contextLearning', {})
    if not isinstance(opts, dict) or opts.get('capture') is not True:
        return 'disabled'
    gate = _edit_gate(payload)
    if gate is not None:
        return gate
    try:
        from .laya_learning import CAPTURE_POOL_LIMIT, corpus_snapshot, wiki_names
        from .selection_port import published_candidate_rows
        root = Path(project_root).resolve(strict=True)
        if str(root) != decision['projectRoot']:
            return 'laya_route_project_mismatch'
        if not isinstance(pool, list) or not pool:
            return 'no_snapshot_candidates'
        if len(pool) > CAPTURE_POOL_LIMIT:
            return 'laya_pool_over_limit'
        state = _private_state(config, root)
        if state is None:
            return 'unsafe_or_missing_state'
        current_corpus = corpus_snapshot(root, config, decision=decision)
        indexed = ({(collection, relative): revision for collection, relative, revision
                    in current_corpus['material']['activeDocuments']}
                   if current_corpus else {})
        verified = published_candidate_rows(pool, wiki_names(decision), decision['scopeId'])
        rows = []; eligible = []; excluded = []; failures = []; compact_sources = {}
        for row in verified:
            cid = row['id']
            eligible.append(cid)
            text, revision = row['source_text'], row['revision_sha256']
            collection, _, relative = cid.partition('/')
            try:
                if len(text.encode('utf-8')) > 65536:
                    raise ValueError('oversize_unstable_or_nonregular')
                if current_corpus is None or indexed.get((collection, relative)) != revision:
                    raise ValueError('candidate_index_stale')
                excerpt = text
                from .compact_input import derive
                try:
                    excerpt = derive(text, revision)['body_text']
                    compact_sources[cid] = text
                except ValueError:
                    pass  # Captured for review, excluded from local training.
                rows.append({'candidate_id': cid, 'revision_sha256': revision,
                             'eligible': True, 'excerpt': excerpt})
            except ValueError as exc:
                failures.append({'candidate_id': cid, 'reason': str(exc)})
        captured = [r['candidate_id'] for r in rows]
        sampling = {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
                    'phase_counts': {'primary': len(pool)}, 'per_phase_limit': 30,
                    'eligible_ids': eligible, 'captured_ids': captured,
                    'baseline_selected_ids': list(dict.fromkeys(
                        r['path'] for r in selected if isinstance(r, dict))),
                    'excluded': excluded, 'snapshot_failures': failures,
                    'retrieval_truncated': False, 'assessment_complete': True,
                    'complete_within_returned_bound': (not failures
                                                       and set(eligible) == set(captured)),
                    'index_revision': (current_corpus['fingerprint'] if current_corpus
                                       else 'unavailable'),
                    'candidate_limit': CAPTURE_POOL_LIMIT}
        sample = request(payload, rows, host=host, request_id=str(uuid.uuid4()),
                         sampling=sampling)
        return _store(sample, config, root, state, compact_sources, current_corpus)
    except Exception:
        return 'capture_unavailable'
