"""Isolated QMD candidate retrieval and conservative v2 duplicate gate.

QMD vector scores locate possible comparisons; they never decide semantic
equivalence. Exact copies and every unresolved pair are held for explicit
review. This module has no teacher transport and cannot merge or delete cards.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import wiki_topical_backend as backend
import wiki_compile_worker as compile_worker

import wiki_topical as topical
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
from wiki_topical_selection import exact_card_identity

# Bound new provider work per worker pass, never the total candidate set. A
# completed pair is keyed by both immutable page hashes and is reused on resume.
MAX_NEW_REVIEWS_PER_PASS = 8
# The isolated story has 53 legacy wiki pages plus the live topical families.
# QMD's approximate vsearch returned only 44 hits with -n 64, but all 59
# indexed pages with -n 256 for the same query. Saturation still fails closed.
MAX_LEGACY_RESULTS = 128
SEARCH_LIMIT = 256
REVIEW_FILE = 'topical-similarity-review.json'
SELECTION_DIR = 'topical-similarity-selection'
SELECTION_SCHEMA = 'qmd-topical-similarity-selection-v1'
LEGACY_VECTOR_HEAD = 8


def _technical_ids(value: str) -> set[str]:
    return {letter.lower() + number for letter, number in
            re.findall(r'(?i)(?<![a-z0-9])([a-z]{1,2})[-_ ]?(\d{1,3})'
                       r'(?![a-z0-9])', value)}


def _selected_legacy(census: list[dict], qmd_legacy: list[str],
                     anchor: str | None) -> list[str]:
    if anchor is None:
        return sorted(row['path'] for row in census)
    selected = set(qmd_legacy[:LEGACY_VECTOR_HEAD])
    selected.update(row['path'] for row in census
                    if anchor in _technical_ids(row['_body'].decode('utf8')))
    return sorted(selected)


def _record_anchor(card: dict) -> str | None:
    """Use a fixture record ID only when source path and card ID agree."""
    revisions = card.get('sourceRevisions')
    if not isinstance(revisions, list) or not revisions:
        return None
    paths = [row.get('path') for row in revisions if isinstance(row, dict)]
    if len(paths) != len(revisions) or any(not isinstance(p, str) or
            not re.fullmatch(r'(?:01_Settings|02_Characters|03_Plot)/'
                r'qmd-prospective/(?:tr|va)-[a-z-]+/[A-Za-z0-9._-]+\.md', p)
            for p in paths):
        return None
    shared = _technical_ids(card.get('cardId', ''))
    for path in paths:
        shared &= _technical_ids(Path(path).stem)
    return next(iter(shared)) if len(shared) == 1 else None


def _legacy_census(root: Path, *, require_index: bool = True) -> list[dict]:
    """Read every legacy page through the same no-follow reader as a review."""
    _project, wiki, collection = publisher._project(root)
    pages = sorted(p for p in wiki.rglob('*.md')
                   if 'topical-v2' not in p.relative_to(wiki).parts)
    if len(pages) > 512:
        raise topical.TopicalError('similarity_census_too_large')
    if not pages:
        return []
    runtime = publisher._qmd_runtime(root) if require_index else None
    rows = []
    db = sqlite3.connect(runtime['INDEX_PATH']) if runtime else None
    try:
        if db:
            db.execute('PRAGMA query_only=ON')
        for page in pages:
            rel = page.relative_to(wiki).as_posix()
            body = _page_bytes(root, rel)
            digest = hashlib.sha256(body).hexdigest()
            if db:
                indexed = db.execute('SELECT hash FROM documents WHERE '
                    'collection=? AND path=? AND active=1',
                    (collection, rel)).fetchone()
                if indexed is None or indexed[0] != digest:
                    raise topical.TopicalError('similarity_index_stale')
            rows.append({'path': rel, 'pageSha256': digest,
                         'lead': body.decode('utf8')[:600], 'legacy': True,
                         '_body': body})
    finally:
        if db:
            db.close()
    return rows


def _selection_path(root: Path, generation_id: str, card_id: str) -> Path:
    if (not experiment.GENERATION_ID.fullmatch(generation_id) or
            not topical.CARD_ID.fullmatch(card_id)):
        raise topical.TopicalError('invalid_similarity_selection')
    return root / SELECTION_DIR / generation_id / (card_id + '.json')


def _selection_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_size > 1_000_000):
            raise topical.TopicalError('unsafe_similarity_selection')
        return os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)


def _selection(root: Path, card: dict, generation_id: str,
               retrieved: list[dict] | None,
               plan_bytes: bytes | None = None) -> list[dict]:
    """Freeze a small legacy set while binding every omitted page's hash."""
    path = _selection_path(root, generation_id, card['cardId'])
    census = _legacy_census(root, require_index=retrieved is not None)
    by_path = {row['path']: row for row in census}
    census_hashes = [{'path': row['path'], 'sha256': row['pageSha256']}
                     for row in census]
    staged_sha = hashlib.sha256(topical.read_generation_bytes(
        root, generation_id, f"cards/{card['cardId']}.md")).hexdigest()
    anchor = _record_anchor(card)
    if path.exists() or path.is_symlink():
        plan = json.loads(plan_bytes if plan_bytes is not None
                          else _selection_bytes(path))
        # Publication freezes the candidate snapshot for crash recovery.
        # Additional legacy pages may arrive later, while every original page
        # and hash must remain available. Before publication, any census drift
        # stops the batch for a new review.
        _project, wiki, _collection = publisher._project(root)
        publication_started = any((wiki / 'topical-v2' /
            generation_id).glob('*.md'))
        frozen = plan.get('census') if isinstance(plan, dict) else None
        frozen_paths = ({row.get('path') for row in frozen}
                        if isinstance(frozen, list) and all(
                            isinstance(row, dict) for row in frozen) else set())
        frozen_census = [by_path[name] for name in sorted(frozen_paths)
                         if name in by_path]
        frozen_hashes = [{'path': row['path'], 'sha256': row['pageSha256']}
                         for row in frozen_census]
        if (not isinstance(plan, dict) or set(plan) != {
                'schema', 'generationId', 'cardId', 'stagedSha256', 'anchor',
                'census', 'qmdLegacyHits', 'selected'}
                or plan['schema'] != SELECTION_SCHEMA
                or plan['generationId'] != generation_id
                or plan['cardId'] != card['cardId']
                or plan['stagedSha256'] != staged_sha
                or plan['anchor'] != anchor
                or (plan['census'] != census_hashes and not (
                    publication_started and len(frozen_census) == len(frozen_paths)
                    and plan['census'] == frozen_hashes))
                or not isinstance(plan['qmdLegacyHits'], list)
                or len(plan['qmdLegacyHits']) != len(set(plan['qmdLegacyHits']))
                or any(name not in by_path for name in plan['qmdLegacyHits'])
                or not isinstance(plan['selected'], list)
                or plan['selected'] != _selected_legacy(
                    frozen_census, plan['qmdLegacyHits'], anchor)):
            raise topical.TopicalError('similarity_selection_changed')
        return [by_path[name] for name in plan['selected']]
    if retrieved is None:
        raise topical.TopicalError('similarity_selection_missing')
    qmd_legacy = [row['path'] for row in retrieved
                  if row.get('legacy') is True]
    if len(qmd_legacy) != len(set(qmd_legacy)) or any(
            name not in by_path for name in qmd_legacy):
        raise topical.TopicalError('similarity_selection_changed')
    selected = _selected_legacy(census, qmd_legacy, anchor)
    plan = {'schema': SELECTION_SCHEMA, 'generationId': generation_id,
            'cardId': card['cardId'], 'stagedSha256': staged_sha,
            'anchor': anchor, 'census': census_hashes,
            'qmdLegacyHits': qmd_legacy, 'selected': selected}
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink():
            raise topical.TopicalError('unsafe_similarity_selection')
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise topical.TopicalError('unsafe_similarity_selection')
    data = topical.encoded(plan) + b'\n'
    fd, temporary = tempfile.mkstemp(prefix='.selection-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return [by_path[name] for name in plan['selected']]


def selection_sha(root: Path, generation_id: str, card_id: str) -> str | None:
    path = _selection_path(root, generation_id, card_id)
    if not path.exists() and not path.is_symlink():
        return None
    data = _selection_bytes(path)
    card = experiment.load_staged_card(root, generation_id, card_id)
    _selection(root, card, generation_id, None, plan_bytes=data)
    return hashlib.sha256(data).hexdigest()


def review_candidates(root: Path, card: dict, generation_id: str,
                      exclude=()) -> list[dict]:
    """Share the exact frozen legacy set and full fresh topical census."""
    root, wiki, _collection = publisher._project(root)
    plan = _selection_path(root, generation_id, card['cardId'])
    retrieved = None if plan.is_file() else retrieve(
        root, card['lead'], exclude=exclude)
    legacy = _selection(root, card, generation_id, retrieved)
    topical_hits = [] if retrieved is None else [
        row for row in retrieved if row.get('legacy') is not True]
    found = {row['path']: row for row in topical_hits}
    for row in publisher.fresh_published_candidates(root, wiki,
                                                     exclude=exclude):
        prior = found.get(row['path'])
        if prior is not None and prior['pageSha256'] != row['pageSha256']:
            raise topical.TopicalError('similarity_candidate_changed')
        found[row['path']] = row
    return legacy + list(found.values())


def _review(root: Path) -> dict:
    path = root / REVIEW_FILE
    value = {'schema': 'qmd-topical-similarity-review-v1', 'decisions': []}
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise topical.TopicalError('unsafe_similarity_review')
            value = json.loads(os.read(fd, info.st_size + 1))
        finally:
            os.close(fd)
    if (not isinstance(value, dict) or value.get('schema') != 'qmd-topical-similarity-review-v1'
            or not isinstance(value.get('decisions'), list)):
        raise topical.TopicalError('invalid_similarity_review')
    decisions = {}
    for row in value['decisions']:
        if not isinstance(row, dict):
            raise topical.TopicalError('invalid_similarity_review')
        complementary = row.get('verdict') == 'complementary_overlap'
        required = {'pairSha256', 'verdict', 'reviewerId', 'reason'}
        if complementary:
            required |= {'sharedClaims', 'uniqueClaims'}
        if (set(row) != required
                or not isinstance(row['pairSha256'], str) or len(row['pairSha256']) != 64
                or row['verdict'] not in ('distinct', 'duplicate', 'conflict', 'unclear',
                                          'complementary_overlap')
                or not isinstance(row['reviewerId'], str) or not row['reviewerId'].strip()
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or row['pairSha256'] in decisions):
            raise topical.TopicalError('invalid_similarity_review')
        if complementary:
            for field in ('sharedClaims', 'uniqueClaims'):
                groups = row[field]
                if (not isinstance(groups, dict) or len(groups) != 2
                        or any(not isinstance(cid, str) or not topical.CARD_ID.fullmatch(cid)
                               or not isinstance(ids, list) or not ids or len(ids) > 12
                               or any(not isinstance(claim, str) or not claim.strip()
                                      for claim in ids) or len(ids) != len(set(ids))
                               for cid, ids in groups.items())):
                    raise topical.TopicalError('invalid_similarity_review')
            if (set(row['sharedClaims']) != set(row['uniqueClaims'])
                    or any(set(row['sharedClaims'][cid]) & set(row['uniqueClaims'][cid])
                           for cid in row['sharedClaims'])):
                raise topical.TopicalError('invalid_similarity_review')
        decisions[row['pairSha256']] = row
    for pair, row in _automatic_decisions(root).items():
        if pair in decisions and decisions[pair]['verdict'] != row['verdict']:
            raise topical.TopicalError('similarity_review_conflict')
        decisions.setdefault(pair, row)
    return decisions


def _page_bytes(root: Path, relative: str, *, staged: bool = False) -> bytes:
    if (not isinstance(relative, str) or not relative or
            Path(relative).is_absolute()):
        raise topical.TopicalError('invalid_similarity_page')
    parts = Path(relative).parts
    if (len(parts) == 3 and parts[0] == 'topical-v2'
            and experiment.GENERATION_ID.fullmatch(parts[1])
            and parts[2].endswith('.md')
            and topical.CARD_ID.fullmatch(parts[2][:-3])):
        if staged:
            body = topical.read_generation_bytes(root, parts[1], f'cards/{parts[2]}')
            if len(body) > 65536:
                raise topical.TopicalError('similarity_page_too_large')
            return body
    elif (staged or not parts or parts[0] == 'topical-v2'
          or Path(relative).as_posix() != relative or
          any(part in ('.', '..') or not topical.CARD_ID.fullmatch(
              part[:-3] if index == len(parts) - 1 and part.endswith('.md')
              else part) for index, part in enumerate(parts)) or
          not parts[-1].endswith('.md')):
        raise topical.TopicalError('invalid_similarity_page')
    _project, wiki, _collection = publisher._project(root)
    # Walk from the exact project root, including .auto-context and wiki.
    # Opening only wiki by its absolute name could follow a swapped ancestor
    # into another novel just before model egress.
    try:
        wiki_parts = wiki.relative_to(root).parts
    except ValueError as exc:
        raise topical.TopicalError('similarity_candidate_missing') from exc
    opened = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.append(directory)
        for part in (*wiki_parts, *parts[:-1]):
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            opened.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > 65536):
                raise topical.TopicalError('similarity_candidate_missing')
            body = os.read(fd, info.st_size + 1)
            if len(body) != info.st_size:
                raise topical.TopicalError('similarity_candidate_changed')
        finally:
            os.close(fd)
    except OSError as exc:
        raise topical.TopicalError('similarity_candidate_missing') from exc
    finally:
        for directory in reversed(opened):
            os.close(directory)
    if len(parts) == 3 and parts[0] == 'topical-v2':
        return publisher.canonical_published_card(root, parts[1],
                                                   parts[2][:-3], body)
    return body


def _dedup_payload(root: Path, generation_id: str, card_id: str,
                   candidate: dict) -> dict:
    proposed_path = f'topical-v2/{generation_id}/{card_id}.md'
    other_path = candidate['path']
    proposed = _page_bytes(root, proposed_path, staged=True)
    # A sibling in the same unpublished batch is read from its immutable stage.
    sibling = other_path.startswith(f'topical-v2/{generation_id}/')
    other = _page_bytes(root, other_path, staged=sibling)
    a_sha, b_sha = (hashlib.sha256(body).hexdigest() for body in (proposed, other))
    if b_sha != candidate['pageSha256']:
        raise topical.TopicalError('similarity_candidate_changed')
    pair = topical.digest(topical.encoded(['qmd-topical-pair-v1',
                                           *sorted((a_sha, b_sha))]))
    return {'task': 'dedup', 'pairSha256': pair,
            'pageA': {'path': proposed_path, 'content': proposed.decode('utf8')},
            'pageB': {'path': other_path, 'content': other.decode('utf8')}}


def _semantic_decision(response, pair):
    if not isinstance(response, dict) or response.get('verdict') not in (
            'distinct', 'duplicate', 'unclear'):
        return None
    reason = response.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        return None
    for field in ('sharedFacts', 'uniqueToA', 'uniqueToB'):
        rows = response.get(field)
        if (not isinstance(rows, list) or len(rows) > 10 or
                any(not isinstance(value, str) or not value.strip() or len(value) > 300
                    for value in rows)):
            return None
    if response['verdict'] == 'distinct' and not (
            response['uniqueToA'] and response['uniqueToB']):
        return None
    return {'pairSha256': pair, 'verdict': response['verdict'],
            'reviewerId': 'codex-cli-semantic-dedup', 'reason': reason}


def _automatic_decisions(root: Path) -> dict:
    """Rebuild decisions from exact, completed, full-content adapter receipts."""
    import topical_story_scope
    if not topical_story_scope.exact_story(root):
        return {}
    topical_story_scope.read(root)
    cfg = json.loads((root / topical_story_scope.AUTO_POLICY).read_text())['compileCfg']
    argv = compile_worker.resolve_extractor_argv(cfg, 'codex')
    if argv is None:
        raise topical.TopicalError('similarity_transport_unavailable')
    transport = backend.transport_fingerprint(root, argv)
    folder = root / 'topical-backend-audit'
    if not folder.is_dir():
        return {}
    fd = backend._audit_fd(root)
    decisions = {}
    try:
        for name in sorted(os.listdir(fd)):
            if not name.startswith('similarity.') or not name.endswith('.attempt.json'):
                continue
            row = backend._read_audit(fd, name)
            if row.get('state') != 'completed':
                continue
            payload = row.get('requestContext')
            if (row.get('schema') != backend.ATTEMPT_SCHEMA or
                    row.get('kind') != 'similarity' or
                    not isinstance(payload, dict) or
                    row.get('requestSha256') != topical.digest(topical.encoded(payload)) or
                    row.get('transportSha256') != transport or
                    name != f"similarity.{row['requestSha256']}.{row.get('attemptId')}.attempt.json"):
                raise topical.TopicalError('similarity_receipt_changed')
            page_a, page_b = payload.get('pageA'), payload.get('pageB')
            if (payload.get('task') != 'dedup' or not isinstance(page_a, dict)
                    or not isinstance(page_b, dict) or
                    set(page_a) != {'path', 'content'} or
                    set(page_b) != {'path', 'content'} or
                    any(not isinstance(page.get(key), str)
                        for page in (page_a, page_b)
                        for key in ('path', 'content'))):
                raise topical.TopicalError('similarity_receipt_changed')
            parts = Path(page_a['path']).parts
            if (len(parts) != 3 or parts[0] != 'topical-v2' or
                    not experiment.GENERATION_ID.fullmatch(parts[1]) or
                    not parts[2].endswith('.md')):
                raise topical.TopicalError('similarity_receipt_changed')
            candidate = {'path': page_b['path'],
                         'pageSha256': hashlib.sha256(page_b['content'].encode('utf8')).hexdigest()}
            try:
                current = _dedup_payload(root, parts[1], parts[2][:-3], candidate)
            except (OSError, UnicodeError, topical.TopicalError):
                # A changed or retired page invalidates this decision for new
                # publication; the old receipt remains intact for audit.
                continue
            if current != payload or current['pairSha256'] != payload.get('pairSha256'):
                continue
            decision = _semantic_decision(row.get('adapterResponse'), payload['pairSha256'])
            if decision is None:
                continue
            prior = decisions.get(decision['pairSha256'])
            if prior is not None and prior['verdict'] != decision['verdict']:
                raise topical.TopicalError('similarity_receipt_conflict')
            decisions.setdefault(decision['pairSha256'], decision)
    finally:
        os.close(fd)
    return decisions


def auto_review_pending(root: Path, proposed: dict, generation_id: str,
                        matches: list[dict], compile_cfg: dict, engine: str,
                        runner, *, recovery_only: bool) -> dict:
    """Judge unresolved exact-story pairs once each; retain every call receipt."""
    import topical_story_scope
    if not topical_story_scope.exact_story(root):
        return evaluate(root, proposed, generation_id, matches)
    topical_story_scope.read(root)
    if engine != 'codex':
        raise topical.TopicalError('similarity_transport_unreviewed')
    argv = compile_worker.resolve_extractor_argv(compile_cfg, engine)
    if argv is None:
        raise topical.TopicalError('similarity_transport_unavailable')
    new_reviews = 0
    for item in matches:
        payload = _dedup_payload(root, generation_id, proposed['cardId'], item)
        if payload['pairSha256'] in _review(root):
            continue
        if new_reviews >= MAX_NEW_REVIEWS_PER_PASS:
            return {'status': 'pending_review', 'reason': 'similarity_review_deferred',
                    'reviewedThisPass': new_reviews}
        if recovery_only or runner is None:
            return {'status': 'pending_review', 'reason': 'similarity_backend_required'}
        from wiki_topical_refresh import _recorded
        attempt_id = None
        for name in ('initial', 'retry1'):
            response, pending, audit = _recorded(root, 'similarity', payload, name)
            if pending and pending != 'similarity_attempt_failed':
                return {'status': 'pending_review', 'reason': pending}
            if response is not None:
                if (audit.get('transportSha256') !=
                        backend.transport_fingerprint(root, argv)):
                    raise topical.TopicalError('similarity_transport_changed')
                if _semantic_decision(response, payload['pairSha256']) is not None:
                    break
            if pending == 'similarity_attempt_failed':
                continue
            if audit is None:
                attempt_id = name
                break
        if attempt_id is None:
            return {'status': 'pending_review', 'reason': 'similarity_response_invalid'}
        # Count the dispatch, including a failed or invalid response, so a
        # retry cannot silently exceed the bounded work budget.
        new_reviews += 1
        try:
            response, _replayed = backend._attempt(root, 'similarity', payload,
                argv, 120, runner, attempt_id, 2, 10, 20)
        except topical.TopicalError as exc:
            return {'status': 'pending_review', 'reason': exc.code}
        if _semantic_decision(response, payload['pairSha256']) is None:
            return {'status': 'pending_review', 'reason': 'similarity_response_invalid'}
    return evaluate(root, proposed, generation_id, matches)


def _complementary_pair(decision: dict, proposed: dict, generation_id: str,
                        item: dict) -> bool:
    """Keep both unchanged cards only after a claim-level review of one sibling pair."""
    if item.get('legacy') is True or not isinstance(item.get('card'), dict):
        return False
    other = item['card']
    ids = {proposed['cardId'], other.get('cardId')}
    if (len(ids) != 2 or item.get('path') !=
            f"topical-v2/{generation_id}/{other.get('cardId')}.md"
            or proposed.get('sourceRevisions') != other.get('sourceRevisions')
            or set(decision['sharedClaims']) != ids
            or set(decision['uniqueClaims']) != ids):
        return False
    for card in (proposed, other):
        cid = card['cardId']
        claim_ids = [claim['claimId'] for claim in card['claims']]
        if (len(claim_ids) != len(set(claim_ids))
                or (set(decision['sharedClaims'][cid]) |
                    set(decision['uniqueClaims'][cid])) != set(claim_ids)):
            return False
    return True


def retrieve(root: Path, text: str, *,
             exclude: tuple[str, str] | list[tuple[str, str]] | None = None) -> list[dict]:
    """Use this project's vector index, then revalidate each returned page."""
    root, wiki, collection = publisher._project(root)
    runtime = publisher._qmd_runtime(root)
    query = ' '.join(text.split())[:1000]
    excluded = ({exclude} if isinstance(exclude, tuple) else set(exclude or []))
    if len(query) < 3:
        return []
    result = publisher._call(root, runtime, ['vsearch', query, '-c', collection,
                                            '-n', str(SEARCH_LIMIT), '--format', 'json'])
    try:
        hits = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise topical.TopicalError('similarity_search_unavailable') from exc
    if not isinstance(hits, list) or len(hits) >= SEARCH_LIMIT:
        raise topical.TopicalError('similarity_search_unavailable')
    db = sqlite3.connect(runtime['INDEX_PATH'])
    matches = []
    legacy_count = 0
    try:
        db.execute('PRAGMA query_only=ON')
        for hit in hits:
            if not isinstance(hit, dict) or not isinstance(hit.get('file'), str):
                raise topical.TopicalError('invalid_similarity_hit')
            prefix = 'qmd://' + collection + '/'
            if not hit['file'].startswith(prefix):
                raise topical.TopicalError('invalid_similarity_hit')
            rel = hit['file'][len(prefix):]
            parts = Path(rel).parts
            is_topical = (len(parts) == 3 and parts[0] == 'topical-v2'
                          and experiment.GENERATION_ID.fullmatch(parts[1])
                          and parts[2].endswith('.md')
                          and topical.CARD_ID.fullmatch(parts[2][:-3]))
            is_legacy = (parts and parts[0] != 'topical-v2'
                         and rel == Path(rel).as_posix()
                         and all(topical.CARD_ID.fullmatch(part) for part in parts[:-1])
                         and parts[-1].endswith('.md')
                         and topical.CARD_ID.fullmatch(parts[-1][:-3]))
            if not is_topical and not is_legacy:
                raise topical.TopicalError('similarity_unreviewed_wiki_hit')
            if is_topical:
                gid, card_id = parts[1], parts[2][:-3]
                if (gid, card_id) in excluded:
                    continue
            page = wiki / rel
            if (any(parent.is_symlink() for parent in
                    [page, *list(page.parents)[:len(parts)]])
                    or not page.is_file()):
                raise topical.TopicalError('similarity_candidate_missing')
            content = page.read_bytes()
            row = db.execute('SELECT hash FROM documents WHERE collection=? AND path=? AND active=1',
                             (collection, rel)).fetchone()
            if row is None or row[0] != hashlib.sha256(content).hexdigest():
                raise topical.TopicalError('similarity_index_stale')
            if is_legacy:
                legacy_count += 1
                if legacy_count > MAX_LEGACY_RESULTS:
                    raise topical.TopicalError('similarity_review_budget_exceeded')
                try:
                    lead = content.decode('utf8')[:600]
                except UnicodeDecodeError as exc:
                    raise topical.TopicalError('similarity_unreviewed_wiki_hit') from exc
                matches.append({'path': rel, 'pageSha256': hashlib.sha256(content).hexdigest(),
                                'lead': lead, 'legacy': True})
                continue
            sidecar = json.loads(topical.read_generation_bytes(root, gid,
                f'cards/{card_id}.evidence.json'))
            if any(not (root / revision['path']).is_file() or
                hashlib.sha256((root / revision['path']).read_bytes()).hexdigest() != revision['sha256']
                for revision in sidecar['sourceRevisions']):
                # A stale indexed page remains a recall-filtered retirement
                # candidate, not a current semantic comparison.
                continue
            publisher._attested(root, gid, card_id)
            card = experiment.load_staged_card(root, gid, card_id)
            staged_sha = hashlib.sha256(topical.read_generation_bytes(root, gid,
                f'cards/{card_id}.md')).hexdigest()
            matches.append({'generationId': gid, 'cardId': card_id, 'path': rel,
                            'pageSha256': staged_sha, 'card': card,
                            'lead': card['lead'][:600]})
    finally:
        db.close()
    return matches


def generation_context(root: Path, matches: list[dict]) -> list[dict]:
    """Bound model context; retain the full search for later similarity review.

    The exact story's teacher may see only validated topical-v2 pages. Its
    legacy wiki remains in the independent publication comparison, but is not
    part of the scoped model input. This also handles older in-flight journals
    that saved the full search before this selection was introduced.
    """
    import topical_story_scope
    scoped = topical_story_scope.exact_story(root)
    selected = []
    for row in matches:
        if not isinstance(row, dict):
            raise topical.TopicalError('invalid_existing_wiki_context')
        path, lead = row.get('path'), row.get('lead')
        if not isinstance(path, str) or not isinstance(lead, str):
            raise topical.TopicalError('invalid_existing_wiki_context')
        parts = path.split('/')
        is_topical = (len(parts) == 3 and parts[0] == 'topical-v2'
                      and experiment.GENERATION_ID.fullmatch(parts[1])
                      and parts[2].endswith('.md')
                      and topical.CARD_ID.fullmatch(parts[2][:-3]))
        if scoped and not is_topical:
            continue
        selected.append({'path': path, 'lead': lead})
        if len(selected) == 8:
            break
    return selected


def evaluate(root: Path, proposed: dict, generation_id: str,
             matches: list[dict]) -> dict:
    """Pass only when every candidate is independently marked distinct."""
    root = experiment.require_sandbox(root)
    proposed_page = topical.read_generation_bytes(root, generation_id,
                                                   f"cards/{proposed['cardId']}.md")
    proposed_sha = hashlib.sha256(proposed_page).hexdigest()
    decisions = _review(root)
    rows = []
    for item in matches:
        pair = topical.digest(topical.encoded(['qmd-topical-pair-v1',
                                               *sorted((proposed_sha, item['pageSha256']))]))
        legacy = item.get('legacy') is True
        exact = not legacy and exact_card_identity(proposed) == exact_card_identity(item['card'])
        proposed_scopes = {(c['statement'].strip().casefold(), c['state'],
                            c['timeScope'], c['condition']) for c in proposed['claims']}
        existing_scopes = set() if legacy else {
            (c['statement'].strip().casefold(), c['state'], c['timeScope'], c['condition'])
            for c in item['card']['claims']}
        shared_statements = {row[0] for row in proposed_scopes} & {
            row[0] for row in existing_scopes}
        scoped_difference = bool(shared_statements and proposed_scopes != existing_scopes)
        decision = decisions.get(pair)
        verdict = 'exact_duplicate' if exact else decision['verdict'] if decision else 'unresolved'
        if verdict == 'complementary_overlap' and not _complementary_pair(
                decision, proposed, generation_id, item):
            verdict = 'unresolved'
        rows.append({'pairSha256': pair, 'candidatePath': item['path'],
                     'candidateSha256': item['pageSha256'],
                     'verdict': verdict,
                     'relation': 'legacy_wiki_review_required' if legacy else
                                 'exact_fact_and_provenance' if exact else
                                 'state_time_or_condition_difference' if scoped_difference else
                                 'semantic_review_required',
                     'scope': [{'state': c['state'], 'timeScope': c['timeScope'],
                                'condition': c['condition']} for c in
                               ([] if legacy else item['card']['claims'])]})
    pending = [row for row in rows if row['verdict'] not in
               ('distinct', 'complementary_overlap')]
    reason = ('similarity_duplicate' if any(row['verdict'] in
              ('duplicate', 'exact_duplicate') for row in pending) else
              'similarity_conflict' if any(row['verdict'] == 'conflict'
              for row in pending) else
              'similarity_unclear' if any(row['verdict'] == 'unclear' for row in pending)
              else 'similarity_unresolved') if pending else None
    return {'status': 'pending_review' if pending else 'distinct_or_no_candidates',
            'proposedSha256': proposed_sha, 'pairs': rows, 'pending': len(pending),
            **({'reason': reason} if reason else {})}
