"""Write a project's 1.x ledgers with a separately imported release's own writers.

Run with that release first on PYTHONPATH (cwd = its archive). Prints the project
root and the bytes each writer produced.
"""
import contextlib
import io
import json
from pathlib import Path
import sys

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.dedup import wiki_dedup_resolve as dedup
from memvane.wiki.verify import wiki_verify_worker as verifier
from memvane.wiki.workspace import api as workspace
from tests.support.project import make_project
from tests.support.settings_fake import expanded

bundle, scratch = sys.argv[1:]
assert Path(writer.__file__).resolve().is_relative_to(Path(bundle).resolve())
project = make_project(Path(scratch), name='project', dirname='project', git=False)
root = project.root.resolve()
config = expanded(project, compile={'semanticDedup': {'enabled': False}, 'verify': {'enabled': False}})


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
    return json.loads(out.getvalue())['action']


alpha = project.write_note('docs/alpha.md', 'Alpha source.')
beta = project.write_note('docs/beta.md', 'Beta source.')
actions = [compile_card(alpha, 'Alpha', 'Alpha fact one.'), compile_card(beta, 'Beta', 'Beta fact.')]
gamma = project.write_note('docs/gamma.md', 'Gamma source.')
actions.append(compile_card(gamma, 'Gamma', 'Gamma fact.'))
written, ok = verifier.record_verify_suppression(
    root, config['compile'].get('verify', {}), [{'path': 'docs/beta.md', 'content': 'Beta source.'}],
    {'targetPath': '.memvane/wiki/concepts/beta.md', 'reasons': ['fixture'], 'engine': 'claude'},
    'inconclusive')
assert (written, ok) == (1, True)
assert dedup.record_skip(root, project.wiki, root / '.memvane/compile',
                         {'pageA': 'concepts/beta.md', 'pageB': 'concepts/alpha.md'})
compile_dir = root / '.memvane/compile'
print(json.dumps({'root': str(root), 'actions': actions,
                  'files': {p.name: p.read_text(encoding='utf-8') for p in compile_dir.glob('*.jsonl')}}))
