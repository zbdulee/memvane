"""Produce the on-disk legacy histories using a separately imported release."""
import contextlib
import errno
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace
from memvane.wiki.card import api as cards
from tests.support.project import make_project
from tests.support.settings_fake import expanded

bundle, scratch, char, escaped = sys.argv[1:]
assert Path(writer.__file__).resolve().is_relative_to(Path(bundle))
scratch, char, escaped = Path(scratch), chr(int(char)), escaped == 'escaped'
project = make_project(scratch, name='project', dirname='project', git=False)
root = project.root.resolve()
config = expanded(project, compile={'semanticDedup': {'enabled': False}, 'verify': {'enabled': False}})
base = root / '.memvane/compile'
target = project.wiki / 'concepts/deleted.md'
known = target.with_name('known.md')
normal = project.write_note('docs/normal.md', 'normal')
project.write_note('docs/incoming.md', 'independent current source')
odd = project.write_note('docs/a' + char + 'b.md', 'excluded')


def write(source, card):
    revision = project.revision(source)
    candidate = {'sources': [{k: revision[k] for k in ('kind', 'collection', 'path')}],
                 'sourceRevisions': [revision], 'summary': 'Joint fixture evidence.',
                 'title': card.stem, 'targetPath': card.relative_to(root).as_posix(),
                 'engine': 'claude', 'trigger': 'post_tool_source'}
    with workspace.card_write_lock(root), workspace.mutation_lock(root), contextlib.redirect_stdout(io.StringIO()) as out:
        rc = writer._compile_locked(root, config, config['compile'], project.wiki, candidate, False)
    assert rc == 0 and json.loads(out.getvalue())['action'] == 'created'


def snapshot():
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def lf(path):
    return [json.loads(row) for row in path.read_bytes().split(b'\n')[:-1]]


def encoded(rows, ascii):
    return b''.join((json.dumps(row, ensure_ascii=ascii, sort_keys=True) + '\n').encode() for row in rows)


def accumulate(path, row, count):
    for _ in range(count):
        writer.append_jsonl(path, row)


manifest = base / 'generated-manifest.jsonl'
audit = base / 'candidates.jsonl'
write(normal, known)
normal_row, = lf(manifest)
normal_audit, = lf(audit)
old = cards.record_pending_refresh(root, 'docs/normal.md', 'codex')
assert old and cards.unresolved_pending_refreshes_strict(root) == [old]
baseline = scratch / 'starting-copy'
shutil.copytree(root, baseline)
histories = []
for created in (True, False):
    if not created:
        root.rename(scratch / 'deleted-history')
        shutil.copytree(baseline, root)
    if created:
        write(odd, target)
        target.unlink()
        assert lf(manifest)[-1]['action'] == 'created'
        if escaped:
            manifest.write_bytes(encoded(lf(manifest), True))
    # Real log trimming drops the prefix; subsequent attempts in these histories can differ in number.
    count = len(audit.read_text().splitlines())
    accumulate(audit, normal_audit, 512 - count)
    assert audit.stat().st_size > 262144
    workspace.trim_jsonl(audit)
    assert all(row['targetPath'] == normal_audit['targetPath'] for row in lf(audit))

    # The tag tolerates a stamp write failure after a successful manifest replace.
    real_write = Path.write_text
    def fail_stamp(path, *args, **kwargs):
        if path == manifest.with_name(manifest.name + '.compact-stamp'):
            raise OSError(errno.ENOSPC, 'injected stamp failure')
        return real_write(path, *args, **kwargs)
    seen = []
    with patch.object(Path, 'write_text', fail_stamp):
        for _ in range(2):
            accumulate(manifest, normal_row, 512)
            assert manifest.stat().st_size > 262144
            workspace.compact_manifest(manifest)
            seen.append(any(row['targetPath'] == target.relative_to(root).as_posix() for row in lf(manifest)))
    if created:
        assert seen == [escaped, False], seen
    assert not manifest.with_name(manifest.name + '.compact-stamp').exists()
    assert cards.unresolved_pending_refreshes_strict(root) == [old]
    odd.rename(root / 'docs/moved.md')
    home = Path.home()
    external = {path.relative_to(home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in home.rglob('*') if path.is_file()}
    histories.append((snapshot(), external))
assert histories[0] == histories[1], 'the full project or installation evidence differs'
assert not target.exists()
assert not histories[1][1], 'the isolated release installed additional external evidence'
print(json.dumps({'target': target.relative_to(root).as_posix(), 'paths': sorted(histories[1][0]),
                  'identicalTrees': True, 'externalFiles': len(histories[1][1])}))
