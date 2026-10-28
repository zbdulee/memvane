"""Shared source-eligibility vocabulary for raw scans and compile enqueue.

This module is the single place that decides which raw source files count as
text documents and which directory names are never walked. The raw scan
(`laya_index_source_port`, `laya_source_catalog`), the compile enqueue gate and
the compile worker all read these values. The embed provider and worker are
not gated here: they are provider-reviewed artifacts, and the memvane-db index embeds
only ``.md`` wiki cards.

Wiki cards under ``.auto-context/wiki`` stay ``.md``-only; ``WIKI_TEXT_EXTENSIONS``
names that narrower set for wiki-only scans.
"""

from __future__ import annotations

from pathlib import PurePath, PurePosixPath

# Case-insensitive suffix match on the file name.
SOURCE_TEXT_EXTENSIONS = ('.md', '.markdown', '.mdx', '.txt', '.rst', '.adoc')
WIKI_TEXT_EXTENSIONS = ('.md',)

# Wiki-root pages that earlier plugin releases created beside the cards. The
# plugin no longer creates or reads them, but existing projects keep them, so
# they are never wiki cards: search, compile grounding, lifecycle, dedup,
# source retirement and the compile proof scan skip them. Only a generated root
# `log.md` stays out of the index itself; the others stay indexed so the
# generation digest does not depend on this list. A card with the same name
# below the root (`concepts/index.md`) is an ordinary card.
WIKI_META_PAGES = ('SCHEMA.md', 'index.md', 'log.md')


def is_wiki_meta_page(path, wiki_root=None) -> bool:
    """True only for a `WIKI_META_PAGES` file directly under the wiki root.

    `path` is relative to the wiki root, or absolute when `wiki_root` is given.
    A path outside `wiki_root` is not a meta page.
    """
    if wiki_root is not None:
        try:
            path = PurePath(path).relative_to(PurePath(wiki_root))
        except ValueError:
            return False
    parts = PurePath(path).parts
    return len(parts) == 1 and parts[0] in WIKI_META_PAGES

# Exact directory-name match at any depth. Dot-prefixed names are skipped by a
# separate rule in every walker; `.auto-context`, `.git`, `.hg` and `.svn` stay
# listed because the enqueue gate also applies this set to collection roots.
DENIED_SOURCE_SEGMENTS = frozenset({
    '.auto-context', '.git', '.hg', '.svn',
    'node_modules', 'bower_components', 'vendor', 'Pods',
    'venv', 'site-packages', '__pycache__',
    'dist', 'build', 'out', 'target', 'coverage', 'DerivedData',
    'Library', 'Temp', 'obj',  # Unity project caches
})


def is_source_text(name: str, extensions: tuple[str, ...] = SOURCE_TEXT_EXTENSIONS) -> bool:
    """True when ``name`` (a file name or path) ends with an allowed extension."""
    return isinstance(name, str) and name.lower().endswith(extensions)


def is_denied_source_path(rel_path: str) -> bool:
    """True when any path segment is a denied directory name."""
    return any(part in DENIED_SOURCE_SEGMENTS for part in PurePosixPath(rel_path).parts)
