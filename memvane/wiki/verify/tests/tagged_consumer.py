"""Run in an archived release's module tree with a fixture project from the new writer."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

from memvane.wiki.engines import api as engines
from memvane.wiki.verify import wiki_verify_worker as verifier

bundle, root = map(Path, sys.argv[1:3])
config = json.loads(sys.argv[3])
answer = {'verdict': 'pass', 'claims': [{'claim': 'Decision', 'supported': True,
           'quote': 'Newest source', 'sourcePath': 'docs/source.md'}], 'reasons': []}
with patch.object(engines, 'run_extractor', return_value=(answer, None, 0)) as called:
    result = verifier.run(root, config, config['compile'])
for name, module in sys.modules.items():
    if name == 'memvane' or name.startswith('memvane.'):
        assert Path(module.__file__).resolve().is_relative_to(bundle), name
print(json.dumps({'remaining': result['remaining'], 'calls': called.call_count}))
