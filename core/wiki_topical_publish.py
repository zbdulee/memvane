#!/usr/bin/env python3
"""Publish a backend-pass topical card into an opted-in isolated QMD project.

The marked project keeps its own QMD DB/config/cache. Existing project settings,
other wiki files and any global hook stay untouched. Only an immutable,
source-fresh backend attestation can authorize the publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

import config as qmd_config
import wiki_compile
import wiki_mutation_lock
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import yaml_scalars
import qmd_route


def _same_published_card(existing: bytes, freshly_stamped: bytes) -> bool:
    """Allow a repeat publish only when the verification clock is the sole change."""
    clock = re.compile(rb"(?m)^verifiedAt: [^\r\n]+$")
    if len(clock.findall(existing)) != 1 or len(clock.findall(freshly_stamped)) != 1:
        return False
    return clock.sub(b"verifiedAt: <publication-time>", existing) == clock.sub(
        b"verifiedAt: <publication-time>", freshly_stamped)


def canonical_published_card(root: Path, generation_id: str, card_id: str,
                             published: bytes) -> bytes:
    """Bind a stamped public page to its attested, immutable model input."""
    record = _attested(root, generation_id, card_id)
    staged = topical.read_generation_bytes(root, generation_id,
                                           f'cards/{card_id}.md')
    if hashlib.sha256(staged).hexdigest() != record['cardMarkdownSha256']:
        raise topical.TopicalError('staged_body_changed')
    body_hash = yaml_scalars.card_body_hash(staged.decode('utf8'))
    if body_hash is None:
        raise topical.TopicalError('staged_body_changed')
    fd, temporary = tempfile.mkstemp(prefix='.similarity-', dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(staged)
        if not wiki_compile.stamp_verification(Path(temporary), 'verified',
                record['engine'], qmd_config.VERIFIED_MODE_UNKNOWN, body_hash):
            raise topical.TopicalError('verification_stamp_failed')
        if not _same_published_card(published, Path(temporary).read_bytes()):
            raise topical.TopicalError('published_card_changed')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return staged


def _project(root: Path):
    root = experiment.require_sandbox(root)
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        scope = topical_story_scope.read(root)
        return root, scope['wiki'], scope['collection']
    found = qmd_config.find_project_config(str(root))
    if found["configFormat"] != "auto-context-dir" or Path(found["projectRoot"]).resolve() != root:
        raise topical.TopicalError("project_optin_required")
    cfg = found["config"]
    if cfg.get("indexing") is not True or cfg.get("recallStrategy") not in ("wikiOnly", "hierarchical"):
        raise topical.TopicalError("wiki_recall_not_enabled")
    matches = [name for name, path in cfg.get("collectionPaths", {}).items()
               if path == ".auto-context/wiki" and cfg.get("collectionRoles", {}).get(name) == "wiki"
               and name in cfg.get("collections", [])]
    if len(matches) != 1:
        raise topical.TopicalError("isolated_wiki_collection_required")
    wiki = root / ".auto-context/wiki"
    for part in (root / ".auto-context", wiki):
        if part.is_symlink() or not part.is_dir():
            raise topical.TopicalError("unsafe_wiki_directory")
    return root, wiki, matches[0]


def _attested(root: Path, generation_id: str, card_id: str):
    request = backend.verification_payload(root, generation_id, card_id)
    record = _read_backend_record(root, generation_id, card_id)
    response = {key: record.get(key) for key in ("verdict", "checks", "reasons")}
    fresh = backend.validate_verification_response(root, request, response,
                                                   mode="backend", engine=record.get("engine", ""))
    if fresh != record:
        raise topical.TopicalError("attestation_input_changed")
    return record


def fresh_published_candidates(root: Path, wiki: Path, *, exclude=()):
    """Return every fresh, attested topical page with canonical pair hashes."""
    excluded = set(exclude)
    matches = []
    for page in sorted((wiki / 'topical-v2').glob('*/*.md')):
        gid, cid = page.parent.name, page.stem
        if (gid, cid) in excluded:
            continue
        if page.is_symlink() or not page.is_file():
            raise topical.TopicalError('unsafe_published_card')
        sidecar = json.loads(topical.read_generation_bytes(root, gid,
            f'cards/{cid}.evidence.json'))
        # An old source-stale page is being retired by refresh. It is never a
        # publishable current candidate; all fresh pages still need review.
        stale = any(not (root / revision['path']).is_file() or
            hashlib.sha256((root / revision['path']).read_bytes()).hexdigest() != revision['sha256']
            for revision in sidecar['sourceRevisions'])
        if stale:
            continue
        _attested(root, gid, cid)
        canonical_published_card(root, gid, cid, page.read_bytes())
        card = experiment.load_staged_card(root, gid, cid)
        matches.append({'path':page.relative_to(wiki).as_posix(),
            'pageSha256':hashlib.sha256(topical.read_generation_bytes(root, gid,
                f'cards/{cid}.md')).hexdigest(), 'card':card,
            'lead': card['lead'][:600]})
    return matches


def _publication_similarity(root: Path, wiki: Path, generation_id: str, card_id: str):
    """Every public publish entrypoint reviews all other fresh v2 pages."""
    import wiki_topical_similarity as similarity
    proposed = experiment.load_staged_card(root, generation_id, card_id)
    matches = fresh_published_candidates(root, wiki,
        exclude=[(generation_id, card_id)])
    result = similarity.evaluate(root, proposed, generation_id, matches)
    if result['status'] == 'pending_review':
        raise topical.TopicalError('similarity_unresolved')
    return result


def _create_review_proof(root, generation_id, card_id, generated_sha, record):
    """Bind create-time legacy and staged sibling reviews outside the old proof."""
    path = root / 'topical-create-review-proofs' / generation_id / (card_id + '.json')
    import topical_story_scope
    required = topical_story_scope.exact_story(root)
    if path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise topical.TopicalError('unsafe_create_review_proof')
    if not path.exists() and not path.is_symlink():
        if required:
            raise topical.TopicalError('create_review_proof_required')
        return None
    if path.is_symlink() or not path.is_file():
        raise topical.TopicalError('unsafe_create_review_proof')
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o077 or info.st_size > 1_000_000):
        raise topical.TopicalError('unsafe_create_review_proof')
    proof = json.loads(path.read_text())
    import wiki_topical_similarity as similarity
    selection_sha = similarity.selection_sha(root, generation_id, card_id)
    already_published = (
        (root / '.auto-context/wiki/topical-v2' / generation_id /
         (card_id + '.md')).is_file() and
        (root / 'topical-publication-proofs' / generation_id /
         (card_id + '.json')).is_file())
    if (required and not already_published and selection_sha is None and
            (root / 'topical-generations' /
            generation_id / 'manifest.json').is_file() and
            similarity._record_anchor(experiment.load_staged_card(
                root, generation_id, card_id)) is not None):
        raise topical.TopicalError('similarity_selection_missing')
    if (proof.get('schema') != 'qmd-topical-create-review-proof-v1'
            or proof.get('generationId') != generation_id
            or proof.get('cardId') != card_id
            or proof.get('generatedSha256') != generated_sha
            or proof.get('attestationSha256') != topical.digest(topical.encoded(record))
            or not isinstance(proof.get('pairs'), list)
            or len(proof['pairs']) > 512
            or (selection_sha is not None and
                proof.get('legacySelectionSha256') != selection_sha)
            or (selection_sha is None and
                'legacySelectionSha256' in proof)):
        raise topical.TopicalError('create_review_proof_changed')
    if selection_sha is not None:
        staged = experiment.load_staged_card(root, generation_id, card_id)
        selected = similarity._selection(root, staged, generation_id, None)
        reviewed_legacy = {(row.get('candidatePath'), row.get('candidateSha256'))
                           for row in proof['pairs'] if isinstance(row, dict)}
        if any((row['path'], row['pageSha256']) not in reviewed_legacy
               for row in selected):
            raise topical.TopicalError('create_review_proof_changed')
    decisions = similarity._review(root, required_pairs={
        row.get('pairSha256') for row in proof['pairs'] if isinstance(row, dict)})
    seen = set()
    for row in proof['pairs']:
        complementary = isinstance(row, dict) and row.get('verdict') == 'complementary_overlap'
        fields = {'pairSha256', 'candidatePath', 'candidateSha256',
                  'relation', 'verdict'}
        if complementary:
            fields.add('reviewDecisionSha256')
        decision = decisions.get(row.get('pairSha256')) if isinstance(row, dict) else None
        if (not isinstance(row, dict)
                or set(row) != fields
                or row['verdict'] not in ('distinct', 'complementary_overlap')
                or not isinstance(row['candidatePath'], str)
                or not isinstance(row['candidateSha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['candidateSha256'])
                or not isinstance(row['relation'], str)
                or row['pairSha256'] != topical.digest(topical.encoded([
                    'qmd-topical-pair-v1',
                    *sorted((generated_sha, row['candidateSha256']))]))
                or row['pairSha256'] in seen
                or not isinstance(decision, dict)
                or decision.get('verdict') != row['verdict']
                or (complementary and row['reviewDecisionSha256'] !=
                    topical.digest(topical.encoded(decision)))):
            raise topical.TopicalError('create_review_proof_changed')
        seen.add(row['pairSha256'])
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_create_review_proof(root, generation_id, card_id, pairs):
    """Create an immutable additive proof, including a pre-rc18 published card."""
    root = experiment.require_sandbox(root)
    record = _attested(root, generation_id, card_id)
    generated_sha = hashlib.sha256(topical.read_generation_bytes(
        root, generation_id, f'cards/{card_id}.md')).hexdigest()
    import wiki_topical_similarity as similarity
    decisions = similarity._review(root)
    rows = []
    for row in pairs:
        item = {key: row[key] for key in
                ('pairSha256', 'candidatePath', 'candidateSha256', 'relation', 'verdict')}
        if item['verdict'] == 'complementary_overlap':
            decision = decisions.get(item['pairSha256'])
            if not isinstance(decision, dict) or decision.get('verdict') != item['verdict']:
                raise topical.TopicalError('create_review_unresolved')
            item['reviewDecisionSha256'] = topical.digest(topical.encoded(decision))
        rows.append(item)
    if any(row['verdict'] not in ('distinct', 'complementary_overlap')
           for row in rows):
        raise topical.TopicalError('create_review_unresolved')
    selection_sha = similarity.selection_sha(root, generation_id, card_id)
    if selection_sha is not None:
        staged = experiment.load_staged_card(root, generation_id, card_id)
        selected = similarity._selection(root, staged, generation_id, None)
        reviewed_legacy = {(row['candidatePath'], row['candidateSha256'])
                           for row in rows}
        if any((row['path'], row['pageSha256']) not in reviewed_legacy
               for row in selected):
            raise topical.TopicalError('create_review_unresolved')
    proof = {'schema': 'qmd-topical-create-review-proof-v1',
             'generationId': generation_id, 'cardId': card_id,
             'generatedSha256': generated_sha,
             'attestationSha256': topical.digest(topical.encoded(record)),
             'pairs': rows}
    if selection_sha is not None:
        proof['legacySelectionSha256'] = selection_sha
    base = root / 'topical-create-review-proofs'
    for folder in (base, base / generation_id):
        if folder.is_symlink():
            raise topical.TopicalError('unsafe_create_review_proof')
        folder.mkdir(mode=0o700, exist_ok=True)
        info = folder.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise topical.TopicalError('unsafe_create_review_proof')
    path = base / generation_id / (card_id + '.json')
    content = topical.encoded(proof) + b'\n'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != content:
            raise topical.TopicalError('create_review_proof_changed')
    else:
        fd, temporary = tempfile.mkstemp(prefix='.review-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(content); out.flush(); os.fsync(out.fileno())
            os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return _create_review_proof(root, generation_id, card_id, generated_sha, record)


def _publication_proof(root, generation_id, card_id, generated_sha, record):
    path=root/'topical-publication-proofs'/generation_id/(card_id+'.json')
    if path.is_symlink() or not path.is_file():
        return None
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode & 0o077 or info.st_size>1_000_000:
        raise topical.TopicalError('unsafe_publication_proof')
    proof=json.loads(path.read_text())
    if (proof.get('schema')!='qmd-topical-publication-proof-v1' or
            proof.get('generatedSha256')!=generated_sha or
            proof.get('attestationSha256')!=topical.digest(topical.encoded(record)) or
            not isinstance(proof.get('reviewPairs'),list)):
        raise topical.TopicalError('publication_proof_changed')
    import wiki_topical_similarity as similarity
    decisions=similarity._review(root, required_pairs={
        pair for pair in proof['reviewPairs'] if isinstance(pair, str)})
    bound = proof.get('reviewDecisionSha256')
    if bound is not None and (not isinstance(bound, dict)
                              or set(bound) != set(proof['reviewPairs'])):
        raise topical.TopicalError('publication_proof_changed')
    if any(not isinstance(pair,str) or decisions.get(pair,{}).get('verdict')
           not in ('distinct', 'complementary_overlap')
           or (bound is None and decisions[pair]['verdict'] != 'distinct')
           or (bound is not None and
               bound.get(pair) != topical.digest(topical.encoded(decisions[pair])))
           for pair in proof['reviewPairs']):
        raise topical.TopicalError('publication_review_revoked')
    create_sha = _create_review_proof(root, generation_id, card_id,
                                      generated_sha, record)
    if create_sha is not None:
        proof = {**proof, 'createReviewSha256': create_sha}
    return proof


def _save_publication_proof(root, generation_id, card_id, generated_sha, record, review):
    base=root/'topical-publication-proofs'
    for folder in (base,base/generation_id):
        if folder.is_symlink():raise topical.TopicalError('unsafe_publication_proof')
        folder.mkdir(mode=0o700,exist_ok=True)
        info=folder.stat()
        if info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise topical.TopicalError('unsafe_publication_proof')
    path=base/generation_id/(card_id+'.json')
    import wiki_topical_similarity as similarity
    decisions = similarity._review(root)
    bound = {}
    for row in review['pairs']:
        decision = decisions.get(row['pairSha256'])
        if not isinstance(decision, dict) or decision.get('verdict') != row['verdict']:
            raise topical.TopicalError('publication_review_revoked')
        bound[row['pairSha256']] = topical.digest(topical.encoded(decision))
    proof={'schema':'qmd-topical-publication-proof-v1',
           'generatedSha256':generated_sha,
           'attestationSha256':topical.digest(topical.encoded(record)),
           'reviewPairs':[row['pairSha256'] for row in review['pairs']]}
    proof['reviewDecisionSha256'] = bound
    content=topical.encoded(proof)+b'\n'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes()!=content:
            raise topical.TopicalError('publication_proof_changed')
        return
    fd,temporary=tempfile.mkstemp(prefix='.proof-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            os.fchmod(out.fileno(),0o600)
            out.write(content);out.flush();os.fsync(out.fileno())
        os.link(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_backend_record(root: Path, generation_id: str, card_id: str):
    if not isinstance(generation_id, str) or not experiment.GENERATION_ID.fullmatch(generation_id):
        raise topical.TopicalError("invalid_generation_id")
    if not isinstance(card_id, str) or not topical.CARD_ID.fullmatch(card_id):
        raise topical.TopicalError("invalid_card_id")
    path = root / "topical-attestations/backend" / generation_id / (card_id + ".json")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise topical.TopicalError("unsafe_attestation_path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 2_000_000):
            raise topical.TopicalError("unsafe_attestation_path")
        record = json.loads(os.read(fd, info.st_size + 1))
    finally:
        os.close(fd)
    if (not isinstance(record, dict) or record.get("status") != "backend_pass"
            or record.get("mode") != "backend" or record.get("verdict") != "pass"
            or record.get("generationId") != generation_id or record.get("cardId") != card_id):
        raise topical.TopicalError("backend_pass_required")
    return record


def publish(root: Path, generation_id: str, card_id: str) -> dict:
    root=experiment.require_sandbox(root)
    with wiki_mutation_lock.lock(root):
        return _publish_locked(root,generation_id,card_id)


def _publish_locked(root: Path, generation_id: str, card_id: str) -> dict:
    root, wiki, collection = _project(root)
    record = _attested(root, generation_id, card_id)
    generated = topical.read_generation_bytes(root, generation_id, f"cards/{card_id}.md").decode("utf8")
    generated_sha=hashlib.sha256(generated.encode()).hexdigest()
    destination = wiki / 'topical-v2' / generation_id / (card_id + '.md')
    _create_review_proof(root, generation_id, card_id, generated_sha, record)
    proof=_publication_proof(root,generation_id,card_id,generated_sha,record) if destination.is_file() else None
    review=None if proof is not None else _publication_similarity(root, wiki, generation_id, card_id)
    body_hash = yaml_scalars.card_body_hash(generated)
    if body_hash is None or hashlib.sha256(generated.encode()).hexdigest() != record["cardMarkdownSha256"]:
        raise topical.TopicalError("staged_body_changed")
    folder = wiki / "topical-v2" / generation_id
    current = wiki
    for part in ("topical-v2", generation_id):
        current = current / part
        if current.is_symlink():
            raise topical.TopicalError("unsafe_publish_directory")
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise topical.TopicalError("unsafe_publish_directory")
    destination = folder / (card_id + ".md")
    fd, stage = tempfile.mkstemp(prefix=".verified-", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as output:
            output.write(generated)
            output.flush(); os.fsync(output.fileno())
        if not wiki_compile.stamp_verification(Path(stage), "verified", record["engine"],
                                               qmd_config.VERIFIED_MODE_UNKNOWN, body_hash):
            raise topical.TopicalError("verification_stamp_failed")
        published = Path(stage).read_bytes()
        try:
            os.link(stage, destination)
        except FileExistsError:
            try:
                existing_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(existing_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                        raise topical.TopicalError("published_card_conflict")
                    existing = os.read(existing_fd, info.st_size + 1)
                finally:
                    os.close(existing_fd)
            except OSError as exc:
                raise topical.TopicalError("published_card_conflict") from exc
            if not _same_published_card(existing, published):
                raise topical.TopicalError("published_card_conflict")
            published = existing
        if proof is None:
            _save_publication_proof(root,generation_id,card_id,generated_sha,record,review)
        return {"status": "published_verified", "collection": collection,
                "cardId": card_id, "generationId": generation_id,
                "path": str(destination), "sha256": hashlib.sha256(published).hexdigest()}
    finally:
        Path(stage).unlink(missing_ok=True)


def retire_stale(root: Path, generation_id: str, card_id: str,
                 source_roots: list[str], cards: list[dict]) -> dict:
    root = experiment.require_sandbox(root)
    with wiki_mutation_lock.lock(root):
        return _retire_stale_locked(root, generation_id, card_id, source_roots, cards)


def _retire_stale_locked(root: Path, generation_id: str, card_id: str,
                         source_roots: list[str], cards: list[dict]) -> dict:
    """Reversibly remove one source-stale card from an isolated QMD collection.

    The real source reverse projection, not a tool hint or mock completion,
    authorizes retirement. The caller publishes backend-verified replacements
    and syncs QMD only after this operation succeeds.
    """
    root, wiki, _collection = _project(root)
    import wiki_topical_reconcile as reconcile
    roots = sorted(set(reconcile.safe_rel(path, explicit_root=True) for path in source_roots))
    validated = reconcile._cards(cards, roots)
    projection = reconcile._projection(validated, reconcile._scan(root, roots, ()), set())
    if projection.get(card_id, {}).get('state') != 'excluded_stale':
        raise topical.TopicalError('source_stale_projection_required')
    if not isinstance(generation_id, str) or not experiment.GENERATION_ID.fullmatch(generation_id):
        raise topical.TopicalError('invalid_generation_id')
    if not isinstance(card_id, str) or not topical.CARD_ID.fullmatch(card_id):
        raise topical.TopicalError('invalid_card_id')
    published = wiki / 'topical-v2' / generation_id / (card_id + '.md')
    archive = root / 'topical-retired'
    archive.mkdir(mode=0o700, exist_ok=True)
    folder = archive / generation_id
    folder.mkdir(mode=0o700, exist_ok=True)
    for path in (archive, folder):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise topical.TopicalError('unsafe_retirement_directory')
    target = folder / (card_id + '.md')
    if published.is_symlink() or target.is_symlink():
        raise topical.TopicalError('retirement_conflict')
    linked_before_unlink = published.exists() and target.exists()
    if linked_before_unlink:
        source_stat, archive_stat = published.stat(), target.stat()
        if (not stat.S_ISREG(source_stat.st_mode)
                or not stat.S_ISREG(archive_stat.st_mode)
                or (source_stat.st_dev, source_stat.st_ino) !=
                   (archive_stat.st_dev, archive_stat.st_ino)):
            raise topical.TopicalError('retirement_conflict')
    proof_page = published if published.is_file() else target
    if not proof_page.is_file():
        raise topical.TopicalError('published_card_missing')
    # Staleness prevents the ordinary current-source attestation check. Bind
    # the caller's old card to the immutable generation, pass record, and
    # exact published page before allowing a retirement by card ID.
    manifest = json.loads(topical.read_generation_bytes(root, generation_id, 'manifest.json'))
    generated = topical.read_generation_bytes(root, generation_id, f'cards/{card_id}.md')
    sidecar = topical.read_generation_bytes(root, generation_id, f'cards/{card_id}.evidence.json')
    entry = next((row for row in manifest.get('cards', [])
                  if isinstance(row, dict) and row.get('cardId') == card_id), None)
    record = _read_backend_record(root, generation_id, card_id)
    if (entry is None or json.loads(sidecar) != next(
            (card for card in cards if isinstance(card, dict) and card.get('cardId') == card_id), None)
            or hashlib.sha256(generated).hexdigest() != entry.get('markdownSha256')
            or hashlib.sha256(sidecar).hexdigest() != entry.get('evidenceSha256')
            or record.get('cardMarkdownSha256') != entry.get('markdownSha256')
            or record.get('evidenceSha256') != entry.get('evidenceSha256')
            or record.get('sourceRevisionsSha256') != topical.digest(topical.encoded(
                json.loads(sidecar)['sourceRevisions']))):
        raise topical.TopicalError('stale_card_identity_mismatch')
    fd, stage = tempfile.mkstemp(prefix='.retirement-proof-', dir=published.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(generated)
        body_hash = yaml_scalars.card_body_hash(generated.decode('utf8'))
        if body_hash is None:
            raise topical.TopicalError('stale_card_identity_mismatch')
        if not wiki_compile.stamp_verification(Path(stage), 'verified', record['engine'],
                                               qmd_config.VERIFIED_MODE_UNKNOWN, body_hash):
            raise topical.TopicalError('stale_card_identity_mismatch')
        if not _same_published_card(proof_page.read_bytes(), Path(stage).read_bytes()):
            raise topical.TopicalError('stale_card_identity_mismatch')
    finally:
        Path(stage).unlink(missing_ok=True)
    # The archive is deliberately outside every wiki collection. Rename is
    # reversible and never overwrites an existing file or the staged proof.
    if proof_page == target:
        return {'status': 'already_retired_stale', 'cardId': card_id,
                'generationId': generation_id, 'archivePath': str(target)}
    if linked_before_unlink:
        source_stat, archive_stat = published.stat(), target.stat()
        if ((source_stat.st_dev, source_stat.st_ino) !=
                (archive_stat.st_dev, archive_stat.st_ino)):
            raise topical.TopicalError('retirement_conflict')
        published.unlink()
        return {'status': 'retired_stale', 'cardId': card_id,
                'generationId': generation_id, 'archivePath': str(target)}
    try:
        os.link(published, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise topical.TopicalError('retirement_conflict') from exc
    published.unlink()
    return {'status': 'retired_stale', 'cardId': card_id,
            'generationId': generation_id, 'archivePath': str(target)}


def _qmd_runtime(root: Path):
    try:
        import topical_story_scope
        if topical_story_scope.exact_story(root):
            scope = topical_story_scope.read(root)
            paths = qmd_route.project_paths(scope['project'], isolated=True,
                reject_conflicts=True)
            if paths.get('selected') is not True:
                raise ValueError('shared_qmd_store_unselected')
        else:
            paths = qmd_route.project_paths(root, isolated=True, reject_conflicts=True,
                fixture_env=os.environ.get('QMD_TOPICAL_SYNTHETIC_RUNTIME') == '1')
        binary = qmd_route.binary_info()['QMD_BIN']
        if os.environ.get('QMD_BIN'):
            from qmd_runtime import probe_cli
            probe_cli(binary)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        reason = str(exc)
        if reason not in ('project_index_env_conflict', 'qmd_runtime_outside_project',
                          'isolated_qmd_runtime_required'):
            reason = 'qmd_runtime_incompatible'
        raise topical.TopicalError(reason) from exc
    return {'QMD_BIN': Path(binary), **{key: Path(paths[key]) for key in qmd_route.PATH_KEYS}}


def _call(root: Path, runtime: dict, args: list[str]):
    result = subprocess.run([str(runtime["QMD_BIN"]), *args], cwd=root,
                            capture_output=True, text=True, timeout=900,
                            env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                 'INDEX_PATH':str(runtime['INDEX_PATH']),
                                 'QMD_CONFIG_DIR':str(runtime['QMD_CONFIG_DIR']),
                                 'XDG_CACHE_HOME':str(runtime['XDG_CACHE_HOME'])})
    if result.returncode:
        raise topical.TopicalError("isolated_qmd_command_failed")
    return result


def _reload_shared_daemon():
    # The shared QMD daemon may hold the old SQLite view. The same writer
    # lock remains held until reload finishes, as in index_worker.sh.
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        return
    manager = Path(__file__).resolve().parent / 'backend_manager.sh'
    result = subprocess.run(['bash', str(manager), 'reload'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise topical.TopicalError('qmd_daemon_reload_failed')


def sync(root: Path, *, allow_empty: bool = False, allow_stale: bool = False,
         reclaim_retired: bool = False) -> dict:
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        topical_story_scope.read(root)
        import qmd_writer_lock
        with qmd_writer_lock.acquire():
            return _sync_locked(root, allow_empty=allow_empty,
                allow_stale=allow_stale, reclaim_retired=reclaim_retired)
    return _sync_locked(root, allow_empty=allow_empty,
        allow_stale=allow_stale, reclaim_retired=reclaim_retired)


def _sync_locked(root: Path, *, allow_empty: bool = False, allow_stale: bool = False,
                 reclaim_retired: bool = False) -> dict:
    root, wiki, collection = _project(root)
    runtime = _qmd_runtime(root)
    cards = list((wiki / "topical-v2").glob("*/*.md")) if (wiki / "topical-v2").is_dir() else []
    if not cards and not allow_empty:
        raise topical.TopicalError("no_verified_topical_cards")
    fresh_cards = []
    stale_cards = []
    for page in cards:
        if page.is_symlink() or not page.is_file():
            raise topical.TopicalError("unsafe_published_card")
        if allow_stale:
            sidecar = json.loads(topical.read_generation_bytes(root, page.parent.name,
                f'cards/{page.stem}.evidence.json'))
            if any(not (root / revision['path']).is_file() or
                hashlib.sha256((root / revision['path']).read_bytes()).hexdigest() != revision['sha256']
                for revision in sidecar['sourceRevisions']):
                stale_cards.append(page)
                continue
        # Recheck the source-bound backend proof before indexing any existing
        # card. A staged page that has gone stale may remain for audit, but it
        # must not be refreshed into the searchable QMD collection.
        if publish(root, page.parent.name, page.stem)["path"] != str(page):
            raise topical.TopicalError("published_card_conflict")
        fresh_cards.append(page)
    config_file = runtime["QMD_CONFIG_DIR"] / "index.yml"
    if not config_file.is_file() or config_file.is_symlink():
        raise topical.TopicalError("isolated_qmd_config_required")
    config = config_file.read_text()
    if collection in config:
        _call(root, runtime, ["update"])
    else:
        _call(root, runtime, ["collection", "add", str(wiki), "--name", collection, "--mask", "**/*.md"])
    if fresh_cards:
        _call(root, runtime, ["embed", "-c", collection, "--max-docs-per-batch", "1", "--max-batch-mb", "1"])
    if reclaim_retired:
        # QMD update deactivates retired documents but retains their vectors.
        # Its own cleanup removes inactive rows and unreferenced embeddings on
        # this selected project DB. Recheck fresh cards after cleanup below.
        _call(root, runtime, ["cleanup"])
    db = sqlite3.connect(runtime["INDEX_PATH"])
    try:
        db.execute("PRAGMA query_only=ON")
        if not cards and db.execute(
            "SELECT 1 FROM documents WHERE collection=? AND path GLOB 'topical-v2/*' "
            "AND active=1 LIMIT 1",
                (collection,)).fetchone():
            raise topical.TopicalError("qmd_retired_card_still_active")
        for page in fresh_cards:
            rel = page.relative_to(wiki).as_posix()
            expected = hashlib.sha256(page.read_bytes()).hexdigest()
            row = db.execute("SELECT hash FROM documents WHERE collection=? AND path=? AND active=1",
                             (collection, rel)).fetchone()
            if row is None or row[0] != expected or not db.execute(
                    "SELECT 1 FROM content_vectors WHERE hash=?", (expected,)).fetchone():
                raise topical.TopicalError("qmd_verified_card_not_ready")
    finally:
        db.close()
    registration = None
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        _reload_shared_daemon()
        from context_learning.topical_worker_provenance import reconcile_pending
        from context_learning.synthetic_scope import NOVEL_ROOT
        from context_learning.synthetic_experiment import shared_for
        registration = reconcile_pending(shared_for(NOVEL_ROOT), NOVEL_ROOT)
    return {"status": "ready", "collection": collection, "verifiedCards": len(fresh_cards),
            "pendingStaleCards": len(stale_cards),
            "lexicalReady": True, "vectorReady": True,
            **({'workerProvenance': registration} if registration is not None else {})}


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("publish", "sync"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--generation-id")
    parser.add_argument("--card-id")
    args = parser.parse_args(argv)
    if args.action == "publish" and (not args.generation_id or not args.card_id):
        parser.error("publish requires --generation-id and --card-id")
    try:
        if args.action == "publish":
            published = publish(Path(args.root), args.generation_id, args.card_id)
            result = {**published, **sync(Path(args.root))}
        else:
            result = sync(Path(args.root))
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        result = {"status": "failed", "reason": exc.code if isinstance(exc, topical.TopicalError) else "publish_unavailable"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
