"""Production shared-owner prune protocol on a disposable proven QMD store."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_shared_store
import runtime_update


def denied(call, code):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == code, (str(exc), code)
    else:
        raise AssertionError('unowned shared collection removal accepted')


with tempfile.TemporaryDirectory(prefix='qmd-owned-prune-', dir=Path.cwd()) as temp:
    home = Path(temp).resolve()
    project = home / 'project'
    docs = project / 'docs'
    docs.mkdir(parents=True)
    (docs / 'synthetic.md').write_text('Synthetic document.\n')
    config = {'indexing': True, 'collections': ['synthetic-docs'],
              'collectionPaths': {'synthetic-docs': 'docs'}}
    settings = project / '.auto-context/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps(config))
    base = qmd_shared_store.root(home)
    generation = base / 'generations' / ('g-' + 'a' * 32)
    config_dir = generation / 'config'
    cache = generation / 'cache'
    cache.mkdir(parents=True)
    config_dir.mkdir()
    for directory in (base, base / 'generations', generation, config_dir, cache):
        directory.chmod(0o700)
    index = generation / 'index.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        db.execute('CREATE TABLE content(hash TEXT)')
        db.execute('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT)')
        db.execute('CREATE TABLE store_collections(name TEXT,path TEXT,pattern TEXT)')
    index.chmod(0o600)
    baseline = 'collections: {}\nmodels: {}\n'
    qmd_config = config_dir / 'index.yml'
    qmd_config.write_text(baseline)
    qmd_config.chmod(0o600)
    proof = generation / 'prepared.json'
    proof.write_text(json.dumps({'schema': qmd_shared_store.SCHEMA,
        'generation': generation.name,
        'configSha256': hashlib.sha256(qmd_config.read_bytes()).hexdigest(),
        'indexSchemaSha256': runtime_update.inspect_index(
            index, include_file_hash=False)['schemaSha256']}))
    proof.chmod(0o600)
    pointer = base / 'active.json'
    pointer.write_text(json.dumps({'schema': qmd_shared_store.SCHEMA,
                                   'generation': generation.name}))
    pointer.chmod(0o600)
    # Legacy selected stores retain preexisting registrations only.
    qmd_shared_store._reserve_owners(base,
        qmd_shared_store._collection_owners(project, config))
    qmd_shared_store.register_config(project, config, home=home)
    denied(lambda: qmd_shared_store.verify_missing_collection(
        project, 'synthetic-docs', config, home=home),
        'plugin_qmd_collection_scope_invalid')
    qmd_config.write_text('collections:\n  synthetic-docs:\n'
                          f'    path: {docs}\n    pattern: "**/*.md"\n'
                          'models: {}\n')
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO store_collections VALUES (?,?,?)',
                   ('synthetic-docs', str(docs), '**/*.md'))
    assert qmd_shared_store.select(home)
    (docs / 'synthetic.md').unlink(); docs.rmdir()
    qmd_shared_store.verify_missing_collection(project, 'synthetic-docs', config, home=home)
    foreign = home / 'foreign'; foreign.mkdir()
    added_config = qmd_config.read_text()
    qmd_config.write_text(baseline)
    with sqlite3.connect(index) as db:
        db.execute('UPDATE store_collections SET path=? WHERE name=?',
                   (str(foreign), 'synthetic-docs'))
    denied(lambda: qmd_shared_store.verify_missing_collection(
        project, 'synthetic-docs', config, home=home),
        'plugin_qmd_collection_scope_invalid')
    with sqlite3.connect(index) as db:
        db.execute('UPDATE store_collections SET path=? WHERE name=?',
                   (str(docs), 'synthetic-docs'))
    qmd_config.write_text(added_config)
    denied(lambda: qmd_shared_store.verify_missing_collection(
        foreign, 'synthetic-docs', config, home=home),
        'plugin_qmd_collection_scope_invalid')
    denied(lambda: qmd_shared_store.retire_pruned(project, {'indexing': False,
            'collections': []}, home=home), 'plugin_qmd_prune_not_settled')
    with sqlite3.connect(index) as db:
        db.execute('DELETE FROM store_collections WHERE name=?', ('synthetic-docs',))
    qmd_config.write_text(baseline)
    retired = qmd_shared_store.retire_pruned(project,
        {'indexing': False, 'collections': []}, home=home)
    assert retired == 1
    assert qmd_shared_store._registry(base) == {}
    qmd_shared_store.assert_all_enrolled(home=home)
    print(json.dumps({'exactOwner': True, 'waitedForPhysicalRemoval': True,
                      'retired': retired}))
