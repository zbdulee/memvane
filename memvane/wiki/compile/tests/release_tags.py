"""Released bundle trees and a selected-project fixture for tests that run them.

``archived_tag`` extracts ``memvane/`` and ``package.json`` of a release tag
once per directory, so a test runs the released writer's own code.
"""
from __future__ import annotations

from pathlib import Path
import subprocess


def archived_tag(repo, directory, tag):
    bundle = Path(directory) / tag
    if not bundle.exists():
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(repo), 'archive', tag, 'memvane', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
    return bundle


def prepare_selected_card(case, name):
    """A selected project with one verified card at ``concepts/decision.md`` (sets attributes on ``case``)."""
    from memvane.wiki.compile import wiki_compile as writer
    from tests.support.project import make_project
    from tests.support.settings_fake import expanded
    case.project = make_project(case.home, name=name, dirname=name, git=False)
    case.root = case.project.root.resolve()
    case.source = case.project.write_note('docs/source.md', 'Original source.')
    case.config = expanded(case.project, compile={'semanticDedup': {'enabled': False}})
    case.queue = case.root / '.memvane/compile/verify-queue.jsonl'
    case.target = case.project.wiki / 'concepts/decision.md'
    case.target.parent.mkdir(parents=True)
    revision = case.project.revision(case.source)
    source = {k: revision[k] for k in ('kind', 'path', 'collection')}
    body = writer.markdown_page({'title': 'Decision', 'sources': [source],
                                 'sourceRevisions': [revision]}, 'Original card.', 'verified', [], 'a' * 16)
    case.target.write_text(body)
    project = case.project
    case.bound = ({'projectRoot': str(case.root), 'installId': '1' * 32, 'scopeId': '2' * 32,
                   'storeIncarnation': '3' * 32, 'recallSettings': {'skipPaths': []},
                   'collections': [{'name': project.raw_name, 'role': 'raw', 'sourceRoot': str(case.root)},
                                   {'name': project.wiki_name, 'role': 'wiki', 'sourceRoot': str(project.wiki)}],
                   'collectionRoots': {project.raw_name: str(case.root), project.wiki_name: str(project.wiki)}}, {})
