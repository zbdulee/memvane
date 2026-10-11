"""An empty topical set must coexist with unrelated wiki documents."""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_publish as publisher


with tempfile.TemporaryDirectory(prefix='qmd-empty-topical-sync-') as name:
    root = Path(name).resolve()
    (root / topical.MARKER).write_text('synthetic only\n')
    wiki = root / 'wiki'
    (wiki / 'topical-v2').mkdir(parents=True)
    config = root / 'qmd-config'
    config.mkdir()
    (config / 'index.yml').write_text('fixture-wiki:\n')
    index = root / 'index.sqlite'
    db = sqlite3.connect(index)
    db.execute('CREATE TABLE documents (collection TEXT, path TEXT, active INTEGER)')
    db.execute('INSERT INTO documents VALUES (?,?,1)',
               ('fixture-wiki', 'ep001-main.md'))
    db.commit()
    runtime = {'QMD_CONFIG_DIR': config, 'INDEX_PATH': index}
    with patch.object(publisher, '_project', return_value=(root, wiki, 'fixture-wiki')), \
         patch.object(publisher, '_qmd_runtime', return_value=runtime), \
         patch.object(publisher, '_call', return_value=None):
        result = publisher._sync_locked(root, allow_empty=True)
        assert result['status'] == 'ready'
        assert result['verifiedCards'] == 0
        db.execute('INSERT INTO documents VALUES (?,?,1)',
                   ('fixture-wiki', 'topical-v2/old/retired.md'))
        db.commit()
        try:
            publisher._sync_locked(root, allow_empty=True)
        except topical.TopicalError as exc:
            assert exc.code == 'qmd_retired_card_still_active'
        else:
            raise AssertionError('active_retired_topical_card_accepted')
    db.close()

print(json.dumps({'legacy_coexists': True, 'stale_topical_rejected': True}))
