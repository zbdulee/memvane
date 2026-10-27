"""Immutable worker lineage for the one scoped synthetic story wiki.

A topical worker is not a host editor. Its registration binds verified source
snapshots, staged generation, backend pass, publication proof and active QMD
rows. Failed registration leaves the card pending for a later index wake.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .safe_source import snapshot_beneath
from .store import canonical
from .synthetic_experiment import shared_for, story_for
from .synthetic_scope import NOVEL_ROOT, WIKI_COLLECTION, generated_path

METHOD = 'topical_worker_v1'
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_SOURCES = 8
MAX_SOURCE_BYTES = 4 * 1024 * 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _scope(root, project):
    import topical_story_scope
    project = Path(project).resolve(strict=True)
    root = Path(root).resolve(strict=True)
    if project != NOVEL_ROOT.resolve(strict=True) or root != shared_for(project):
        raise ValueError('topical_worker_project_invalid')
    story = story_for(project).resolve(strict=True)
    scope = topical_story_scope.read(story)
    if scope['project'] != project:
        raise ValueError('topical_worker_project_invalid')
    return story, scope


def _identity(story, generation_id, card_id):
    import wiki_topical as topical
    from wiki_topical_experiment import GENERATION_ID
    if (not isinstance(generation_id, str) or not GENERATION_ID.fullmatch(generation_id)
            or not isinstance(card_id, str) or not topical.CARD_ID.fullmatch(card_id)):
        raise ValueError('topical_worker_identity_invalid')
    relative = '.auto-context/wiki/topical-v2/' + generation_id + '/' + card_id + '.md'
    return relative, 'topical-v2/' + generation_id + '/' + card_id + '.md'


def _provider_receipts(story, generation_id, card_id, record):
    """Bind this staged card and pass to completed, reviewed transport audits."""
    import os
    import topical_story_scope
    import wiki_compile_worker as adapter
    import wiki_topical as topical
    import wiki_topical_backend as backend
    import wiki_topical_experiment as experiment
    scope = topical_story_scope.read(story)
    policy = json.loads((story / topical_story_scope.AUTO_POLICY).read_text())
    argv = adapter.resolve_extractor_argv(policy['compileCfg'], 'codex')
    transports = backend.accepted_transport_fingerprints(story, generation_id, argv)
    manifest = json.loads(topical.read_generation_bytes(story, generation_id, 'manifest.json'))
    staged = experiment.load_staged_card(story, generation_id, card_id)
    fd = backend._audit_fd(story)
    try:
        names = os.listdir(fd)
        if len(names) > 4096:
            raise ValueError('topical_worker_audit_capacity')
        audits = {name: backend._read_audit(fd, name) for name in names
                  if name.endswith(('.attempt.json', '.generation.json', '.verification.json'))}
    finally:
        os.close(fd)

    def completed(kind, request_sha, response):
        prefix = kind + '.' + request_sha + '.'
        matches = [(name, row) for name, row in audits.items()
                   if name.startswith(prefix) and name.endswith('.attempt.json')
                   and row.get('schema') == backend.ATTEMPT_SCHEMA
                   and row.get('kind') == kind and row.get('requestSha256') == request_sha
                   and row.get('state') == 'completed'
                   and row.get('transportSha256') in transports
                   and row.get('adapterResponse') == response
                   and row.get('selectedEngine', 'codex') == 'codex']
        if len(matches) != 1:
            raise ValueError('topical_worker_completed_attempt_missing')
        return _sha(topical.encoded(matches[0][1]))

    def generation_request_valid(audit, request_sha):
        context = audit.get('requestContext')
        if not isinstance(context, dict) or set(context) != {
                'sourcePaths','leadBudgetChars','existingWikiCandidates'}:
            return False
        try:
            request = experiment.generation_contract(
                story, context['sourcePaths'], context['leadBudgetChars'])
            if context['existingWikiCandidates'] is not None:
                topical_story_scope.validate_wiki_candidates(
                    story, context['existingWikiCandidates'])
                request['existingWikiCandidates'] = context['existingWikiCandidates']
            request['timeout'] = 420
            return topical.digest(topical.encoded(request)) == request_sha
        except (KeyError, TypeError, ValueError, topical.TopicalError):
            return False

    generation = None
    for name, audit in audits.items():
        if not name.startswith('generation.') or not name.endswith('.attempt.json'):
            continue
        raw = audit.get('adapterResponse')
        if not isinstance(raw, dict) or raw.get('schema') != topical.SCHEMA or not isinstance(raw.get('cards'), list):
            continue
        # The adapter may attach transport metadata. The staging path compiles
        # only schema/cards, so replay that exact input for identity checks.
        normalized = {'schema': raw['schema'], 'cards': copy.deepcopy(raw['cards'])}
        try:
            for card in normalized['cards']:
                for claim in card['claims']:
                    for span in claim['evidence']:
                        revision, _body, _lines = topical.source_snapshot(story, span['sourcePath'], {})
                        span['sourceRevisionSha256'] = revision['sha256']
                        span['quoteSha256'] = topical.digest(span['quoteAnchor'].encode('utf-8'))
            cards, _cache = topical.normalize(story, normalized, manifest['leadBudget'])
            identity = topical.digest(topical.encoded({
                'cards': [{k:v for k,v in card.items() if k != 'candidate'} for card in cards],
                'leadBudget':manifest['leadBudget']}))[:24]
            match = next((card for card in cards if card['cardId'] == card_id), None)
            if identity != generation_id or match is None or {k:v for k,v in match.items() if k != 'candidate'} != staged:
                continue
            request_sha = audit['requestSha256']
            if (not name.startswith('generation.' + request_sha + '.')
                    or not generation_request_valid(audit, request_sha)):
                continue
            secondary = audits.get(request_sha + '.generation.json')
            if secondary is not None and (secondary.get('schema') != 'qmd-topical-backend-response-audit-v1'
                    or secondary.get('requestSha256') != request_sha
                    or secondary.get('adapterResponse') != raw):
                continue
            attempt_sha = completed('generation', request_sha, raw)
            generation = {'attempt':attempt_sha}
            break
        except (KeyError, TypeError, ValueError, topical.TopicalError):
            continue
    if generation is None:
        raise ValueError('topical_worker_generation_receipt_missing')

    direct_request = backend.verification_payload(story, generation_id, card_id)
    response_sha = record['responseSha256']
    verification = None
    request_sha = topical.digest(topical.encoded(direct_request))
    for name, audit in audits.items():
        raw = audit.get('adapterResponse')
        if not isinstance(raw, dict):
            continue
        projected = {key:raw.get(key) for key in ('verdict','checks','reasons')}
        if (name.startswith('verification.' + request_sha + '.')
                and name.endswith('.attempt.json') and audit.get('state') == 'completed'
                and topical.digest(topical.encoded(projected)) == response_sha):
            verification = {'attempt':completed('verification', request_sha,
                                                raw)}
            break
    if verification is None:
        for name, audit in audits.items():
            if not name.endswith('.verification.json') or audit.get('schema') != 'qmd-topical-backend-response-audit-v1':
                continue
            raw = audit.get('adapterResponse')
            if not isinstance(raw, dict) or not isinstance(raw.get('cards'), list):
                continue
            rows = raw['cards']
            ids = [row.get('cardId') for row in rows if isinstance(row, dict)]
            if len(ids) != len(rows) or card_id not in ids or len(ids) != len(set(ids)):
                continue
            try:
                bundle = backend.verification_bundle_payload(story, generation_id, ids)
                bundle_sha = topical.digest(topical.encoded(bundle))
                selected = next(row for row in rows if row['cardId'] == card_id)
                response = {key:selected.get(key) for key in ('verdict','checks','reasons')}
                if (name != bundle_sha + '.verification.json' or audit.get('requestSha256') != bundle_sha
                        or topical.digest(topical.encoded(response)) != response_sha):
                    continue
                verification = {'attempt':completed('bundle_verification', bundle_sha, raw),
                                'response':_sha(topical.encoded(audit))}
                break
            except (KeyError, TypeError, ValueError, topical.TopicalError):
                continue
    if verification is None or record.get('engine') != 'codex':
        raise ValueError('topical_worker_verification_receipt_missing')
    return _sha(topical.encoded({'scopePolicySha256':scope['policySha256'],
                                 'generation':generation,'verification':verification}))


def _ack(project, source_rows, card_suffix, card_sha, *, require_vector=True):
    expected = [[row['collection'], row['path'].split('/', 1)[1], row['sha256']]
                for row in source_rows]
    expected.append([WIKI_COLLECTION, card_suffix, card_sha])
    from .laya_learning import selected as laya_selected
    bound = laya_selected(project)
    if bound is not None:
        return _laya_ack(bound, project, expected, require_vector=require_vector)
    raise ValueError('topical_worker_index_unselected')


def _laya_ack(bound, project, expected, *, require_vector):
    """Selected Laya route: sealed head membership (and vector rows) for the
    card; raw sources, never indexed by a v2 route, by their current bytes."""
    from .laya_learning import file_revision, membership_ack
    story = story_for(Path(project))
    for collection, relative, revision in expected[:-1]:
        if file_revision(Path(story) / generated_path(
                collection + '/' + relative)) != revision:
            raise ValueError('topical_worker_index_unacknowledged')
    try:
        membership_ack(project, [expected[-1]], require_vector=require_vector,
                       bound=bound)
    except ValueError as exc:
        reason = ('topical_worker_vector_unacknowledged'
                  if str(exc) == 'laya_learning_vector_unacknowledged'
                  else 'topical_worker_index_unacknowledged')
        raise ValueError(reason) from exc
    return expected


def _evidence(story, project, scope, generation_id, card_id, *, require_index):
    import wiki_topical as topical
    import wiki_topical_publish as publisher
    relative, card_suffix = _identity(story, generation_id, card_id)
    card = snapshot_beneath(story, relative)
    proof_path = story / 'topical-publication-proofs' / generation_id / (card_id + '.json')
    if proof_path.is_symlink() or not proof_path.is_file():
        raise ValueError('topical_worker_publication_missing')
    # publish() is idempotent for an existing page and checks its exact
    # verifier stamp against the immutable staged Markdown. Requiring both
    # page and proof first prevents this read path from creating either one.
    published = publisher.publish(story, generation_id, card_id)
    if published['sha256'] != _sha(card) or snapshot_beneath(story, relative) != card:
        raise ValueError('topical_worker_card_changed')
    sidecar_bytes = topical.read_generation_bytes(story, generation_id,
                                                  f'cards/{card_id}.evidence.json')
    sidecar = json.loads(sidecar_bytes)
    source_revisions = sidecar.get('sourceRevisions')
    if (not isinstance(source_revisions, list) or not 1 <= len(source_revisions) <= MAX_SOURCES
            or len({row.get('path') for row in source_revisions
                    if isinstance(row, dict)}) != len(source_revisions)):
        raise ValueError('topical_worker_sources_invalid')
    sources = []
    size = 0
    for expected in source_revisions:
        if not isinstance(expected, dict):
            raise ValueError('topical_worker_sources_invalid')
        current, _, _ = topical.source_snapshot(story, expected['path'], {})
        if current != expected:
            raise ValueError('topical_worker_source_changed')
        raw = snapshot_beneath(story, expected['path'])
        size += len(raw)
        if size > MAX_SOURCE_BYTES or _sha(raw) != expected['sha256']:
            raise ValueError('topical_worker_source_changed')
        sources.append({'revision': expected,
                        'bytesB64': base64.b64encode(raw).decode('ascii')})
    record = publisher._attested(story, generation_id, card_id)
    provider_sha = _provider_receipts(story, generation_id, card_id, record)
    staged = topical.read_generation_bytes(story, generation_id,
                                           f'cards/{card_id}.md')
    proof = publisher._publication_proof(story, generation_id, card_id,
                                         _sha(staged), record)
    if proof is None:
        raise ValueError('topical_worker_publication_missing')
    proof_sha = _sha(proof_path.read_bytes())
    if proof.get('createReviewSha256'):
        # Reuse the existing immutable provenance column, binding both the
        # original publication proof and the additive create-time review.
        proof_sha = topical.digest(topical.encoded([
            'qmd-publication-review-pair-v1', proof_sha,
            proof['createReviewSha256']]))
    expected_ack = [[row['revision']['collection'],
                     row['revision']['path'].split('/', 1)[1],
                     row['revision']['sha256']] for row in sources]
    expected_ack.append([WIKI_COLLECTION, card_suffix, _sha(card)])
    if require_index and _ack(project, [row['revision'] for row in sources],
                              card_suffix, _sha(card)) != expected_ack:
        raise ValueError('topical_worker_index_ack_changed')
    return {'cardPath':relative,'cardSha256':_sha(card),'cardBytes':card,
            'sourceSnapshots':canonical(sources),
            'evidenceSha256':_sha(sidecar_bytes),
            'attestationSha256':topical.digest(topical.encoded(record)),
            'providerReceiptsSha256':provider_sha,
            'publicationSha256':proof_sha,
            'scopePolicySha256':scope['policySha256'], 'indexAck':expected_ack}


def register(root, project, generation_id, card_id):
    """Register one indexed worker page; a retry is idempotent."""
    root, project = Path(root).resolve(strict=True), Path(project).resolve(strict=True)
    story, scope = _scope(root, project)
    evidence = _evidence(story, project, scope, generation_id, card_id,
                         require_index=True)
    from .shared import _db
    with _db(root) as db:
        row = db.execute('SELECT registration_id,state,source_snapshots,index_ack,provider_receipts_sha256,publication_sha256 '
                         'FROM topical_worker_versions WHERE card_path=? AND card_sha256=?',
                         (evidence['cardPath'], evidence['cardSha256'])).fetchone()
        if row is not None:
            if (row[1] != 'active' or row[2] != evidence['sourceSnapshots']
                    or json.loads(row[3]) != evidence['indexAck']
                    or row[4] != evidence['providerReceiptsSha256']
                    or row[5] != evidence['publicationSha256']):
                raise ValueError('topical_worker_registration_conflict')
            return {'registration_id':row[0], 'status':'already_registered'}
        if db.execute('SELECT 1 FROM source_card_versions WHERE card_path=? '
                      'UNION SELECT 1 FROM experiment_generated WHERE relative_path=?',
                      (evidence['cardPath'], evidence['cardPath'])).fetchone():
            raise ValueError('topical_worker_path_collision')
        fresh = _evidence(story, project, scope, generation_id, card_id,
                          require_index=True)
        if fresh != evidence:
            raise ValueError('topical_worker_changed_before_registration')
        registration = uuid.uuid4().hex
        db.execute('INSERT INTO topical_worker_versions VALUES '
                   '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (registration,str(project),str(story),evidence['cardPath'],
                    evidence['cardSha256'],evidence['cardBytes'],generation_id,card_id,
                    evidence['sourceSnapshots'],evidence['evidenceSha256'],
                    evidence['attestationSha256'],evidence['publicationSha256'],
                    evidence['scopePolicySha256'],evidence['providerReceiptsSha256'],
                    canonical(evidence['indexAck']),
                    'active',time.time()))
    return {'registration_id':registration,'status':'registered',
            'card_sha256':evidence['cardSha256'],'index_ack':evidence['indexAck']}


def registered_revision(db, root, project, candidate_id, revision, *, require_index=True):
    """Recheck every frozen artifact before candidate egress or gold reuse."""
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        return False
    try:
        root, project = Path(root).resolve(strict=True), Path(project).resolve(strict=True)
        card_relative = generated_path(candidate_id).as_posix()
        row = db.execute('SELECT project_root,story_root,card_sha256,card_bytes,'
                         'generation_id,card_id,source_snapshots,evidence_sha256,'
                         'attestation_sha256,publication_sha256,scope_policy_sha256,provider_receipts_sha256,'
                         'index_ack,state FROM topical_worker_versions WHERE card_path=? '
                         'ORDER BY rowid DESC LIMIT 1', (card_relative,)).fetchone()
        if row is None or row[-1] != 'active':
            return False
        story, scope = _scope(root, project)
        current = _evidence(story, project, scope, row[4], row[5],
                            require_index=require_index)
        stored = (row[0],row[1],row[2],row[3],row[6],row[7],row[8],row[9],row[10],row[11])
        expected = (str(project),str(story),revision,current['cardBytes'],
                    current['sourceSnapshots'],current['evidenceSha256'],
                    current['attestationSha256'],current['publicationSha256'],
                    current['scopePolicySha256'],current['providerReceiptsSha256'])
        return (card_relative == current['cardPath'] and stored == expected
                and json.loads(row[12]) == current['indexAck'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error):
        return False


def latest_paths(db):
    """Current worker pages and their exact raw source revisions for census."""
    rows = db.execute('SELECT card_path,card_sha256,source_snapshots FROM '
                      "topical_worker_versions WHERE state='active' ORDER BY rowid").fetchall()
    current = {}
    for card, card_sha, sources_json in rows:
        sources = json.loads(sources_json)
        for source in sources:
            revision = source['revision']
            path, sha = revision['path'], revision['sha256']
            if path in current and current[path][1] != sha:
                raise ValueError('topical_worker_source_revision_conflict')
            current[path] = ('topical_worker_source', sha)
        if card in current and current[card][1] != card_sha:
            raise ValueError('topical_worker_card_path_conflict')
        current[card] = ('topical_worker_card', card_sha)
    return current


def reconcile_pending(root, project):
    """After a QMD wake, register indexed pages and retire archived revisions."""
    story, _scope_value = _scope(root, project)
    from .shared import _db
    from .safe_source import snapshot_beneath
    with _db(root) as db:
        rows = db.execute("SELECT card_path,card_sha256,generation_id,card_id FROM "
                          "topical_worker_versions WHERE state='active'").fetchall()
        retired = 0
        for relative, sha, gid, cid in rows:
            if (story / relative).is_file():
                continue
            archive = story / 'topical-retired' / gid / (cid + '.md')
            if archive.is_symlink() or not archive.is_file() or _sha(archive.read_bytes()) != sha:
                raise ValueError('topical_worker_retirement_unproven')
            from .laya_learning import path_absent, selected as laya_selected
            bound = laya_selected(project)
            if bound is not None:
                if not path_absent(project, WIKI_COLLECTION,
                                   'topical-v2/' + gid + '/' + cid + '.md', bound=bound):
                    raise ValueError('topical_worker_retirement_unindexed')
            else:
                raise ValueError('topical_worker_index_unselected')
            db.execute("UPDATE topical_worker_versions SET state='retired' WHERE "
                       "card_path=? AND card_sha256=? AND state='active'", (relative,sha))
            retired += 1
    pages = sorted((story / '.auto-context/wiki/topical-v2').glob('*/*.md'))
    registered, deferred = [], []
    for page in pages:
        try:
            if page.is_symlink() or not page.is_file():
                raise ValueError('topical_worker_card_unsafe')
            result = register(root, project, page.parent.name, page.stem)
            registered.append(result['registration_id'])
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            deferred.append({'card': page.relative_to(story).as_posix(),
                             'reason': str(exc)[:80]})
    return {'registered':registered,'deferred':deferred,'retired':retired}
