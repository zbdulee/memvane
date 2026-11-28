"""Write generated cards and their unfinished 1.x review rows with a release's own writer.

Run with that release first on PYTHONPATH (cwd = its archive). ``alpha``, ``beta`` and
``gamma`` are first cards; ``delta`` is compiled twice, so its card is the refreshed
one and the queue holds two rows for it. Prints the project root and the review rows.
"""
import contextlib
import io
import json
from pathlib import Path
import sys

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace
from tests.support.project import make_project
from tests.support.settings_fake import expanded

bundle, scratch = sys.argv[1:]
assert Path(writer.__file__).resolve().is_relative_to(Path(bundle).resolve())
project = make_project(Path(scratch), name='project', dirname='project', git=False)
root = project.root.resolve()
config = expanded(project, compile={'semanticDedup': {'enabled': False}})


def compile_card(source, title, summary):
    revision = project.revision(source)
    candidate = {'sources': [{k: revision[k] for k in ('kind', 'collection', 'path')}],
                 'sourceRevisions': [revision], 'summary': summary, 'title': title,
                 'targetPath': f'.memvane/wiki/concepts/{title.lower()}.md',
                 'engine': 'claude', 'trigger': 'post_tool_source'}
    with workspace.card_write_lock(root), workspace.mutation_lock(root), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        rc = writer._compile_locked(root, config, config['compile'], project.wiki, candidate, False)
    assert rc == 0, out.getvalue()
    return json.loads(out.getvalue().splitlines()[-1])['action']


actions = []
for name in ('alpha', 'beta', 'gamma', 'delta'):
    note = project.write_note(f'docs/{name}.md', f'{name.title()} source.')
    actions.append(compile_card(note, name.title(), f'{name.title()} fact.'))
note = project.write_note('docs/delta.md', 'Delta source, revised.')
actions.append(compile_card(note, 'Delta', 'Delta fact, revised.'))
queue = root / '.memvane/compile/verify-queue.jsonl'
rows = [json.loads(line) for line in queue.read_text(encoding='utf-8').splitlines() if line.strip()]
print(json.dumps({'root': str(root), 'actions': actions, 'rows': rows}))
