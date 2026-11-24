"""Admission of compiler-proven wiki card content."""
from __future__ import annotations

from pathlib import Path
import re

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _verified_wiki_row(row: dict, decision: dict,
                       pending_sources: frozenset[str] = frozenset(),
                       source_locations: dict | None = None,
                       source_skip_paths=(), *, current_source: bool = True,
                       reasons: dict | None = None) -> bool:
    """Accept compiler-proven wiki bytes, optionally checking current raw data.

    An optional ``sourceSpans`` list must be bound to the card's revisions.
    The compile writer re-hashes the current raw bytes; selected recall checks
    published card provenance without reopening the raw files. A malformed
    list rejects the card; an absent list is a legacy file-level card.
    Locators describe the published revision, not current raw freshness.
    ``reasons`` (optional, caller-owned) counts why a card was rejected.
    """
    located = _verified_wiki_locations(row, decision, pending_sources,
                                       source_skip_paths, current_source=current_source,
                                       reasons=reasons)
    if located is None:
        return False
    if source_locations is not None and located:
        source_locations[row['path']] = located
    return True


def _rejected(reasons, code):
    if reasons is not None:
        reasons[code] = reasons.get(code, 0) + 1
    return None


def _revision_raw_root(project: Path, path: str, collection: str,
                       roots: dict, roles: dict) -> Path | None:
    """Raw root that holds a revision path, or None.

    A raw collection label resolves to its own root. A label that is not a
    collection of this route (a renamed or copied project) resolves by path,
    like the QMD reader, which never consults the label: the project-relative
    path must lie under a configured raw root. A wiki label never does.
    """
    role = roles.get(collection)
    if role == 'raw':
        return Path(roots[collection])
    if role is not None:
        return None
    candidate = project / path
    for name in sorted(roots):
        if roles.get(name) != 'raw':
            continue
        try:
            candidate.relative_to(roots[name])
        except ValueError:
            continue
        return Path(roots[name])
    return None


def _verified_wiki_locations(row: dict, decision: dict,
                             pending_sources: frozenset[str],
                             source_skip_paths=(), *,
                             current_source: bool = True,
                             reasons: dict | None = None) -> tuple[str, ...] | None:
    from memvane.wiki.card import wiki_card
    from memvane.wiki.card import types as wiki_markers
    from memvane.wiki.card import source_spans
    from memvane.wiki.card import wiki_freshness
    from memvane.wiki.card import yaml_scalars
    from memvane.settings.api import excluded_source
    body = row['body'].replace('\r\n', '\n').replace('\r', '\n')
    if not body.startswith('---\n'):
        return _rejected(reasons, 'frontmatter_missing')
    end = body.find('\n---\n', 4)
    if end < 0:
        return _rejected(reasons, 'frontmatter_missing')
    block = body[4:end]
    if _card_path_excluded(block):
        return _rejected(reasons, 'compile_source_path_line_separator')
    fields = wiki_card.parse_frontmatter_scalars(block)
    revisions = wiki_card.frontmatter_source_revisions(block)
    meta = {'status': fields.get('status'), 'createdBy': fields.get('createdBy'),
            'sourceRevisions': revisions}
    if not wiki_card.is_auto_trusted_card(meta):
        if meta['status'] not in wiki_card.TRUSTED_WIKI_STATUSES:
            return _rejected(reasons, 'status_not_verified')
        if meta['createdBy'] not in wiki_markers.CREATED_BY_NAMES:
            return _rejected(reasons, 'creator_foreign')
        return _rejected(reasons, 'source_revisions_missing')
    # Same body-binding rule as the QMD reader (recall.read_card_meta): an
    # absent digest is "not bound", not "mismatch". Cards verified before the
    # digest existed stay servable; a present digest must match the bytes.
    expected_body = fields.get('verifiedBodyHash')
    if expected_body not in (None, '') and (
            not isinstance(expected_body, str) or not _HEX64.fullmatch(expected_body)
            or yaml_scalars.card_body_hash(body) != expected_body):
        return _rejected(reasons, 'body_digest_mismatch')
    span_state, spans = source_spans.read_card_spans(block)
    if (span_state == source_spans.INVALID
            or not source_spans.bound_to_revisions(spans, revisions)):
        return _rejected(reasons, 'source_spans_invalid')
    project = Path(decision['projectRoot'])
    roots = decision['collectionRoots']
    roles = {item['name']: item['role'] for item in decision['collections']}
    for revision in revisions:
        normalized = yaml_scalars.normalize_source_revision(revision)
        if normalized is None:
            return _rejected(reasons, 'source_revision_invalid')
        if (excluded_source(normalized['path'], source_skip_paths)
                or (current_source and normalized['path'] in pending_sources)):
            return _rejected(reasons, 'source_skipped_or_pending')
        raw_root = _revision_raw_root(project, normalized['path'],
                                      normalized['collection'], roots, roles)
        if raw_root is None:
            return _rejected(reasons, 'source_outside_raw_roots')
        try:
            candidate = project / normalized['path']
            relative = candidate.relative_to(raw_root)
            if (not relative.parts or any(part in ('', '.', '..') for part in relative.parts)
                    or (current_source and candidate.resolve(strict=True) != candidate)):
                return _rejected(reasons, 'source_outside_raw_roots')
        except (OSError, RuntimeError, ValueError):
            return _rejected(reasons, 'source_outside_raw_roots')
        if not current_source:
            continue
        # One stable read serves both the revision check and every span on it.
        snapshot = wiki_freshness.snapshot_bytes(candidate)
        if snapshot is None:
            return _rejected(reasons, 'source_unreadable')
        current, data = snapshot
        # A branch switch can change mtime without changing source bytes.
        # The stable snapshot hash and size prove the revision;
        # a metadata-only timestamp change must not hide a verified wiki card.
        if (current['sha256'] != normalized['sha256']
                or current['size'] != normalized['size']):
            return _rejected(reasons, 'source_changed')
        if any(span['path'] == normalized['path'] and not source_spans.verify_span(span, data)
               for span in spans):
            return _rejected(reasons, 'source_span_changed')
    return tuple(source_spans.locator(span) for span in spans)


def _report_excluded(decision):
    from memvane.diagnostics import api as events
    events.emit('compile.source', outcome='skipped', reason='compile_source_path_line_separator', decision=decision)


def incompatible_source(value, *, report=False, decision=None):
    """Only decoded source paths are subject to the released JSONL readers' line boundary."""
    from memvane.settings.api import excluded_source
    if isinstance(value, dict):
        records = [value, value.get('source')]
        for key in ('sources', 'sourceRevisions', 'authoritativeSources'):
            if isinstance(value.get(key), list):
                records.extend(value[key])
        spec = value.get('sourceFingerprints')
        if isinstance(spec, dict) and isinstance(spec.get('fingerprints'), list):
            records.extend(spec['fingerprints'])
        paths = [row[key] for row in records if isinstance(row, dict) for key in ('path', 'sourcePath') if key in row]
    else:
        paths = [value]
    rejected = any(excluded_source(path, (), wiki_source=True) for path in paths)
    if rejected and report:
        _report_excluded(decision)
    return rejected


# Match complete quoted scalars between source fields too: text in another field
# can contain "path:" or braces. Physical LF is the card writer's record boundary.
_QUOTED = r""""(?:[^"\\]|\\.)*"|'(?:[^']|'')*'"""
_PATH_FIELD = re.compile(
    r"(?:^[ \t]*(?:-[ \t]+)?|[{,][ \t]*)"
    r"""(?:path|sourcePath|"path"|"sourcePath"|'path'|'sourcePath')[ \t]*:[ \t]*"""
    + r"(?P<value>" + _QUOTED + r"|[^,}\]#]*)|" + _QUOTED + r"|\#.*"
)


def _card_path_excluded(block):
    from memvane.wiki.card import yaml_scalars
    if not incompatible_source(block) and '\\' not in block:
        return False
    section = None
    for line in block.split('\n'):
        if line and line[0] not in ' \t':
            section = line.partition(':')[0]
        if section not in ('sources', 'sourceRevisions'):
            continue
        for field in _PATH_FIELD.finditer(line):
            raw = field['value']
            if raw is not None and (incompatible_source(raw) or incompatible_source(yaml_scalars.load(raw))):
                return True
    return False


def card_sources_allowed(body, skip_paths=(), *, report=False, decision=None) -> bool:
    """Check the complete source paths of a job or a serialized wiki card."""
    if isinstance(body, dict):
        return not incompatible_source(body, report=report, decision=decision)
    if not isinstance(body, str):
        return not skip_paths
    from memvane.settings.api import excluded_source
    from memvane.wiki.card import wiki_card
    from memvane.wiki.card import yaml_scalars
    normalized_body = body.replace('\r\n', '\n').replace('\r', '\n')
    end = normalized_body.find('\n---\n', 4) if normalized_body.startswith('---\n') else -1
    if end > 0 and _card_path_excluded(normalized_body[4:end]):
        if report:
            _report_excluded(decision)
        return False
    if not skip_paths:
        return True
    revisions = (wiki_card.frontmatter_source_revisions(normalized_body[4:end])
                 if end > 0 else [])
    normalized = [yaml_scalars.normalize_source_revision(item) for item in revisions]
    return bool(normalized) and all(
        item is not None and not excluded_source(item['path'], skip_paths)
        for item in normalized)
