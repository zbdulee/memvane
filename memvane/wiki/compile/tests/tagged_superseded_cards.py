"""Write the cards of one source revision with a release's own compile writer.

Run with that release first on PYTHONPATH (cwd = its archive). Arguments: the
bundle, the project root, the config JSON, the selected route JSON and the card
titles to write from ``docs/source.md`` as it is now. Each title becomes one
candidate the way that release's compile worker builds it (one file source,
compiler-owned ``sourceRevisions``). Prints the writer action of each card.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace

bundle, root, config, bound, *titles = sys.argv[1:]
assert Path(writer.__file__).resolve().is_relative_to(Path(bundle).resolve())
root = Path(root)
config, bound = json.loads(config), json.loads(bound)
raw_name = next(item['name'] for item in bound[0]['collections'] if item['role'] == 'raw')
source = root / 'docs/source.md'
data = source.read_bytes()
ref = {'kind': 'file', 'path': 'docs/source.md', 'collection': raw_name}
revision = {**ref, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'mtimeNs': source.stat().st_mtime_ns}
actions = []
for title in titles:
    candidate = {'title': title, 'summary': f'Durable decision about {title.lower()}.',
                 'sources': [ref], 'authoritativeSources': [ref], 'sourceRevisions': [revision],
                 'engine': 'claude', 'trigger': 'post_tool_source'}
    with workspace.card_write_lock(root), workspace.mutation_lock(root), \
            patch.object(workspace, 'selected', return_value=bound), patch.object(workspace, 'recheck'), \
            patch.object(workspace, 'request_sync'), contextlib.redirect_stdout(io.StringIO()) as out:
        assert writer._compile_locked(root, config, config['compile'], root / config['wikiPath'],
                                      candidate) == 0, out.getvalue()
    actions.append(json.loads(out.getvalue().splitlines()[-1])['action'])

for name, module in sys.modules.copy().items():
    if name == 'memvane' or name.startswith('memvane.'):
        assert Path(module.__file__).resolve().is_relative_to(Path(bundle).resolve()), name
print(json.dumps({'actions': actions}))
