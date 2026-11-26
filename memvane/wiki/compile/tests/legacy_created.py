"""Create the review fixture's public manifest row with the unmodified v1.1.0 writer."""
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.release_tags import archived_tag


def old_created(case, separator):
    repo = Path.cwd()
    bundle = archived_tag(repo, case.tmp, 'v1.1.0')
    note = case.project.write_note('docs/legacy' + separator + 'only.md', 'Original unrelated source.')
    revision = case.project.revision(note)
    card = case.target.with_name('legacy.md')
    candidate = {'title': 'Legacy', 'summary': 'Old unrelated card.', 'engine': 'claude', 'trigger': 'manual',
                 'targetPath': card.relative_to(case.root).as_posix(), 'sourceRevisions': [revision],
                 'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}]}
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('released_writer.py')), str(case.root)],
                            input=json.dumps([case.config, candidate]), cwd=bundle, text=True, capture_output=True,
                            timeout=15, env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))})
    case.assertEqual(result.returncode, 0, result.stderr)
    case.assertEqual(json.loads(result.stdout)['action'], 'created')
    raw = case.queue.with_name('generated-manifest.jsonl').read_bytes()
    case.assertEqual(json.loads(raw)['sourceRevisions'][0]['path'], revision['path'])
    case.assertTrue(card.is_file())
    return raw
