"""Tiny isolated SQLite store for setup-guard tests; no QMD invocation."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_shared_store
import runtime_update

home = Path(sys.argv[1]).resolve()
base = qmd_shared_store.root(home)
generation = base / 'generations' / ('g-' + 'a' * 32)
config = generation / 'config'
cache = generation / 'cache'
config.mkdir(mode=0o700, parents=True)
cache.mkdir(mode=0o700)
for directory in (base, base / 'generations', generation, config, cache):
    directory.chmod(0o700)
index = generation / 'index.sqlite'
with sqlite3.connect(index) as db:
    db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
    db.execute('CREATE TABLE content(hash TEXT)')
    db.execute('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT)')
    db.execute('CREATE TABLE store_collections(name TEXT)')
index.chmod(0o600)
cfg = config / 'index.yml'
cfg.write_text('models: {}\n')
cfg.chmod(0o600)
proof = generation / 'prepared.json'
proof.write_text(json.dumps({'schema': qmd_shared_store.SCHEMA,
    'generation': generation.name, 'configSha256': hashlib.sha256(cfg.read_bytes()).hexdigest(),
    'indexSchemaSha256': runtime_update.inspect_index(index, include_file_hash=False)['schemaSha256']}))
proof.chmod(0o600)
pointer = base / 'active.json'
pointer.write_text(json.dumps({'schema': qmd_shared_store.SCHEMA,
                               'generation': generation.name}))
pointer.chmod(0o600)
