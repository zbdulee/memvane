"""Compact both native ledgers twice in an archived release and reread the exact logical rows."""
import json
from pathlib import Path
import sys

from memvane.wiki.card import wiki_freshness as refresh
from memvane.wiki.workspace import api as workspace

bundle, root = map(Path, sys.argv[1:3])
assert root.is_relative_to(Path.home()) and Path(refresh.__file__).resolve().is_relative_to(bundle)
if sys.argv[3] == 'seed':
    pending = refresh.record_pending_refresh(root, 'docs/open.md', 'codex')
    done = refresh.record_pending_refresh(root, 'docs/settled.md', 'codex')
    revision = dict(refresh.snapshot_file(root / 'docs/settled.md'), kind='file', path='docs/settled.md', collection='raw')
    assert refresh.resolve_pending_refresh(root, done, revision, [revision])
    print(json.dumps([pending, done]))
    sys.exit(0)

manifest, ledger = map(Path, sys.argv[3:5])
target, pending_id, resolved_id = sys.argv[5:8]
assert manifest.is_relative_to(root) and ledger.is_relative_to(root)
noise = root / 'docs/compaction-control.md'
noise.write_text('unrelated compaction control')
noise_revision = dict(refresh.snapshot_file(noise), kind='file', path='docs/compaction-control.md', collection='raw')
template = next(row for row in map(json.loads, manifest.read_bytes().splitlines()) if row.get('targetPath') == target)
result = []
for run in range(2):
    obsolete = refresh.record_pending_refresh(root, 'docs/compaction-control.md', 'codex')
    fresh = refresh.record_pending_refresh(root, 'docs/compaction-control.md', 'codex')
    assert refresh.resolve_pending_refresh(root, fresh, noise_revision, [noise_revision])
    with manifest.open('ab') as stream:
        for pos in range(64):
            row = dict(template, targetPath='.memvane/wiki/concepts/unrelated.md', summary=f'other {run} {pos}')
            stream.write((json.dumps(row) + '\n').encode())
    before = manifest.stat().st_ino, ledger.stat().st_ino
    workspace.compact_manifest(manifest, max_bytes=0)
    assert refresh.compact_pending_refreshes(root, force=True)
    assert before[0] != manifest.stat().st_ino and before[1] != ledger.stat().st_ino
    records = [json.loads(row) for row in manifest.read_text().splitlines()]
    assert [r['summary'] for r in records if r.get('targetPath') == '.memvane/wiki/concepts/unrelated.md'] == [f'other {run} 63']
    pending_rows = [json.loads(row) for row in ledger.read_text().splitlines()]
    ids = [r.get('eventId') for r in pending_rows]
    assert fresh['eventId'] in ids and obsolete['eventId'] not in ids and pending_id in ids and resolved_id in ids
    assert any(r.get('pendingEventId') == resolved_id for r in pending_rows)
    assert [r['eventId'] for r in refresh.unresolved_pending_refreshes_strict(root)] == [pending_id]
    result.append([r for r in records if r.get('targetPath') == target][-1])
assert all(Path(m.__file__).resolve().is_relative_to(bundle) for k,m in sys.modules.items() if k=='memvane' or k.startswith('memvane.'))
print(json.dumps(result))
