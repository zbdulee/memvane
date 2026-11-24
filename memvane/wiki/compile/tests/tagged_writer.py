"""Create both generations with an archived writer inside its fixture checkout."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace

bundle, root, config, bound = sys.argv[1:5]
root = Path(root)
config, bound = map(json.loads, (config, bound))
source = root / 'docs/source.md'

revisions = (
        ('Older source.', 'Old decision.', '2099-01-01T00:00:00Z'),
        ('Newest source.', 'Latest decision.', '2026-01-01T00:00:00Z'))
if sys.argv[5:] == ['single']:
    revisions = [('Older source for the decision.', 'Decision in the current source.', writer.now_iso())]
for content, summary, stamp in revisions:
    source.write_text(content)
    raw = source.read_bytes()
    ref = {'kind': 'file', 'path': 'docs/source.md', 'collection': bound[0]['collections'][0]['name']}
    revision = {**ref, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                'mtimeNs': source.stat().st_mtime_ns}
    candidate = {'title': 'Decision', 'summary': summary, 'sources': [ref], 'sourceRevisions': [revision],
                 'targetPath': config['wikiPath'] + '/concepts/decision.md',
                 'engine': 'claude', 'trigger': 'post_tool_source'}
    with workspace.card_write_lock(root), workspace.mutation_lock(root), \
         patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'), \
         patch.object(workspace, 'request_sync'), patch.object(writer, 'now_iso', return_value=stamp):
        assert writer._compile_locked(root, config, config['compile'], root / config['wikiPath'], candidate, False) == 0

for name, module in sys.modules.copy().items():
    if name == 'memvane' or name.startswith('memvane.'):
        assert Path(module.__file__).resolve().is_relative_to(Path(bundle)), name
