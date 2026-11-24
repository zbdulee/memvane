"""Leave the released deletion audit on disk at the exact manifest-append boundary."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.verify import api as verifier
from memvane.wiki.engines import api as engines
from memvane.wiki.workspace import api as workspace

FAIL = {'verdict': 'fail', 'claims': [{'claim': 'Decision', 'supported': False, 'quote': ''}],
        'reasons': ['source contradicts claim']}
open_path = Path.open
def interrupted(path, mode='r', *args, **kwargs):
    if path.name == 'generated-manifest.jsonl' and mode == 'a':
        os._exit(27)
    return open_path(path, mode, *args, **kwargs)

root = Path(sys.argv[1])
config, candidate = json.load(sys.stdin)
assert all(Path(module.__file__).resolve().is_relative_to(Path.cwd()) for module in (writer, verifier))
with workspace.card_write_lock(root), workspace.mutation_lock(root):
    assert writer._compile_locked(root, config, config['compile'], root / config['wikiPath'], candidate, False) == 0
with patch.object(engines, 'run_extractor', return_value=(FAIL, None, 0)), \
     patch.object(Path, 'open', interrupted):
    verifier.run(root, config, config['compile'])
raise SystemExit(28)
