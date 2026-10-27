"""Shared source-eligibility vocabulary for raw scans and compile enqueue.

This module is the single place that decides which raw source files count as
text documents and which directory names are never walked. The raw scan
(`laya_index_source_port`, `laya_source_catalog`), the compile enqueue gate and
the compile worker all read these values. The embed provider and worker are
not gated here: they are provider-reviewed artifacts, and the Laya index embeds
only ``.md`` wiki cards.

Wiki cards under ``.auto-context/wiki`` stay ``.md``-only; ``WIKI_TEXT_EXTENSIONS``
names that narrower set for wiki-only scans.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Case-insensitive suffix match on the file name.
SOURCE_TEXT_EXTENSIONS = ('.md', '.markdown', '.mdx', '.txt', '.rst', '.adoc')
WIKI_TEXT_EXTENSIONS = ('.md',)

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
