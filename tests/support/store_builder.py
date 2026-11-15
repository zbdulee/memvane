"""S8: a format-4 store of a project, opened through the store api.

``make_store(case, project, cards=...)`` writes the cards, onboards the
project (``install_builder.build_install``), settles the sync passes and
opens the store as a reader: ``(store, handle, head)``. With ``publish=False``
the store is only created (``store.api.observe_binding``) beside the case and
holds no generation. ``ingest`` writes one more card file and ``publish``
settles it into a new generation, returning the fresh ``(handle, head)``.
The store owner stays the product's sync pass; nothing here writes store rows.
"""
from __future__ import annotations

import json

_OWNERS = {}


def _repository(path, project_root):
    from memvane.store.api import read_header
    meta = read_header(path, project_root=str(project_root), timeout=5)
    if meta is None or len(meta['worktrees']) != 1:
        raise AssertionError('store header unavailable: %s' % (path,))
    return json.loads(meta['repository_json']), json.loads(meta['worktrees'][0]['record_json'])['worktreeId']


def _open(case, path, scope_id, reader, repository=None, worktree_id=None, project_root=None):
    from memvane.store.api import open_store
    if repository is None:
        repository, worktree_id = _repository(path, project_root)
    store = open_store(path, repository=repository, scope_id=scope_id, reader=reader)
    case.addCleanup(store.close)
    handle = store.capture_view(worktree_id)
    return store, handle, store.head(handle)


def make_store(case, project, *, cards=None, vectors=None, publish=True):
    """``cards``: ``{rel: body}`` or ``{rel: (title, body)}`` verified cards; ``vectors``: provider dimension."""
    for rel, value in (cards or {}).items():
        title, body = value if isinstance(value, tuple) else (rel.rsplit('/', 1)[-1].rsplit('.', 1)[0], value)
        project.write_card(rel, title=title, body=body)
    if not publish:
        from memvane.install import api as install
        from memvane.store.api import observe_binding
        scope_id = install.plan_fresh_wiki_origin(project.root)['scopeId']
        path = case.tmp / 'store.sqlite'
        binding = observe_binding(path, scope_id=scope_id, create_root=str(project.root))
        result = _open(case, path, scope_id, reader=False, repository=binding['repository'],
                       worktree_id=binding['worktree']['worktreeId'])
        _OWNERS[id(result[0])] = (project, None)
        return result
    from tests.support.install_builder import build_install, fixture_provider
    provider = fixture_provider(case, vectors) if vectors else None
    target = build_install(case, project, provider=provider)
    target.settle()
    result = _open(case, target.store_path, target.decision['scopeId'], reader=True,
                   project_root=project.root)
    _OWNERS[id(result[0])] = (project, target)
    return result


def ingest(store, handle, rel, text):
    """Write one card file (raw text) into the store's project wiki; ``publish`` makes it a member."""
    project, _target = _OWNERS[id(store)]
    path = project.wiki / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def publish(store, handle):
    """Settle the owning install's sync passes; the fresh ``(handle, head)`` of the view."""
    _project, target = _OWNERS[id(store)]
    if target is None:
        raise AssertionError('publish needs a store made with publish=True')
    target.settle()
    _repository_value, worktree_id = _repository(target.store_path, target.project.root)
    fresh = store.capture_view(worktree_id)
    return fresh, store.head(fresh)
