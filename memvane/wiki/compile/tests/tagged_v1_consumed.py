"""1.x review rows a release's own verifier already consumed, and rows a dead claim left behind.

Run with that release first on PYTHONPATH (cwd = its archive). Every card and row is the
release's own output; only the leftover claimed files are placed the way the release
leaves them (its claim renames the queue; a run that cannot clean up keeps the file):

- ``alpha``: compiled, then judged ``inconclusive`` by the release's verifier with
  ``onInconclusive: none`` (card kept ``generated``, row consumed, ``verify-log.jsonl``
  entry). The queue bytes it held stay behind as a claimed file.
- ``gamma``: compiled, its queue claimed (renamed), its source changed and compiled
  again, so the claimed row is older than the queue row of the same card.
- ``beta``: compiled, its row waits in the queue.
- ``delta``: compiled while review is off, so it never had a row.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest import mock

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.verify import api as verifier
from memvane.wiki.workspace import api as workspace
from tests.support.project import make_project
from tests.support.settings_fake import expanded

bundle, scratch = sys.argv[1:]
assert Path(writer.__file__).resolve().is_relative_to(Path(bundle).resolve())
project = make_project(Path(scratch), name='project', dirname='project', git=False)
root = project.root.resolve()
compile_dir = root / '.memvane/compile'
queue = compile_dir / 'verify-queue.jsonl'


def settings(review=True):
    return expanded(project, compile={'semanticDedup': {'enabled': False},
                                      'verify': {'enabled': review, 'onInconclusive': 'none'}})


def compile_card(name, text, review=True):
    note = project.write_note(f'docs/{name}.md', text)
    revision = project.revision(note)
    config = settings(review)
    candidate = {'sources': [{k: revision[k] for k in ('kind', 'collection', 'path')}],
                 'sourceRevisions': [revision], 'summary': text, 'title': name.title(),
                 'targetPath': f'.memvane/wiki/concepts/{name}.md', 'engine': 'claude',
                 'trigger': 'post_tool_source'}
    with workspace.card_write_lock(root), workspace.mutation_lock(root), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        rc = writer._compile_locked(root, config, config['compile'], project.wiki, candidate, False)
    assert rc == 0, out.getvalue()
    return json.loads(out.getvalue().splitlines()[-1])['action']


actions = [compile_card('alpha', 'Alpha source.')]
held = queue.read_bytes()
answer = {'verdict': 'inconclusive', 'claims': [], 'reasons': ['fixture']}
with mock.patch('memvane.wiki.engines.api.run_extractor', return_value=(answer, None, 0)), \
        contextlib.redirect_stdout(io.StringIO()):
    verifier.run(root, settings(), settings()['compile'])
assert 'status: generated' in (project.wiki / 'concepts/alpha.md').read_text(), 'alpha was not kept'
assert not queue.exists() or not queue.read_text().strip(), 'alpha row was not consumed'
leftover = compile_dir / 'verify-queue.jsonl.claimed.4242.a1'
leftover.write_bytes(held)
os.utime(leftover, ns=(1, 1))
actions.append(compile_card('gamma', 'Gamma source.'))
os.rename(queue, compile_dir / 'verify-queue.jsonl.claimed.4343.b2')
actions.append(compile_card('gamma', 'Gamma source, revised.'))
actions.append(compile_card('beta', 'Beta source.'))
actions.append(compile_card('delta', 'Delta source.', review=False))
log = [json.loads(line) for line in (compile_dir / 'verify-log.jsonl').read_text().splitlines() if line.strip()]
print(json.dumps({'root': str(root), 'actions': actions,
                  'log': [(row.get('targetPath'), row.get('result')) for row in log]}))
