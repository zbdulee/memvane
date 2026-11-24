"""Read and compact a native manifest with a released reader and compactor."""
import json
import sys
from pathlib import Path

from memvane.kernel.paths import read_jsonl
from memvane.wiki.workspace import manifest_ledger

bundle, path = map(Path, sys.argv[1:3])
target = sys.argv[3]
assert Path(manifest_ledger.__file__).resolve().is_relative_to(bundle)
assert path.is_relative_to(Path.home())
for run in range(2):
    with path.open('ab') as stream:
        for number in range(64):
            noise = {'targetPath': 'wiki/unrelated.md', 'action': 'created', 'serial': [run, number],
                     'summary': 'unrelated' * 16}
            stream.write((json.dumps(noise) + '\n').encode())
    manifest_ledger.compact_manifest(path, max_bytes=0)
    rows = read_jsonl(path)
    assert [r['serial'] for r in rows if r['targetPath'] == 'wiki/unrelated.md'] == [[run, 63]]
print(json.dumps([r for r in rows if r['targetPath'] == target]))
