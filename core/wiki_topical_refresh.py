"""Recoverable v2 refresh in an explicitly marked isolated project.

Every external attempt is reserved by wiki_topical_backend. Recovery may reuse
only a completed recorded response or a verified staged artifact. An uncertain
attempt is never called again automatically. Filesystem/QMD steps are repeated
idempotently until the durable reconcile baseline is settled.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_replacement as replacement
import topical_stop_budget as stop_budget
import wiki_topical_similarity as similarity
import wiki_verify_worker as verifier
import wiki_compile_worker as worker

JOURNAL = 'topical-refresh-state.json'
LOCK = '.topical-refresh.lock'
AUTO_CONFIG = '.topical-auto-refresh.json'


def _read(root: Path):
    path = root / JOURNAL
    if not path.exists() and not path.is_symlink():
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise topical.TopicalError('unsafe_auto_refresh_config') from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1_000_000):
            raise topical.TopicalError('unsafe_refresh_journal')
        row = json.loads(os.read(fd, info.st_size + 1))
    finally:
        os.close(fd)
    if (not isinstance(row, dict) or row.get('schema') != 'qmd-topical-refresh-v1'
            or not {'batchId', 'oldGenerationId', 'oldCardId', 'oldCardSha256',
                    'sourceRoots', 'phase', 'generationId', 'preCandidates', 'engine'} <= set(row)):
        raise topical.TopicalError('invalid_refresh_journal')
    return row


def _save(root: Path, row: dict):
    reconcile._atomic(root / JOURNAL, row)


def _foreign_projection(root, generation_id, cards, projection, retire_ids,
                        snapshot):
    by_id = {card['cardId']: card for card in cards}
    for card_id in set(by_id) & (set(projection) - set(retire_ids)):
        fresh = reconcile._projection({card_id: by_id[card_id]}, snapshot,
                                       {card_id})[card_id]
        if not reconcile.same_generation_projection(root, generation_id,
                card_id, projection[card_id], fresh):
            return True
    return False


def _baseline_cards(root: Path, journal=None):
    if journal is None:
        _root, wiki, _collection = publisher._project(root)
        pages = sorted((wiki / 'topical-v2').glob('*/*.md'))
        refs = []
        for page in pages:
            if page.is_symlink() or not page.is_file():
                raise topical.TopicalError('unsafe_published_card')
            refs.append({'generationId': page.parent.name, 'cardId': page.stem})
    else:
        refs = journal.get('baselineCards')
        if not isinstance(refs, list):
            raise topical.TopicalError('refresh_baseline_missing')
    cards = []
    seen = set()
    for ref in refs:
        gid, card_id = ref['generationId'], ref['cardId']
        if card_id in seen:
            raise topical.TopicalError('duplicate_baseline_card')
        seen.add(card_id)
        manifest = json.loads(topical.read_generation_bytes(root, gid, 'manifest.json'))
        entry = next((row for row in manifest.get('cards', [])
                      if row.get('cardId') == card_id), None)
        sidecar = topical.read_generation_bytes(root, gid,
                                                f'cards/{card_id}.evidence.json')
        if entry is None or hashlib.sha256(sidecar).hexdigest() != entry.get('evidenceSha256'):
            raise topical.TopicalError('refresh_baseline_changed')
        cards.append(json.loads(sidecar))
    return refs, cards


def _recorded(root: Path, kind: str, payload: dict, attempt_id: str = 'initial'):
    """Return a completed audit response, None, or an explicit pending state."""
    request_sha = topical.digest(topical.encoded(payload))
    audit = root / 'topical-backend-audit' / f'{kind}.{request_sha}.{attempt_id}.attempt.json'
    if not audit.exists() and not audit.is_symlink():
        return None, None, None
    fd = backend._audit_fd(root)
    try:
        row = backend._read_audit(fd, audit.name)
    finally:
        os.close(fd)
    if (row.get('schema') != backend.ATTEMPT_SCHEMA or
            row.get('requestSha256') != request_sha or row.get('kind') != kind or
            row.get('attemptId') != attempt_id or
            not isinstance(row.get('transportSha256'), str) or
            len(row['transportSha256']) != 64):
        raise topical.TopicalError('backend_attempt_changed')
    if row.get('state') == 'completed' and 'adapterResponse' in row:
        return row['adapterResponse'], None, row
    return None, kind + '_attempt_' + str(row.get('state', 'unknown')), row


def _generation_transport_reviewed(root, payload, response, audit, compile_cfg, engine):
    """Validate a completed generation receipt before either create or refresh replays it."""
    argv = worker.resolve_extractor_argv(compile_cfg, engine)
    old_batch = (topical.digest(topical.encoded(payload)) ==
                 backend.RC17_GENERATION_REQUEST and
                 topical.digest(topical.encoded(response)) ==
                 backend.RC17_GENERATION_RESPONSE)
    reviewed_generation = backend.RC17_GENERATION if old_batch else ''
    return (argv is not None and audit is not None and
            audit.get('transportSha256') in
            backend.accepted_transport_fingerprints(root, reviewed_generation, argv))


def _generation(root, surviving, compile_cfg, engine, pre_context, runner, recovery_only):
    pre_context = similarity.generation_context(root, pre_context)
    payload = experiment.generation_contract(root, surviving)
    payload['existingWikiCandidates'] = pre_context
    payload['timeout'] = 420
    response, pending, audit = _recorded(root, 'generation', payload)
    if pending:
        return None, pending
    if response is not None:
        if not _generation_transport_reviewed(root, payload, response, audit,
                                              compile_cfg, engine):
            return None, 'generation_policy_changed'
        return backend.stage_generation_response(root, response, requested_sources=surviving,
                                                  reuse_existing=True), None
    if recovery_only:
        return None, 'generation_requires_explicit_backend'
    return backend.run_generation_backend(root, surviving, compile_cfg, engine,
        existing_wiki_candidates=pre_context, allow_backend_execution=True,
        runner=runner), None


def _verification(root, generation_id, card_id, compile_cfg, engine, runner, recovery_only):
    try:
        publisher._attested(root, generation_id, card_id)
        return 'backend_pass', None
    except (FileNotFoundError, topical.TopicalError):
        pass
    request = backend.verification_payload(root, generation_id, card_id)
    response, pending, audit = _recorded(root, 'verification', request)
    if pending:
        return None, pending
    if response is not None:
        selected_engine = audit.get('selectedEngine') if audit else None
        if not isinstance(selected_engine, str) or not selected_engine:
            return None, 'verification_policy_unavailable'
        vcfg = verifier.verify_cfg_of(compile_cfg)
        attempts, _mode, _reason = verifier.plan_verify_attempts(
            compile_cfg, vcfg, engine, set())
        if (not attempts or attempts[0]['engine'] != selected_engine or
            audit.get('transportSha256') not in backend.accepted_transport_fingerprints(
                root, generation_id, attempts[0]['argv'])):
            return None, 'verification_policy_changed'
        parsed = {key: response.get(key) for key in ('verdict', 'checks', 'reasons')}
        result = backend.validate_verification_response(root, request, parsed,
                                                       mode='backend', engine=selected_engine)
        if result['status'] == 'backend_pass':
            backend.publish_pass_attestation(root, result)
        return result['status'], None
    if recovery_only:
        return None, 'verification_requires_explicit_backend'
    result = backend.run_verification_backend(root, generation_id, card_id,
        compile_cfg, engine, allow_backend_execution=True, runner=runner)
    return result['status'], None


def refresh_one(root: Path, source_roots: list[str], old_card: dict,
                old_generation_id: str, compile_cfg: dict, engine: str, *,
                allow_backend_execution: bool = False,
                generation_runner=None, verification_runner=None,
                recovery_only: bool = False, skip_paths=()) -> dict:
    if not recovery_only and not allow_backend_execution:
        raise topical.TopicalError('backend_execution_not_enabled')
    if not recovery_only and (generation_runner is None or verification_runner is None):
        raise topical.TopicalError('explicit_backend_runner_required')
    root = experiment.require_sandbox(root)
    old_id = old_card.get('cardId') if isinstance(old_card, dict) else None
    if not isinstance(old_id, str) or not topical.CARD_ID.fullmatch(old_id):
        raise topical.TopicalError('invalid_card_id')
    lock = root / LOCK
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise topical.TopicalError('unsafe_refresh_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise topical.TopicalError('refresh_busy') from exc
        return _refresh_locked(root, source_roots, old_card, old_generation_id,
                               compile_cfg, engine, generation_runner,
                               verification_runner, recovery_only, skip_paths)
    finally:
        os.close(fd)


def _refresh_locked(root, source_roots, old_card, old_generation_id,
                    compile_cfg, engine, generation_runner, verification_runner,
                    recovery_only, skip_paths):
    old_id = old_card['cardId']
    old_sha = topical.digest(topical.encoded(old_card))
    journal = _read(root)
    if journal is None:
        if recovery_only:
            return {'status': 'no_pending_refresh'}
        refs, baseline_cards = _baseline_cards(root)
        if old_card not in baseline_cards:
            raise topical.TopicalError('old_card_not_published')
        initial = reconcile.start_batch(root, source_roots, baseline_cards,
                                        trusted_card_ids=[card['cardId'] for card in baseline_cards],
                                        skip_paths=skip_paths)
        source_paths = {row['path'] for row in old_card['sourceRevisions']}
        if not initial['started'] and initial['reason'] != 'batch_running':
            return {'status': initial['reason']}
        batch = initial['batch']
        # Stop may have claimed this exact source batch before SessionStart's
        # backend worker. The refresh lock serializes refresh/create owners;
        # an existing create journal or an unrelated source batch is never
        # adopted as this card's refresh.
        create_journal = root / 'topical-create-state.json'
        if (create_journal.exists() or create_journal.is_symlink() or
                not source_paths.intersection(batch['sources'])):
            return {'status': 'batch_running'}
        if not initial['started'] and reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
            # No refresh journal exists, so no backend attempt was made for
            # this card. Reconcile the changed source and claim its new final
            # snapshot; an already-running owner cannot pass our refresh lock.
            if not reconcile.supersede_changed_batch(root, source_roots,
                    batch['batchId'], skip_paths=skip_paths):
                return {'status': 'batch_running'}
            initial = reconcile.start_batch(root, source_roots, baseline_cards,
                trusted_card_ids=[card['cardId'] for card in baseline_cards],
                skip_paths=skip_paths)
            if not initial['started']:
                return {'status': initial['reason']}
            batch = initial['batch']
            if not source_paths.intersection(batch['sources']):
                return {'status': 'batch_running'}
        if reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
            return {'status': 'pending_review', 'reason': 'source_changed_during_refresh'}
        journal = {'schema': 'qmd-topical-refresh-v1', 'batchId': initial['batch']['batchId'],
                   'oldGenerationId': old_generation_id, 'oldCardId': old_id,
                   'oldCardSha256': old_sha, 'sourceRoots': sorted(source_roots),
                   'phase': 'claimed', 'generationId': None, 'preCandidates': [],
                   'baselineCards': refs,
                   'skipPaths': list(skip_paths),
                   'engine': engine,
                   'compilePolicySha256': topical.digest(topical.encoded(compile_cfg))}
        _save(root, journal)
    elif (journal['oldGenerationId'] != old_generation_id or journal['oldCardId'] != old_id
          or journal['oldCardSha256'] != old_sha or journal['sourceRoots'] != sorted(source_roots)
          or journal['engine'] != engine or
          journal.get('skipPaths', []) != list(skip_paths) or
          (not recovery_only and journal.get('compilePolicySha256') !=
           topical.digest(topical.encoded(compile_cfg)))):
        raise topical.TopicalError('refresh_journal_conflict')
    baseline_refs, baseline_cards = _baseline_cards(root, journal)
    retire_generation = {row['cardId']: row['generationId'] for row in baseline_refs}
    state = reconcile._read(root)
    batch = state.get('inFlight') if state else None
    if not batch:
        if state and state.get('lastCompletedBatchId') == journal['batchId']:
            (root / JOURNAL).unlink()
            return {'status': 'already_completed', 'generationId': journal['generationId']}
        raise topical.TopicalError('refresh_batch_missing')
    if batch['batchId'] != journal['batchId']:
        raise topical.TopicalError('refresh_batch_changed')
    if reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
        return {'status': 'pending_review', 'reason': 'source_changed_during_refresh'}
    projection = reconcile.safe_projection(root, source_roots, baseline_cards,
        trusted_card_ids=[card['cardId'] for card in baseline_cards], skip_paths=skip_paths)
    if projection[old_id]['state'] != 'excluded_stale':
        raise topical.TopicalError('stale_card_required')
    surviving = sorted({row['path'] for row in old_card['sourceRevisions']
                        if row['path'] in batch['snapshot']})
    if len(surviving) > 3:
        raise topical.TopicalError('too_many_refresh_sources')
    generation_id = journal['generationId']
    new_cards = []
    if surviving:
        if journal['phase'] == 'claimed':
            lead = ' '.join(old_card['lead'].split())
            try:
                matches = similarity.retrieve(root, lead, exclude=(old_generation_id, old_id))
            except topical.TopicalError as exc:
                return {'status': 'pending_review', 'reason': exc.code}
            journal['preCandidates'] = [{'path': x['path'], 'lead': x['lead']} for x in matches]
            journal['phase'] = 'pre_retrieved'; _save(root, journal)
        if generation_id is None:
            try:
                generated, pending = _generation(root, surviving, compile_cfg, engine,
                                                  journal['preCandidates'], generation_runner,
                                                  recovery_only)
            except topical.TopicalError as exc:
                return {'status': 'pending_review', 'reason': exc.code}
            if pending:
                return {'status': 'pending_review', 'reason': pending}
            generation_id = generated['generationId']
            card_ids = [row['cardId'] for row in generated['cards']]
            if (len(card_ids) != len(set(card_ids))
                    or any(cid in projection and
                           projection[cid]['state'] != 'excluded_stale'
                           for cid in card_ids)):
                return {'status': 'pending_review', 'reason': 'replacement_card_id_mismatch'}
            staged = [experiment.load_staged_card(root, generation_id, cid)
                      for cid in card_ids]
            try:
                lineage = replacement.derive(root, baseline_cards, staged,
                    projection, surviving, batch['snapshot'], old_id)
            except topical.TopicalError as exc:
                return {'status': 'pending_review', 'reason': exc.code}
            if _foreign_projection(root, generation_id, staged, projection,
                    {link['oldCardId'] for link in lineage['links']},
                    batch['snapshot']):
                return {'status': 'pending_review',
                        'reason': 'replacement_source_mismatch'}
            journal['cardIds'] = card_ids
            journal['retireCardIds'] = [link['oldCardId']
                                        for link in lineage['links']]
            journal['replacementLineage'] = lineage
            journal['generationId'] = generation_id
            journal['phase'] = 'generated'; _save(root, journal)
        card_ids = journal.get('cardIds', [old_id])
        retire_ids = journal.get('retireCardIds', [old_id])
        lineage = journal.get('replacementLineage')
        if (old_id not in retire_ids or len(card_ids) != len(set(card_ids))
                or len(retire_ids) != len(set(retire_ids))
                or (lineage is None and (old_id not in card_ids or
                    not set(retire_ids) <= set(card_ids)))):
            raise topical.TopicalError('refresh_card_set_changed')
        manifest = json.loads(topical.read_generation_bytes(
            root, generation_id, 'manifest.json'))
        rows = manifest.get('cards')
        if (manifest.get('generationId') != generation_id
                or not isinstance(rows, list)
                or any(not isinstance(row, dict) for row in rows)
                or card_ids != [row.get('cardId') for row in rows]
                or (lineage is not None and retire_ids !=
                    [link['oldCardId'] for link in lineage['links']])
                or (lineage is None and retire_ids != [cid for cid in card_ids
                    if cid in projection and projection[cid]['state'] == 'excluded_stale'])):
            raise topical.TopicalError('refresh_card_set_changed')
        new_cards = [experiment.load_staged_card(root, generation_id, cid)
                     for cid in card_ids]
        if lineage is not None:
            actual = replacement.derive(root, baseline_cards, new_cards,
                projection, surviving, batch['snapshot'], old_id)
            if (actual != lineage or retire_ids !=
                    [link['oldCardId'] for link in actual['links']]):
                raise topical.TopicalError('replacement_lineage_changed')
            if _foreign_projection(root, generation_id, new_cards, projection,
                    set(retire_ids), batch['snapshot']):
                raise topical.TopicalError('replacement_source_mismatch')
        if any(not {row['path'] for row in card['sourceRevisions']} <= set(surviving)
               for card in new_cards):
            return {'status': 'pending_review', 'reason': 'replacement_source_mismatch'}
        old_by_id = {card['cardId']: card for card in baseline_cards}
        new_by_id = {card['cardId']: card for card in new_cards}
        # A generated ID alone cannot authorize retirement of another stale
        # card. Additional cards must be entirely covered by this selected
        # source set. The primary card may have a deleted source path, while
        # its replacement still needs to cover every surviving old source.
        pairs = ({link['oldCardId']: link['newCardId'] for link in lineage['links']}
                 if lineage is not None else {cid: cid for cid in retire_ids})
        for cid, new_id in pairs.items():
            old_paths = {row['path'] for row in old_by_id[cid]['sourceRevisions']}
            replacement_paths = {row['path'] for row in
                                 new_by_id[new_id]['sourceRevisions']}
            if ((cid != old_id and not old_paths <= set(surviving))
                    or not (old_paths & set(surviving)) <= replacement_paths):
                return {'status': 'pending_review',
                        'reason': 'replacement_source_mismatch'}
        for card_id in card_ids:
            try:
                verified, pending = _verification(root, generation_id, card_id,
                    compile_cfg, engine, verification_runner, recovery_only)
            except topical.TopicalError as exc:
                return {'status': 'pending_review', 'reason': exc.code}
            if pending:
                return {'status': 'pending_review', 'reason': pending}
            if verified != 'backend_pass':
                return {'status': 'pending_review',
                        'reason': 'replacement_backend_pass_required'}
        journal['phase'] = 'verified'; _save(root, journal)
        proof_dir = root / 'topical-create-review-proofs' / generation_id
        publication_dir = root / '.auto-context/wiki/topical-v2' / generation_id
        resume_review = (all((proof_dir / (cid + '.json')).is_file()
                             for cid in card_ids)
                         and any((publication_dir / (cid + '.md')).is_file()
                                 for cid in card_ids))
        reviewed = {}
        excluded = [(retire_generation[cid], cid) for cid in retire_ids]
        excluded += [(generation_id, cid) for cid in card_ids]
        for card in ([] if resume_review else new_cards):
            try:
                matches = similarity.review_candidates(root, card,
                    generation_id, exclude=excluded)
                for sibling in new_cards:
                    if sibling['cardId'] == card['cardId']:
                        continue
                    sibling_id = sibling['cardId']
                    sibling_page = topical.read_generation_bytes(root,
                        generation_id, f'cards/{sibling_id}.md')
                    matches.append({'path': f'topical-v2/{generation_id}/{sibling_id}.md',
                        'pageSha256': topical.digest(sibling_page), 'card': sibling})
                result = similarity.evaluate(root, card, generation_id, matches)
                original_pairs = result['pairs']
                if result['status'] == 'pending_review':
                    journal['phase'] = 'awaiting_similarity'; _save(root, journal)
                    result = similarity.auto_review_pending(root, card,
                        generation_id, matches, compile_cfg, engine,
                        verification_runner, recovery_only=recovery_only)
            except topical.TopicalError as exc:
                return {'status': 'pending_review', 'reason': exc.code}
            if result['status'] == 'pending_review':
                journal['phase'] = 'awaiting_similarity'; _save(root, journal)
                return {'status': 'pending_review',
                        'reason': result.get('reason', 'similarity_unresolved'),
                        'cardId': card['cardId'],
                        'pairs': result.get('pairs', original_pairs),
                        'reviewedThisPass': result.get('reviewedThisPass', 0)}
            reviewed[card['cardId']] = result['pairs']
        # Pair rows can be numerous and are persisted as per-card immutable
        # publication proofs below. Duplicating them in the bounded journal
        # would make a later crash unrecoverable at the journal size limit.
        journal['phase'] = 'similarity_clear'; _save(root, journal)
    if reconcile._scan(root, source_roots, skip_paths) != batch['snapshot']:
        return {'status': 'pending_review', 'reason': 'source_changed_during_refresh'}
    if new_cards:
        if not resume_review:
            for card_id in card_ids:
                publisher.save_create_review_proof(
                    root, generation_id, card_id, reviewed[card_id])
        for card_id in card_ids:
            publisher.publish(root, generation_id, card_id)
        journal['phase'] = 'new_published'; _save(root, journal)
        # Keep the old published pages through the replacement's verified
        # QMD document and vector ACK. Stale old pages are skipped by recall;
        # the second sync below removes them only after this first ACK.
        publisher.sync(root, allow_stale=True)
    retiring = [card for card in baseline_cards if card['cardId'] in
                (retire_ids if surviving else [old_id])]
    if len(retiring) != len(retire_ids if surviving else [old_id]):
        raise topical.TopicalError('refresh_retirement_card_missing')
    retired_rows = [publisher.retire_stale(root, retire_generation[card['cardId']],
        card['cardId'], source_roots, [card]) for card in retiring]
    retired = retired_rows[0] if len(retired_rows) == 1 else retired_rows
    journal['phase'] = 'old_retired'; _save(root, journal)
    ready = publisher.sync(root, allow_empty=not new_cards, allow_stale=True,
                           reclaim_retired=True)
    journal['phase'] = 'qmd_synced'; _save(root, journal)
    finished = reconcile.finish_backend_batch(root, source_roots,
        batch['batchId'], generation_id, new_cards,
        old_card_ids=[card['cardId'] for card in retiring],
        replacement_lineage=journal.get('replacementLineage'),
        old_cards=baseline_cards, primary_old_id=old_id,
        old_generation_ids={card['cardId']: retire_generation[card['cardId']]
                            for card in retiring},
        surviving_paths=surviving if surviving else None,
        settled_paths=sorted({row['path'] for card in retiring
                              for row in card['sourceRevisions']}),
        skip_paths=skip_paths)
    (root / JOURNAL).unlink()
    return {'status': 'backend_verified_synced', 'retired': retired,
            'ready': ready, 'finished': finished, 'generationId': generation_id,
            'replacementCards': len(new_cards)}


def recover_pending(root: Path) -> dict:
    """One-shot recovery of recorded work; never initiates an external call."""
    root = experiment.require_sandbox(root)
    journal = _read(root)
    if journal is None:
        return {'status': 'no_pending_refresh'}
    try:
        policy = read_auto_policy(root)
        if policy is None:
            return {'status': 'pending_review', 'reason': 'auto_teacher_policy_required'}
        if not policy['enabled']:
            return {'status': 'pending_review', 'reason': 'auto_teacher_policy_disabled'}
        compile_cfg = policy['compileCfg']
        if (topical.digest(topical.encoded(compile_cfg)) !=
                journal['compilePolicySha256']):
            return {'status': 'pending_review', 'reason': 'refresh_policy_changed'}
        old = json.loads(topical.read_generation_bytes(root, journal['oldGenerationId'],
            f"cards/{journal['oldCardId']}.evidence.json"))
        result = refresh_one(root, journal['sourceRoots'], old,
            journal['oldGenerationId'], compile_cfg, journal['engine'], recovery_only=True,
            skip_paths=journal.get('skipPaths', []))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'pending_review', 'reason': getattr(exc, 'code', 'recovery_unavailable')}
    return result


def read_auto_policy(root: Path) -> dict | None:
    """Read the full owner policy before a hook may scan or claim work."""
    root = experiment.require_sandbox(root)
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        topical_story_scope.read(root)
    path = root / AUTO_CONFIG
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise topical.TopicalError('unsafe_auto_refresh_config')
        try:
            cfg = json.loads(os.read(fd, info.st_size + 1))
        except (ValueError, UnicodeDecodeError) as exc:
            raise topical.TopicalError('invalid_auto_refresh_config') from exc
    finally:
        os.close(fd)
    if (not isinstance(cfg, dict) or set(cfg) != {'schema', 'enabled', 'sourceRoots',
            'engine', 'compileCfg', 'maxEstimatedCents'} or
            cfg['schema'] != 'qmd-topical-auto-refresh-v1' or
            type(cfg['enabled']) is not bool or cfg['engine'] != 'codex' or
            not isinstance(cfg['sourceRoots'], list) or not cfg['sourceRoots'] or
            not isinstance(cfg['compileCfg'], dict) or
            type(cfg['maxEstimatedCents']) is not int or
            not 20 <= cfg['maxEstimatedCents'] <= 160):
        raise topical.TopicalError('invalid_auto_refresh_config')
    try:
        if not all(isinstance(value, str) and
                   reconcile.safe_rel(value, explicit_root=True) == value
                   for value in cfg['sourceRoots']):
            raise ValueError('invalid_source_root')
    except ValueError as exc:
        raise topical.TopicalError('invalid_auto_refresh_config') from exc
    return cfg


def auto_refresh_pending(root: Path, *, generation_runner=None,
                         verification_runner=None, skip_paths=(),
                         expected_policy_sha256=None) -> dict:
    """One bounded, explicitly opted-in project card/source operation."""
    root = experiment.require_sandbox(root)
    cfg = read_auto_policy(root)
    if cfg is None:
        return {'status': 'pending_review', 'reason': 'auto_teacher_policy_required'}
    if not cfg['enabled']:
        return {'status': 'disabled'}
    if (expected_policy_sha256 is not None and
            stop_budget._digest(cfg) != expected_policy_sha256):
        return {'status': 'pending_review', 'reason': 'stop_batch_policy_changed'}
    source_roots = cfg['sourceRoots']
    publisher._project(root)
    publisher._qmd_runtime(root)
    state = reconcile._read(root)
    if state is None or state.get('sourceRoots') != sorted(set(source_roots)):
        return {'status': 'pending_review', 'reason': 'reconcile_baseline_required'}
    journal = _read(root)
    if journal is not None:
        _refs, cards = _baseline_cards(root, journal)
        old = next((card for card in cards if card['cardId'] == journal['oldCardId']), None)
        if old is None:
            raise topical.TopicalError('refresh_baseline_missing')
        gid = journal['oldGenerationId']
    else:
        import wiki_topical_create as creator
        create_journal = creator._read(root)
        if create_journal is not None:
            return creator.create_one(root, source_roots, create_journal['sourcePath'],
                cfg['compileCfg'], cfg['engine'], cfg['maxEstimatedCents'],
                generation_runner=generation_runner or backend.worker.run_extractor,
                verification_runner=verification_runner or backend.worker.run_extractor,
                skip_paths=skip_paths)
        refs, cards = _baseline_cards(root)
        projection = reconcile.safe_projection(root, source_roots, cards,
            trusted_card_ids=[card['cardId'] for card in cards], skip_paths=skip_paths)
        stale = sorted(card_id for card_id, row in projection.items()
                       if row['state'] == 'excluded_stale')
        active_batch = state.get('inFlight') if state else None
        active_paths = set(active_batch['sources']) if active_batch else set()
        if stale and active_paths:
            # A batch claimed for another card or a newly created source must
            # not be taken over by the alphabetically first stale card.
            matching = [card_id for card_id in stale if any(
                row['path'] in active_paths for row in next(
                    card for card in cards if card['cardId'] == card_id)['sourceRevisions'])]
            if matching:
                stale = matching
            else:
                claimed_paths = {row['path'] for card in cards
                                 for row in card['sourceRevisions']}
                new_paths = sorted(path for path in active_paths
                                   if path not in claimed_paths and
                                   state['queue'].get(path, {}).get('currentSha256') is not None)
                if new_paths:
                    return creator.create_one(root, source_roots, new_paths[0],
                        cfg['compileCfg'], cfg['engine'], cfg['maxEstimatedCents'],
                        generation_runner=generation_runner or backend.worker.run_extractor,
                        verification_runner=verification_runner or backend.worker.run_extractor,
                        skip_paths=skip_paths)
                return {'status': 'batch_running'}
        if not stale:
            claimed = {row['path'] for card in cards for row in card['sourceRevisions']}
            unclaimed = [path for path, row in sorted(state['queue'].items())
                         if path not in claimed and row['currentSha256'] is not None]
            if unclaimed:
                return creator.create_one(root, source_roots, unclaimed[0],
                    cfg['compileCfg'], cfg['engine'], cfg['maxEstimatedCents'],
                    generation_runner=generation_runner or backend.worker.run_extractor,
                    verification_runner=verification_runner or backend.worker.run_extractor,
                    skip_paths=skip_paths)
            if state['queue']:
                return {'status': 'pending_review', 'reason': 'unclaimed_source_change'}
            return {'status': 'no_net_changes'}
        old_id = stale[0]
        old = next(card for card in cards if card['cardId'] == old_id)
        gid = next(ref['generationId'] for ref in refs if ref['cardId'] == old_id)
    return refresh_one(root, source_roots, old, gid, cfg['compileCfg'], cfg['engine'],
        allow_backend_execution=True,
        generation_runner=generation_runner or backend.worker.run_extractor,
        verification_runner=verification_runner or backend.worker.run_extractor,
        skip_paths=skip_paths)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('recover', 'auto'):
        raise SystemExit(2)
    fn = recover_pending if sys.argv[1] == 'recover' else auto_refresh_pending
    print(json.dumps(fn(Path(sys.argv[2])), ensure_ascii=False, sort_keys=True))
