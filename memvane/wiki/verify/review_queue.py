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


def enqueue_review(root: Path, job: dict, image: bytes | None = None) -> bool:
    try:
        with _open(root) as store:
            store.append_jobs('verify', [job], pending_blobs=[image])
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


def publish_first_card(root: Path, wiki_root: Path, job: dict, image: bytes, *, review: bool,
                       manifest: dict) -> bool:
    """Queue the review row, then expose the generated card with one no-replace rename.

    The caller holds the card and mutation locks. A row left without its card is
    closed as ``card_missing`` by the verifier; the compile job is retried.
    """
    from memvane.wiki.card.api import unresolved_pending_refreshes_strict
    from memvane.wiki.pipeline import api as pipeline
    target = root / job['targetPath']
    if unresolved_pending_refreshes_strict(root, before_ledger_write=True,
                                           missing_target=None if target.exists() else manifest,
                                           append_for=manifest) is None:
        return False
    if review and not enqueue_review(root, job):
        return False
    try:
        wiki = Path(wiki_root).resolve()
        outcome = pipeline.publish_card(root, wiki, target.resolve().relative_to(wiki), image,
                                        expected_target_sha=None, lock_context=nullcontext)
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
