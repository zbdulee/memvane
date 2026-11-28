"""Verify rows written by compile and read before an extraction.

A first card is published as ``generated`` at once, after its ``review`` row
is queued, so a crash never leaves a visible card without a review. A
replacement is only a ``replace`` row whose ``pending_blob`` holds the new
image; the visible card stays until the verifier publishes the reviewed one.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
from pathlib import Path
import sqlite3

from memvane.kernel.paths import now_iso as _now_iso
from memvane.settings import types as settings_types
from memvane.wiki.card import api as yaml_scalars, types as wiki_markers


def job_revisions(job):
    raw = job.get('sourceRevisions')
    if not isinstance(raw, list) or not raw:
        return []
    revisions = [yaml_scalars.normalize_source_revision(item) for item in raw]
    return revisions if all(revisions) else []


def candidate_matches_job(text, job):
    """The managed candidate names exactly the source snapshots in this job."""
    front = wiki_markers.FRONTMATTER_RE.match(text)
    marker = wiki_markers.AUTO_START_RE.search(text)
    revisions = job_revisions(job)
    meta, valid = yaml_scalars.parse_frontmatter(text)
    return bool(valid and meta.get('status') == 'generated'
                and revisions and front and marker and marker.group(1) == job.get('sourceHash')
                and yaml_scalars.auto_writable_text(text)[0]
                and yaml_scalars.frontmatter_source_revisions(front.group(1)) == revisions)


def _open(root):
    from memvane.wiki.pipeline import api as pipeline
    return pipeline.open_pipeline(root)


def enqueue_review(root: Path, job: dict, image: bytes | None = None, *,
                   generation: dict | None = None) -> bool:
    """Queue one verify row; ``generation`` is recorded in the same transaction."""
    try:
        with _open(root) as store:
            store.append_jobs('verify', [job], pending_blobs=[image], generation=generation)
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


def publish_first_card(root: Path, wiki_root: Path, job: dict, image: bytes, *, review: bool,
                       manifest: dict) -> bool:
    """Queue the review row, then expose the generated card with one no-replace rename.

    The caller holds the card and mutation locks. A row left without its card is
    closed as ``card_missing`` by the verifier; the compile job is retried. The
    ``manifest`` history row lands in ``card_generations`` with the publish.
    """
    from memvane.wiki.card.api import unresolved_pending_refreshes_strict
    from memvane.wiki.pipeline import api as pipeline
    target = root / job['targetPath']
    if unresolved_pending_refreshes_strict(root) is None:
        return False
    if review and not enqueue_review(root, job):
        return False
    try:
        wiki = Path(wiki_root).resolve()
        outcome = pipeline.publish_card(root, wiki, target.resolve().relative_to(wiki), image,
                                        expected_target_sha=None, lock_context=nullcontext,
                                        generation=manifest)
    except (OSError, ValueError, sqlite3.Error):
        return False
    return outcome.state == 'published'


def pending_source_review(root, config, revision, *, reuse=True):
    """Return (reusable replace target, a live claim citing this source).

    A queued or claimed ``replace`` row for this exact revision whose base is
    still the live card means the extraction already happened and only its
    review is outstanding. A live claim citing the source keeps the compile job.
    """
    from memvane.wiki.pipeline import api as pipeline
    wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
    claimed_source = False
    if wiki.resolve() != wiki or not wiki.is_relative_to(root):
        return None, claimed_source
    keys = ('path', 'collection', 'sha256', 'size')
    try:
        with pipeline.open_pipeline(root) as store:
            rows = store.open_verify_jobs()
            for row in rows:
                job = row['payload']
                cites = any(item['path'] == revision['path'] and item['collection'] == revision['collection']
                            for item in job_revisions(job))
                if cites and row['claimLive']:
                    claimed_source = True
            if not reuse:
                return None, claimed_source
            for row in reversed(rows):
                job = row['payload']
                if job.get('kind') != 'replace':
                    continue
                revisions = job_revisions(job)
                if not any(all(item[k] == revision[k] for k in keys) for item in revisions):
                    continue
                if len(revisions) > 1:
                    freshness = yaml_scalars.check_card(job, root, yaml_scalars.allow_roots_of(config))
                    if freshness.get('state') != wiki_markers.FRESH:
                        continue
                rel = job.get('targetPath')
                if not isinstance(rel, str) or Path(rel).is_absolute():
                    continue
                live = root / rel
                if (live.resolve() != live or not live.is_relative_to(wiki)
                        or any(part.startswith('.') for part in live.relative_to(wiki).parts)):
                    continue
                blob = store.job_blob('verify', row['jobId'])
                try:
                    if (blob is None or not candidate_matches_job(blob.decode('utf-8'), job)
                            or hashlib.sha256(live.read_bytes()).hexdigest() != job.get('expectedTargetHash')):
                        continue
                except (OSError, UnicodeError):
                    continue
                return rel, claimed_source
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        pass  # Unproved reuse costs a new extraction; it never suppresses work.
    return None, claimed_source


# -- review rows for generated cards that have none -------------------------------------------
# A card the 1.x cutover left without its review (``verify-queue.jsonl`` was not imported) has no
# other path back into review while its source is unchanged. Two entries queue the ``kind: review``
# row compile would have written, with ``expectedTargetHash`` = the live bytes, so released 1.1.4+
# verifiers process it unchanged: ``v1_reviews`` at the cutover (marker transaction), and
# ``reload_unreviewed`` once afterwards (``pipeline_meta`` ``review_reload_v1_at``).
REVIEW_RELOAD_TRIGGER = 'review_reload'


def _wiki_dir(root: Path, config: dict) -> Path:
    return (root / config.get('wikiPath', settings_types.WIKI_REL)).resolve()


def _card_at(root: Path, wiki: Path, rel) -> tuple[bytes, str] | None:
    """The bytes and text of a visible card at project-relative ``rel``, or None."""
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        return None
    live = root / rel
    try:
        if live.is_symlink() or live.resolve() != live.absolute():
            return None
        inside = live.relative_to(wiki)
        if any(part.startswith('.') for part in inside.parts):
            return None
        data = live.read_bytes()
        return data, data.decode('utf-8')
    except (OSError, ValueError):
        return None


def _machine_generated(text: str) -> tuple[list, str] | None:
    """(sourceRevisions, managed sourceHash) of a machine-owned ``generated`` card."""
    meta, valid = yaml_scalars.parse_frontmatter(text)
    front = wiki_markers.FRONTMATTER_RE.match(text)
    marker = wiki_markers.AUTO_START_RE.search(text)
    if not (valid and front and marker and meta.get('status') == 'generated'
            and meta.get('createdBy') in wiki_markers.CREATED_BY_NAMES):
        return None
    revisions = yaml_scalars.frontmatter_source_revisions(front.group(1))
    return (revisions, marker.group(1)) if revisions else None


def _fresh(root: Path, config: dict, revisions: list) -> bool:
    state = yaml_scalars.check_card({'sourceRevisions': revisions}, root, yaml_scalars.allow_roots_of(config))
    return state.get('state') == wiki_markers.FRESH


def _resolvable(config: dict):
    """``engine -> bool``: the verifier can call this producing engine (``plan_verify_attempts``)."""
    from memvane.wiki.engines import api as engines
    compile_cfg = config.get('compile') if isinstance(config.get('compile'), dict) else {}
    builtins = []
    for block in (compile_cfg.get('extractor'), compile_cfg.get('verify')):
        if isinstance(block, dict) and isinstance(block.get('builtins'), list):
            builtins += [e for e in block['builtins'] if isinstance(e, str) and e]
    return lambda engine: (isinstance(engine, str) and engine != settings_types.UNKNOWN_ENGINE
                           and engines.resolve_extractor_argv(compile_cfg, engine, builtins=builtins) is not None)


def v1_reviews(root: Path, config: dict):
    """The cutover filter (``pipeline.migrate_from_v1(reviews=...)``) for unfinished 1.x rows.

    A row comes across as one ``kind: review`` payload when it names a first card (a
    replacement's ``pendingPath`` image is discarded with the hidden wiki files), that card
    is a machine-owned ``generated`` card whose managed block and ``sourceRevisions`` are the
    row's, its bytes equal the row's ``expectedTargetHash`` when the row has one, its source
    is fresh (a stale card waits for its refresh), and the verifier can call the row's engine
    (an unresolvable engine would keep the row queued every pass). The last row of a target wins.
    """
    root = Path(root)
    wiki = _wiki_dir(root, config)
    resolvable = _resolvable(config)

    def convert(rows: list[dict]) -> list[dict]:
        payloads, seen = [], set()
        for row in reversed(rows):
            target = row.get('targetPath')
            if target in seen or 'pendingPath' in row or not resolvable(row.get('engine')):
                continue
            seen.add(target)
            card = _card_at(root, wiki, target)
            if card is None:
                continue
            data, text = card
            sha = hashlib.sha256(data).hexdigest()
            if (row.get('expectedTargetHash') not in (None, sha) or _machine_generated(text) is None
                    or not candidate_matches_job(text, row) or not _fresh(root, config, job_revisions(row))):
                continue
            payloads.append({**row, 'kind': 'review', 'expectedTargetHash': sha})
        return payloads[::-1]
    return convert


def _engines(root: Path) -> tuple[dict, set]:
    """(latest engine per target, every engine) named by this project's verify rows."""
    from memvane.wiki.pipeline import api as pipeline
    by_target, every = {}, set()
    with pipeline.open_pipeline(root, readonly=True) as store:
        for row in store.queued_jobs('verify', states=('queued', 'claimed', 'done', 'dropped')):
            engine, target = row['payload'].get('engine'), row['payload'].get('targetPath')
            if isinstance(engine, str) and engine:
                every.add(engine)
                by_target[target] = engine
    return by_target, every


def reload_payloads(root: Path, config: dict) -> tuple[list[dict], dict]:
    """One ``kind: review`` payload per machine-owned, fresh ``generated`` card.

    The producing engine is the latest one a verify row of that target names, else the
    project's only engine; with neither the payload's ``engine`` is None. The ledger
    transaction (``pipeline.reload_reviews``) drops the payloads whose bytes something already
    settles, then leaves a None-engine card alone as ``engineUnknown``: the verifier never
    reviews with another engine, and an unresolvable row would stay queued every pass.
    """
    root = Path(root)
    wiki = _wiki_dir(root, config)
    resolvable = _resolvable(config)
    by_target, every = _engines(root)
    project_engine = next(iter(every)) if len(every) == 1 else None
    payloads, counts = [], {'cards': 0, 'stale': 0}
    for path in sorted(wiki.rglob('*.md')):
        rel = path.relative_to(root).as_posix()
        card = _card_at(root, wiki, rel)
        owned = _machine_generated(card[1]) if card is not None else None
        if owned is None:
            continue
        counts['cards'] += 1
        revisions, source_hash = owned
        if not _fresh(root, config, revisions):
            counts['stale'] += 1
            continue
        engine = by_target.get(rel) if resolvable(by_target.get(rel)) else project_engine
        # An unresolvable engine is left to the ledger transaction: it counts the card
        # ``engineUnknown`` only when nothing settles its bytes already.
        engine = engine if resolvable(engine) else None
        sources = [{'kind': 'file', 'collection': r['collection'], 'path': r['path']} for r in revisions]
        payloads.append({'ts': _now_iso(), 'kind': 'review', 'targetPath': rel,
                         'expectedTargetHash': hashlib.sha256(card[0]).hexdigest(),
                         'sources': sources, 'sourceRevisions': revisions, 'authoritativeSources': sources,
                         'sourceHash': source_hash, 'engine': engine, 'trigger': REVIEW_RELOAD_TRIGGER})
    return payloads, counts


def reload_unreviewed(root: Path, config: dict) -> dict | None:
    """Queue the one-time review reload; None when it already ran or review is off.

    Rows and the marker commit in one transaction (``pipeline.reload_reviews``), so a crash
    before the commit reloads again on the next pass and a pass after it reloads nothing.
    """
    from memvane.wiki.pipeline import api as pipeline
    compile_cfg = config.get('compile') if isinstance(config.get('compile'), dict) else {}
    vcfg = compile_cfg.get('verify') if isinstance(compile_cfg.get('verify'), dict) else {}
    if not vcfg.get('enabled', True) or pipeline.review_reload_marked(root):
        return None
    payloads, counts = reload_payloads(root, config)
    return {**counts, **pipeline.reload_reviews(root, payloads)}
