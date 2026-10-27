"""Selected Laya source-folder exclusions; legacy QMD recall is unchanged.

The legacy skipPaths strings are substrings.  Apply them only to the parent
directory of a raw source here, so ``build`` excludes ``docs/build/a.md`` but
does not accidentally exclude ``docs/build-notes.md``.  This deliberately does
not interpret glob syntax or inspect a wiki card's own output path.
"""

from pathlib import PurePosixPath


def excluded_source(path: str, skip_paths) -> bool:
    if not isinstance(path, str) or not isinstance(skip_paths, (list, tuple)):
        return False
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return False
    directory = '/'.join(parts[:-1]) + '/'
    return any(isinstance(skip, str) and skip and skip in directory
               for skip in skip_paths)


def card_sources_allowed(body: str, skip_paths) -> bool:
    """Fail closed for a selected wiki card's complete compiler provenance."""
    if not skip_paths:
        return True
    if not isinstance(body, str):
        return False
    import wiki_card
    import yaml_scalars
    normalized_body = body.replace('\r\n', '\n').replace('\r', '\n')
    end = normalized_body.find('\n---\n', 4) if normalized_body.startswith('---\n') else -1
    revisions = (wiki_card.frontmatter_source_revisions(normalized_body[4:end])
                 if end > 0 else [])
    normalized = [yaml_scalars.normalize_source_revision(item) for item in revisions]
    return bool(normalized) and all(
        item is not None and not excluded_source(item['path'], skip_paths)
        for item in normalized)
