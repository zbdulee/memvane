"""Select a fully proved synthetic QMD CLI under an isolated test HOME."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_runtime
import qmd_shared_store
import runtime_update

home = Path(sys.argv[1]).resolve()
store = qmd_shared_store.root(home)
generation = store / 'generations' / ('g-' + 'a' * 32)
index = generation / 'index.sqlite'
with sqlite3.connect(index) as db:
    db.execute('ALTER TABLE store_collections ADD COLUMN path TEXT')
    db.execute('ALTER TABLE store_collections ADD COLUMN pattern TEXT')
proof_path = generation / 'prepared.json'
proof = json.loads(proof_path.read_text())
proof['indexSchemaSha256'] = runtime_update.inspect_index(
    index, include_file_hash=False)['schemaSha256']
proof_path.write_text(json.dumps(proof))
proof_path.chmod(0o600)
package = home / 'synthetic-qmd'
binary = package / 'bin' / 'qmd.js'
binary.parent.mkdir(mode=0o700, parents=True)
(package / 'package.json').write_text(json.dumps({'version': qmd_runtime.VERSION}))
binary.write_text('''#!/usr/bin/env node
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const args = process.argv.slice(2);
if (args[0] === '--version') { console.log('qmd 2.5.3'); process.exit(0); }
if (args[0] === '--help') {
  console.log('qmd vsearch\\nqmd search\\nqmd get\\nqmd collection add\\nqmd update\\nqmd embed');
  process.exit(0);
}
const stub = process.env.QMD_TEST_STUB || path.join(path.dirname(process.env.HOME), 'bin', 'qmd');
const result = spawnSync(stub, args, {stdio: 'inherit', env: process.env});
if (result.status === 0 && args[0] === 'collection' && args[1] === 'remove' && process.env.INDEX_PATH) {
  const sql = 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute("DELETE FROM store_collections WHERE name=?", (sys.argv[2],)); db.commit()';
  const removed = spawnSync('python3', ['-c', sql, process.env.INDEX_PATH, args[2]], {stdio: 'inherit'});
  if (removed.status !== 0) process.exit(removed.status == null ? 1 : removed.status);
}
process.exit(result.status == null ? 1 : result.status);
''')
binary.chmod(0o700)
# Hosted CI Node binaries may be group-writable. The runtime intentionally
# refuses those paths, so probe an owner-private launcher in this synthetic
# HOME. This fixture does not test acceptance of the host Node binary itself.
host = shutil.which('node')
if host is None:
    raise SystemExit('node not found for synthetic qmd runtime')
host_node = Path(host).resolve()
node = home / 'synthetic-node'
fd = os.open(node, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
with os.fdopen(fd, 'w') as stream:
    stream.write('#!/bin/sh\nexec ' + shlex.quote(str(host_node)) + ' "$@"\n')
root = qmd_runtime.managed_root(home)
stage = qmd_runtime.prepare_existing(root, binary, node)
qmd_runtime.activate(root, stage['generation'], require_unselected=True)
print(stage['wrapper'])
