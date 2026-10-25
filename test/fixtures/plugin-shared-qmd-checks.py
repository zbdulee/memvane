"""No real QMD, model, daemon, or user database is contacted."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / 'core'))
import config
import qmd_route
import qmd_scope
import qmd_shared_store
import resolve_paths


def denied(call, reason):
    try:
        call()
    except ValueError as error:
        assert str(error) == reason, (error, reason)
    else:
        raise AssertionError('unexpectedly allowed')


with tempfile.TemporaryDirectory(prefix='qmd-plugin-shared-', dir='/tmp') as temp:
    home = Path(temp).resolve()
    os.environ['HOME'] = str(home)
    os.environ.pop('QMD_SETUP_GUARD_FIXTURE', None)
    # /tmp is intentionally risky in production; this fixture's owner-only
    # temporary home stands in for a normal /Users/<name> project.
    resolve_paths.is_risky_path = lambda _: False
    user_db = home / '.cache/qmd/index.sqlite'
    user_db.parent.mkdir(parents=True)
    user_db.write_bytes(b'preserved-user-database')
    novel = home / 'novel'
    novel.mkdir()
    stories = [novel / 'story-a', novel / 'story-b']
    for story in stories:
        (story / 'wiki').mkdir(parents=True)
        (story / 'wiki/card.md').write_text('synthetic card\n')
    settings = novel / '.auto-context/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({'indexing': False,
                                    'ownedScopes': ['story-a', 'story-b']}))
    for i, story in enumerate(stories):
        directory = story / '.auto-context'
        directory.mkdir()
        (directory / 'settings.json').write_text(json.dumps({
            'indexing': True, 'collections': [f'novel-story-{i}-wiki'],
            'collectionPaths': {f'novel-story-{i}-wiki': 'wiki'},
            'collectionRoles': {f'novel-story-{i}-wiki': 'wiki'}}))
        found = config.find_project_config(str(story))
        assert found['projectRoot'] == str(story), found
        assert found['projectOwner'] == str(novel)
    denied(lambda: qmd_route.project_paths(stories[0]),
           'plugin_qmd_store_not_ready')
    assert user_db.read_bytes() == b'preserved-user-database'

    base = qmd_shared_store.root(home)
    gen = base / 'generations' / ('g-' + 'a' * 32)
    conf = gen / 'config'
    cache = gen / 'cache'
    conf.mkdir(parents=True, mode=0o700)
    cache.mkdir(mode=0o700)
    for directory in (base, base / 'generations', gen, conf, cache):
        directory.chmod(0o700)
    index = gen / 'index.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        db.execute('CREATE TABLE content(hash TEXT)')
        db.execute('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT)')
        db.execute('CREATE TABLE store_collections(name TEXT,path TEXT,pattern TEXT)')
    index.chmod(0o600)
    cfg = conf / 'index.yml'
    cfg.write_text('collections: {}\nmodels: {}\n')
    cfg.chmod(0o600)
    import runtime_update
    inspected = runtime_update.inspect_index(index, include_file_hash=False)
    proof = gen / 'prepared.json'
    proof.write_text(json.dumps({'schema': qmd_shared_store.SCHEMA,
        'generation': gen.name, 'configSha256': hashlib.sha256(cfg.read_bytes()).hexdigest(),
        'indexSchemaSha256': inspected['schemaSha256']}))
    proof.chmod(0o600)
    assert qmd_shared_store.activate_empty(gen, home=home)['INDEX_PATH'] == str(index)
    # The old selected generation has no scope policy. These two stories
    # represent registrations made before scoped enrollment was introduced.
    for story in stories:
        existing = config.find_project_config(str(story))['config']
        qmd_shared_store._reserve_owners(base,
            qmd_shared_store._collection_owners(story, existing))
    for story in stories:
        assert qmd_route.project_paths(story, isolated=True)['INDEX_PATH'] == str(index)
        found = config.find_project_config(str(story))
        qmd_shared_store.register_config(story, found['config'], home=home)
        assert qmd_shared_store.owned_collections(story, found['config'], home=home) == set(found['config']['collections'])
    def added_config(entries, tail='models: {}\n'):
        return ('collections:\n' + ''.join(
            f'  {name}:\n    path: {path}\n    pattern: "**/*.md"\n'
            for name, path in entries) + tail)
    entry_a = ('novel-story-0-wiki', stories[0] / 'wiki')
    entry_b = ('novel-story-1-wiki', stories[1] / 'wiki')
    cfg.write_text(added_config([entry_a]))
    denied(lambda: qmd_shared_store.select(home), 'plugin_qmd_store_proof_invalid')
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO store_collections VALUES (?,?,?)',
                   (entry_a[0], str(entry_a[1]), '**/*.md'))
    assert qmd_shared_store.select(home)['INDEX_PATH'] == str(index)
    cfg.write_text(added_config([entry_a, entry_b]))
    denied(lambda: qmd_shared_store.select(home), 'plugin_qmd_store_proof_invalid')
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO store_collections VALUES (?,?,?)',
                   (entry_b[0], str(entry_b[1]), '**/*.md'))
    assert qmd_route.project_paths(stories[1], isolated=True)['INDEX_PATH'] == str(index)
    for changed in (
            added_config([entry_a], 'models: {changed: true}\n'),
            added_config([entry_b]),
            added_config([entry_a]),
            added_config([entry_a, ('novel-story-1-wiki', stories[0] / 'wiki')]),
            added_config([entry_a, ('unknown-wiki', stories[1] / 'wiki')]),
            added_config([entry_a, entry_a])):
        cfg.write_text(changed)
        denied(lambda: qmd_shared_store.select(home), 'plugin_qmd_store_proof_invalid')
    cfg.write_text(added_config([entry_a, entry_b]))
    assert user_db.read_bytes() == b'preserved-user-database'
    collision = config.find_project_config(str(stories[1]))['config'].copy()
    collision['collections'] = ['novel-story-0-wiki']
    collision['collectionPaths'] = {'novel-story-0-wiki': 'wiki'}
    denied(lambda: qmd_shared_store.register_config(stories[1], collision, home=home),
           'project_shared_migration_not_approved')
    source_collision = {**collision,
                        'collectionRoles': {'novel-story-0-wiki': 'source'}}
    denied(lambda: qmd_shared_store.register_config(stories[1], source_collision, home=home),
           'project_shared_migration_not_approved')

    own = stories[0]
    foreign = stories[1]
    config_a = config.find_project_config(str(own))['config']
    original_bytes = (own / 'wiki/card.md').read_bytes()
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO documents VALUES (?,?,?,1)',
            ('novel-story-0-wiki', 'card.md', hashlib.sha256(original_bytes).hexdigest()))
    rows = [
        {'file': 'qmd://novel-story-0-wiki/card.md', 'score': 1,
         'title': 'foreign-title', 'snippet': 'foreign-body',
         '_wiki_summary': 'foreign-summary'},
        {'file': 'qmd://novel-story-1-wiki/card.md', 'score': 1},
        {'file': 'qmd://novel-story-0-wiki/../secret.md', 'score': 1},
        {'file': 'qmd://novel-story-0-wiki/card.md', '_collection': 'novel-story-1-wiki'},
        {'file': str(foreign / 'wiki/card.md'), 'score': 1},
        {'file': 'qmd://novel-story-0-wiki/missing.md', 'score': 1},
        {'file': 'qmd://novel-story-0-wiki//card.md', 'score': 1},
    ]
    kept = qmd_scope.scoped_hits(rows, own, config_a, {'novel-story-0-wiki'}, index)
    assert kept == [{'file': rows[0]['file'], '_collection': 'novel-story-0-wiki',
                     'title': 'card', 'score': 1.0}]
    moved = {**config_a, 'collectionPaths': {'novel-story-0-wiki': 'new-wiki'}}
    (own / 'new-wiki').mkdir()
    (own / 'new-wiki/card.md').write_text('different unindexed synthetic card\n')
    denied(lambda: qmd_shared_store.register_config(own, moved, home=home),
           'project_shared_migration_not_approved')
    assert not qmd_scope.scoped_hits(rows[:1], own, moved, {'novel-story-0-wiki'}, index)
    assert not qmd_shared_store.owned_collections(own, moved, home=home)
    (own / 'wiki/card.md').write_text('changed before reindex\n')
    assert not qmd_scope.scoped_hits(rows[:1], own, config_a,
                                     {'novel-story-0-wiki'}, index)
    with (own / 'wiki/card.md').open('wb') as large:
        large.truncate(qmd_scope.MAX_REVISION_BYTES + 1)
    assert not qmd_scope.scoped_hits(rows[:1], own, config_a,
                                     {'novel-story-0-wiki'}, index)
    qmd_shared_store.assert_all_enrolled(home=home)
    foreign_settings = foreign / '.auto-context/settings.json'
    withdrawn = json.loads(foreign_settings.read_text())
    withdrawn['indexing'] = False
    foreign_settings.write_text(json.dumps(withdrawn))
    denied(lambda: qmd_shared_store.assert_all_enrolled(home=home),
           'plugin_qmd_revoked_collection_requires_reconciliation')
    withdrawn['indexing'] = True
    withdrawn['collectionRoles']['novel-story-1-wiki'] = 'source'
    foreign_settings.write_text(json.dumps(withdrawn))
    denied(lambda: qmd_shared_store.assert_all_enrolled(home=home),
           'plugin_qmd_revoked_collection_requires_reconciliation')
    withdrawn['collectionRoles']['novel-story-1-wiki'] = 'wiki'
    foreign_settings.write_text(json.dumps(withdrawn))
    (foreign / 'wiki/card.md').unlink()
    (foreign / 'wiki').rmdir()
    denied(lambda: qmd_shared_store.assert_all_enrolled(home=home),
           'plugin_qmd_revoked_collection_requires_reconciliation')
    assert user_db.read_bytes() == b'preserved-user-database'
    print('shared-store-and-scope-ok')
