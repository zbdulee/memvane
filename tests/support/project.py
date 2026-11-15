"""S3: a ``.memvane`` project with minimal settings, notes and cards.

``make_project(home, name=...)`` writes ``<home>/<dirname>/.memvane/settings.json``
as ``{"name": name, "skipPaths": [...]}`` (the only settings a user writes)
and, with ``git=True``, an initialized repository with fixture-local identity
and no hooks. ``write_card`` fills ``sourceRevisions`` from the current note
bytes and, for a verified card, ``verifiedBodyHash`` through the card api.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

GIT_TIMEOUT = 30


class Project:
    def __init__(self, root, name, skip_paths):
        from memvane.settings import api as settings
        self.root = Path(root)
        self.name = name
        self.skip_paths = list(skip_paths)
        folder = self.root / '.memvane'
        self.settings_path = folder / 'settings.json'
        self.wiki = folder / 'wiki'
        # Minimal settings name the raw and wiki collections; read them back the product's way.
        config = settings.expand_minimal_settings({'name': name, 'skipPaths': self.skip_paths}, folder)
        roles = settings.role_map(config)
        self.raw_name = next(key for key, role in roles.items() if role == 'raw')
        self.wiki_name = next(key for key, role in roles.items() if role == 'wiki')

    # -- files -------------------------------------------------------------------
    def write_settings(self, **overrides):
        """Rewrite the minimal settings (``name``/``skipPaths`` overrides); returns the bytes."""
        value = {'name': overrides.get('name', self.name),
                 'skipPaths': list(overrides.get('skip_paths', self.skip_paths))}
        raw = (json.dumps(value) + '\n').encode('utf-8')
        self.settings_path.write_bytes(raw)
        return raw

    def write_note(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def revision(self, path):
        """The ``sourceRevisions`` entry of one raw file's current bytes."""
        from memvane.wiki.card import api as card
        snapshot = card.snapshot_bytes(Path(path))
        if snapshot is None:
            raise FileNotFoundError(str(path))
        return {'kind': 'file', 'path': Path(path).relative_to(self.root).as_posix(),
                'collection': self.raw_name, **snapshot[0]}

    def card_text(self, *, title, body, sources=(), status='verified', created_by='memvane'):
        from memvane.wiki.card import api as card
        lines = ''.join('  - ' + line + '\n'
                        for line in card.dump_source_revisions([self.revision(p) for p in sources]))
        text = (f'---\ntitle: "{title}"\nstatus: {status}\ncreatedBy: {created_by}\n'
                + ('sourceRevisions:\n' + lines if lines else '')
                + f'---\n# {title}\n{body}')
        if status == 'verified':
            marker = f'createdBy: {created_by}\n'
            text = text.replace(marker, marker + 'verifiedBodyHash: ' + card.card_body_hash(text) + '\n', 1)
        return text

    def write_card(self, rel, *, title, body, sources=(), status='verified', created_by='memvane'):
        path = self.wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.card_text(title=title, body=body, sources=sources, status=status,
                                       created_by=created_by), encoding='utf-8')
        return path

    # -- git ---------------------------------------------------------------------
    def git(self, *args, check=True):
        base = ['git', '-C', str(self.root), '-c', 'init.defaultBranch=main',
                '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
               'GIT_TERMINAL_PROMPT': '0'}
        return subprocess.run(base + list(args), check=check, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, env=env).stdout

    def commit(self, message='fixture', *, everything=True):
        if everything:
            self.git('add', '-A')
        self.git('commit', '--allow-empty', '-qm', message)
        settle_index(self.root)

    def linked_worktree(self, name, branch):
        """A linked worktree of this repository at ``<root>-<name>`` on a new ``branch``."""
        target = self.root.with_name(self.root.name + '-' + name)
        self.git('worktree', 'add', '-q', '-b', branch, str(target))
        settle_index(target)
        linked = Project.__new__(Project)
        linked.__dict__.update(self.__dict__)
        linked.root = target
        linked.settings_path = target / '.memvane' / 'settings.json'
        linked.wiki = target / '.memvane' / 'wiki'
        return linked


def settle_index(root):
    """Move the Git index two seconds into the past so no later fixture write is racily clean.

    Git trusts an index entry's stat data only when the file is older than the
    index itself; setting the index back replaces a 1 s sleep after each commit.
    """
    git_dir = Path(root) / '.git'
    if git_dir.is_file():  # a linked worktree: ``gitdir: <path>``
        git_dir = Path(git_dir.read_text(encoding='utf-8').split(':', 1)[1].strip())
    index = git_dir / 'index'
    if index.exists():
        info = index.stat()
        os.utime(index, ns=(info.st_atime_ns - 2_000_000_000, info.st_mtime_ns - 2_000_000_000))


def make_project(home, *, name='fixture', git=True, skip_paths=(), dirname='project'):
    root = Path(home) / dirname
    root.mkdir(mode=0o700)
    (root / '.memvane' / 'wiki').mkdir(parents=True)
    project = Project(root, name, skip_paths)
    project.write_settings()
    if git:
        project.git('init', '-q')
    return project
