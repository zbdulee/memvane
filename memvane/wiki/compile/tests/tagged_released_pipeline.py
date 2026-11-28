"""Run a released 1.1.4+ cutover or verifier on a fixture project with that release's own code.

Run with the release first on PYTHONPATH (cwd = its archive):
  migrate <root>          -> the release's ``migrate_from_v1`` (1.x review rows discarded)
  maintain <root>         -> the release's retention pass; prints its report and the rows left per suppression kind
  summary <root>          -> the release's ``pipeline_summary`` (1.1.6+)
  verify <root> <runs> [verdict]
                          -> the release's verify ``run`` that many times, the host CLI answering
                             ``verdict`` (default ``pass``); prints the paid calls
"""
import contextlib
import io
import json
from pathlib import Path
import sys
from unittest import mock

from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.verify import api as verify
from tests.support.project import Project
from tests.support.settings_fake import expanded

bundle, command, root = sys.argv[1:4]
assert Path(pipeline.__file__).resolve().is_relative_to(Path(bundle).resolve())
root = Path(root)
if command == 'migrate':
    counts = pipeline.migrate_from_v1(root, wiki_root=root / '.memvane/wiki')
    print(json.dumps(counts))
elif command == 'maintain':
    report = pipeline.maintain(root, wiki_root=root / '.memvane/wiki', source_roots=[root])
    kinds = {kind: len(pipeline.suppressions(root, kind)) for kind in ('verify', 'dedup', 'supersession')}
    print(json.dumps({'report': report, 'rows': kinds}, default=str))
elif command == 'summary':
    print(json.dumps(pipeline.pipeline_summary(root)['tables']['suppressions'], default=str))
else:
    project = Project(root, 'project', [])
    config = expanded(project, compile={'semanticDedup': {'enabled': False}})
    calls = []

    def answer(argv, payload, timeout, cwd):
        calls.append(payload['card']['path'])
        verdict = sys.argv[5] if len(sys.argv) > 5 else 'pass'
        return ({'verdict': verdict, 'claims': [{'claim': 'c', 'supported': verdict == 'pass', 'quote': 'q'}],
                 'reasons': []}, None, 0)
    with mock.patch('memvane.wiki.engines.api.run_extractor', answer), \
            contextlib.redirect_stdout(io.StringIO()):
        for _ in range(int(sys.argv[4])):
            verify.run(root, config, config['compile'])
    print(json.dumps({'calls': calls}))
for name, module in sys.modules.copy().items():
    if (name == 'memvane' or name.startswith('memvane.')) and getattr(module, '__file__', None):
        assert Path(module.__file__).resolve().is_relative_to(Path(bundle).resolve()), name
