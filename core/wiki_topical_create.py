"""One-source, recoverable topical creation in an explicitly opted-in project."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat

import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_similarity as similarity

JOURNAL = 'topical-create-state.json'


def _read(root):
    path = root / JOURNAL
    if not path.exists() and not path.is_symlink(): return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1_000_000):
            raise topical.TopicalError('unsafe_create_journal')
        row = json.loads(os.read(fd, info.st_size + 1))
    finally:
        os.close(fd)
    if (not isinstance(row, dict) or row.get('schema') != 'qmd-topical-create-v1'
            or not {'batchId', 'sourcePath', 'sourceSha256', 'sourceRoots',
                    'compilePolicySha256', 'engine', 'phase', 'generationId',
                    'cardIds', 'preCandidates'} <= set(row)):
        raise topical.TopicalError('invalid_create_journal')
    return row


def create_one(root, source_roots, source_path, compile_cfg, engine, max_estimated_cents,
               *, generation_runner=None, verification_runner=None,
               recovery_only=False, skip_paths=()):
    root = experiment.require_sandbox(root)
    if not recovery_only and (generation_runner is None or verification_runner is None):
        raise topical.TopicalError('explicit_backend_runner_required')
    if type(max_estimated_cents) is not int or not 20 <= max_estimated_cents <= 160:
        raise topical.TopicalError('invalid_auto_refresh_budget')
    lock = root / refresh.LOCK
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise topical.TopicalError('unsafe_refresh_lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise topical.TopicalError('refresh_busy') from exc
        return _create_locked(root, source_roots, source_path, compile_cfg, engine,
                              max_estimated_cents, generation_runner, verification_runner,
                              recovery_only, skip_paths)
    finally:
        os.close(fd)


def _create_locked(root, source_roots, source_path, compile_cfg, engine, budget,
                   generation_runner, verification_runner, recovery_only, skip_paths):
    journal = _read(root)
    refs, baseline_cards = refresh._baseline_cards(root)
    if journal is None:
        if recovery_only: return {'status': 'no_pending_create'}
        state = reconcile._read(root)
        if state is None or source_path not in state['queue'] or not state['queue'][source_path]['currentSha256']:
            return {'status': 'no_new_source_pending'}
        claimed = reconcile.start_batch(root, source_roots, baseline_cards,
            trusted_card_ids=[card['cardId'] for card in baseline_cards],
            skip_paths=skip_paths)
        if not claimed['started'] and claimed['reason'] != 'batch_running':
            return {'status': claimed['reason']}
        batch = claimed['batch']
        if source_path not in batch['sources'] or source_path not in batch['snapshot']:
            return {'status': 'pending_review', 'reason': 'created_source_not_in_batch'}
        journal = {'schema': 'qmd-topical-create-v1', 'batchId': batch['batchId'],
                   'sourcePath': source_path, 'sourceSha256': batch['snapshot'][source_path]['sha256'],
                   'sourceRoots': sorted(source_roots), 'engine': engine,
                   'skipPaths': list(skip_paths),
                   'compilePolicySha256': topical.digest(topical.encoded(compile_cfg)),
                   'phase': 'claimed', 'generationId': None, 'cardIds': [], 'preCandidates': []}
        reconcile._atomic(root / JOURNAL, journal)
    elif (journal['sourcePath'] != source_path or journal['sourceRoots'] != sorted(source_roots)
          or journal['engine'] != engine or
          journal.get('skipPaths', []) != list(skip_paths) or
          (not recovery_only and journal['compilePolicySha256'] !=
           topical.digest(topical.encoded(compile_cfg)))):
        raise topical.TopicalError('create_journal_conflict')
    state = reconcile._read(root)
    batch = state.get('inFlight') if state else None
    if not batch:
        if state and state.get('lastCompletedBatchId') == journal['batchId']:
            (root / JOURNAL).unlink()
            return {'status': 'already_completed', 'generationId': journal['generationId']}
        raise topical.TopicalError('create_batch_missing')
    if batch['batchId'] != journal['batchId']:
        raise topical.TopicalError('create_batch_changed')
    if reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
        reconcile.finish_mock_batch(root, source_roots, batch['batchId'],
                                    success=False, skip_paths=skip_paths)
        (root / JOURNAL).unlink()
        return {'status': 'superseded_source_changed'}
    if batch['snapshot'].get(source_path, {}).get('sha256') != journal['sourceSha256']:
        raise topical.TopicalError('create_source_revision_changed')
    if journal['phase'] == 'claimed':
        _revision, body, _lines = topical.source_snapshot(root, source_path, {})
        published = list((root / '.auto-context/wiki/topical-v2').glob('*/*.md'))
        try: matches = similarity.retrieve(root, body[:600]) if published else []
        except topical.TopicalError as exc:
            return {'status': 'pending_review', 'reason': exc.code}
        journal['preCandidates'] = [{'path': row['path'], 'lead': row['lead']} for row in matches]
        journal['phase'] = 'pre_retrieved'; reconcile._atomic(root / JOURNAL, journal)
    generation_id = journal['generationId']
    if generation_id is None:
        pre_context = similarity.generation_context(root, journal['preCandidates'])
        pending = None
        try:
            if journal['phase'] == 'generation_failed_wait':
                if budget < 30:
                    return {'status': 'pending_review', 'reason': 'estimated_budget_exceeded'}
                payload = experiment.generation_contract(root, [source_path])
                payload['existingWikiCandidates'] = pre_context
                payload['timeout'] = 420
                response, pending, audit = refresh._recorded(root, 'generation',
                    payload, 'retry1')
                if pending:
                    return {'status': 'pending_review', 'reason': pending}
                if response is not None:
                    if not refresh._generation_transport_reviewed(
                            root, payload, response, audit, compile_cfg, engine):
                        return {'status': 'pending_review',
                                'reason': 'generation_policy_changed'}
                    generated = backend.stage_generation_response(root, response,
                        requested_sources=[source_path], reuse_existing=True)
                elif recovery_only:
                    return {'status': 'pending_review',
                            'reason': 'generation_retry_requires_explicit_backend'}
                else:
                    generated = backend.run_generation_backend(root, [source_path],
                        compile_cfg, engine, existing_wiki_candidates=pre_context,
                        allow_backend_execution=True, runner=generation_runner,
                        attempt_id='retry1', max_attempts=2,
                        estimated_cost_cents=10, max_cost_cents=20)
            else:
                generated, pending = refresh._generation(root, [source_path], compile_cfg,
                    engine, pre_context, generation_runner, recovery_only)
        except topical.TopicalError as exc:
            if journal['phase'] != 'generation_failed_wait':
                try:
                    payload = experiment.generation_contract(root, [source_path])
                    payload['existingWikiCandidates'] = pre_context
                    payload['timeout'] = 420
                    _response, failure, _audit = refresh._recorded(root, 'generation', payload)
                    if failure == 'generation_attempt_failed':
                        journal['phase'] = 'generation_failed_wait'
                        reconcile._atomic(root / JOURNAL, journal)
                        return {'status': 'pending_review', 'reason': 'generation_failed_retry_scheduled'}
                except topical.TopicalError:
                    pass
            return {'status': 'pending_review', 'reason': exc.code}
        if pending:
            if pending == 'generation_attempt_failed' and not recovery_only:
                journal['phase'] = 'generation_failed_wait'
                reconcile._atomic(root / JOURNAL, journal)
                return {'status': 'pending_review', 'reason': 'generation_failed_retry_scheduled'}
            return {'status': 'pending_review', 'reason': pending}
        generation_id = generated['generationId']
        card_ids = [row['cardId'] for row in generated['cards']]
        generation_attempts = 2 if journal['phase'] == 'generation_failed_wait' else 1
        if len(card_ids) != len(set(card_ids)) or 10 * (generation_attempts + len(card_ids)) > budget:
            return {'status': 'pending_review', 'reason': 'estimated_budget_exceeded'}
        if set(card_ids) & {card['cardId'] for card in baseline_cards}:
            return {'status': 'pending_review', 'reason': 'created_card_id_conflict'}
        journal.update(generationId=generation_id, cardIds=card_ids, phase='generated')
        reconcile._atomic(root / JOURNAL, journal)
    for card_id in journal['cardIds']:
        try:
            verified, pending = refresh._verification(root, generation_id, card_id,
                compile_cfg, engine, verification_runner, recovery_only)
        except topical.TopicalError as exc:
            return {'status': 'pending_review', 'reason': exc.code}
        if pending: return {'status': 'pending_review', 'reason': pending}
        if verified != 'backend_pass':
            return {'status': 'pending_review', 'reason': 'created_backend_pass_required'}
    journal['phase'] = 'verified'; reconcile._atomic(root / JOURNAL, journal)
    cards = [experiment.load_staged_card(root, generation_id, cid) for cid in journal['cardIds']]
    _project_root, wiki, _collection = publisher._project(root)
    published_other = (any(wiki.glob('*.md'))
                       or any((wiki / 'topical-v2').glob('*/*.md')))
    proof_dir = root / 'topical-create-review-proofs' / generation_id
    publication_dir = wiki / 'topical-v2' / generation_id
    # Once publication starts, the immutable proofs fix the reviewed candidate
    # set. A later QMD index can reorder its top hits, so searching again would
    # change a completed review during crash recovery.
    resume_review = (all((proof_dir / (cid + '.json')).is_file()
                         for cid in journal['cardIds'])
                     and any((publication_dir / (cid + '.md')).is_file()
                             for cid in journal['cardIds']))
    batch_exclude = [(generation_id, cid) for cid in journal['cardIds']]
    reviewed = {}
    for card in cards:
        if {row['path'] for row in card['sourceRevisions']} != {source_path}:
            return {'status': 'pending_review', 'reason': 'created_card_source_mismatch'}
    for card in ([] if resume_review else cards):
        try:
            matches = similarity.review_candidates(root, card, generation_id,
                exclude=batch_exclude) if published_other else []
            # Review the immutable staged sibling pages as one batch. A retry
            # sees the same pairs even if an earlier publish partly completed.
            for sibling in cards:
                if sibling['cardId'] == card['cardId']:
                    continue
                sibling_id = sibling['cardId']
                sibling_page = topical.read_generation_bytes(root, generation_id,
                    f'cards/{sibling_id}.md')
                matches.append({'path': f'topical-v2/{generation_id}/{sibling_id}.md',
                    'pageSha256': topical.digest(sibling_page), 'card': sibling})
            result = similarity.evaluate(root, card, generation_id, matches)
            original_pairs = result['pairs']
            if result['status'] == 'pending_review':
                journal['phase'] = 'awaiting_similarity'
                reconcile._atomic(root / JOURNAL, journal)
                result = similarity.auto_review_pending(root, card, generation_id,
                    matches, compile_cfg, engine, verification_runner,
                    recovery_only=recovery_only)
        except topical.TopicalError as exc:
            return {'status': 'pending_review', 'reason': exc.code}
        if result['status'] == 'pending_review':
            journal['phase'] = 'awaiting_similarity'; reconcile._atomic(root / JOURNAL, journal)
            return {'status': 'pending_review', 'reason': result.get('reason', 'similarity_unresolved'),
                    'pairs': result.get('pairs', original_pairs),
                    'reviewedThisPass': result.get('reviewedThisPass', 0)}
        reviewed[card['cardId']] = result['pairs']
    if reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
        reconcile.finish_mock_batch(root, source_roots, batch['batchId'],
                                    success=False, skip_paths=skip_paths)
        (root / JOURNAL).unlink()
        return {'status': 'superseded_source_changed'}
    if not resume_review:
        for card_id in journal['cardIds']:
            publisher.save_create_review_proof(root, generation_id, card_id,
                                               reviewed[card_id])
    for card_id in journal['cardIds']:
        publisher.publish(root, generation_id, card_id)
    journal['phase'] = 'published'; reconcile._atomic(root / JOURNAL, journal)
    ready = publisher.sync(root, allow_stale=True)
    journal['phase'] = 'qmd_synced'; reconcile._atomic(root / JOURNAL, journal)
    finished = reconcile.finish_created_batch(root, source_roots, batch['batchId'],
                                              generation_id, cards, source_path,
                                              skip_paths=skip_paths)
    (root / JOURNAL).unlink()
    return {'status': 'backend_created_synced', 'ready': ready,
            'finished': finished, 'generationId': generation_id, 'cards': len(cards)}
