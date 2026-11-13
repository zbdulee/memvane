"""Stable project directory names."""

CURRENT = '.memvane'
NAMES = (CURRENT,)
SETTINGS = 'settings.json'
WIKI = 'wiki'

SOURCE_TEXT_EXTENSIONS = ('.md', '.markdown', '.mdx', '.txt', '.rst', '.adoc')

WIKI_TEXT_EXTENSIONS = ('.md',)

WIKI_META_PAGES = ('SCHEMA.md', 'index.md', 'log.md')

DENIED_SOURCE_SEGMENTS = frozenset({
    '.memvane', '.auto-context', '.git', '.hg', '.svn',
    'node_modules', 'bower_components', 'vendor', 'Pods',
    'venv', 'site-packages', '__pycache__',
    'dist', 'build', 'out', 'target', 'coverage', 'DerivedData',
    'Library', 'Temp', 'obj',  # Unity project caches
})
