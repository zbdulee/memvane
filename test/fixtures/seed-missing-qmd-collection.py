"""Represent a previously enrolled, now absent collection in a private store."""
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_shared_store

home, project, name, relative = sys.argv[1:]
home = Path(home).resolve()
project = Path(project).resolve()
missing = project / relative
assert not missing.exists() and not missing.is_symlink()
base = qmd_shared_store.root(home)
selected = qmd_shared_store.select(home)
registry = base / 'collections.json'
records = json.loads(registry.read_text()) if registry.exists() else {}
records[name] = {'scope': str(project), 'path': str(missing), 'indexed': True}
registry.write_text(json.dumps(records))
registry.chmod(0o600)
with sqlite3.connect(selected['INDEX_PATH']) as db:
    db.execute('INSERT INTO store_collections(name,path,pattern) VALUES(?,?,?)',
               (name, str(missing), '**/*.md'))
