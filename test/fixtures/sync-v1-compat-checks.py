"""Literal snapshots from the pre-default-on sync writer's version-1 shape."""
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'core'))
import sync


root = '/synthetic/story'
config = root + '/.auto-context/settings.json'
states = {
    'normal': {
        'version': 1, 'projectRoot': root, 'configPath': config,
        'collections': {
            'docs': {'root': root + '/docs', 'files': {
                'a.md': {'mtimeNs': 1000, 'size': 3},
                '한글/b.md': {'mtimeNs': 2000, 'size': 4, 'sha256': '0' * 64},
            }},
        },
    },
    'empty': {
        'version': 1, 'projectRoot': root, 'configPath': config,
        'collections': {'empty': {'root': root + '/empty', 'files': {}}},
    },
    'missing_root': {
        'version': 1, 'projectRoot': root, 'configPath': config,
        'collections': {},
    },
}

with tempfile.TemporaryDirectory(prefix='qmd-sync-v1-compat-') as temp:
    folder = Path(temp)
    for name, expected in states.items():
        path = folder / f'{name}.json'
        path.write_text(json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                        encoding='utf-8')
        assert sync.load_state(path) == expected, name

    previous = states['normal']
    with mock.patch.object(sync, 'scan_files', return_value=None):
        snapshot, totals, changed, changed_files, warnings = sync.build_snapshot(
            root, config, [('docs', Path(root + '/docs'))], previous)
    assert snapshot['collections']['docs'] == previous['collections']['docs']
    assert totals == {'created': 0, 'updated': 0, 'deleted': 0}
    assert changed == {} and changed_files == {}
    assert warnings == [{'collection': 'docs', 'reason': 'missing_root'}]
    carryover = folder / 'carryover.json'
    carryover.write_text(json.dumps(snapshot), encoding='utf-8')
    assert sync.load_state(carryover) == snapshot

print('sync-v1-compat-ok')
