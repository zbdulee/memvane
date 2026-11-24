"""Run released writers across the loss of every transition flock in an isolated project."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys

from memvane.wiki.card import wiki_freshness as refresh
from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace
from tests.support.project import make_project
from tests.support.settings_fake import expanded

assert Path(writer.__file__).resolve().is_relative_to(Path.cwd())


def sync_file(path, raw):
    with path.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def owner(root):
    folder = root / '.memvane/compile'
    manifest = folder / 'generated-manifest.jsonl'
    public = folder / 'source-refresh-pending.jsonl'
    queue_lock = workspace.queue_lock_path(folder / 'verify-queue.jsonl')
    with workspace.card_write_lock(root), workspace.mutation_lock(root), queue_lock.open('a') as queue, \
            refresh._pending_lock_path(public).open('a') as edits:
        fcntl.flock(queue, fcntl.LOCK_EX)
        fcntl.flock(edits, fcntl.LOCK_EX)
        print('held', flush=True)
        assert input() == 'project'
        # Simulate the favorable ordering: both immutable originals and normal E1 are durable.
        snapshot = folder / 'fixture-backup'
        snapshot.mkdir()
        for path in (manifest, public):
            sync_file(snapshot / path.name, path.read_bytes())
            assert (snapshot / path.name).read_bytes() == path.read_bytes()
        sync_dir(snapshot)
        sync_dir(folder)
        temp = folder / 'fixture-projection'
        normal = [line for line in public.read_bytes().split(b'\n') if line and
                  json.loads(line)['sourcePath'] == 'docs/unrelated.md']
        sync_file(temp, normal[0] + b'\n')
        os.replace(temp, public)
        sync_dir(folder)
        sync_file(temp, b'')
        os.replace(temp, manifest)
        os._exit(77)  # Process disappears before the final directory fsync or transaction completion.


def observe_flock():
    real = fcntl.flock

    def traced(fd, op):
        if op & fcntl.LOCK_EX:
            print('waiting', file=sys.stderr, flush=True)
        return real(fd, op)
    fcntl.flock = traced


def spawn(stack, mode, root, *, payload=None):
    child = subprocess.Popen([sys.executable, '-Bu', __file__, mode, str(root)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def reap():
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    stack.callback(reap)
    if payload is not None:
        child.stdin.write(json.dumps(payload))
        child.stdin.close()
        child.stdin = None
    return child


def reached(stream, expected):
    assert select.select([stream], [], [], 8)[0], expected
    assert stream.readline().strip() == expected


def exercise(scratch, with_tombstone):
    project = make_project(scratch, name='example', dirname='project', git=False)
    root = project.root
    target = project.wiki / 'concepts/deleted.md'
    folder = root / '.memvane/compile'
    manifest = folder / 'generated-manifest.jsonl'
    public = folder / 'source-refresh-pending.jsonl'
    old_source = project.write_note('docs/a\u2028b.md', 'Original source.')
    independent = project.write_note('docs/new.md', 'An independent manual source.')
    config = expanded(project, compile={'semanticDedup': {'enabled': False}, 'verify': {'enabled': False}})

    def candidate(path):
        revision = project.revision(path)
        return {'title': 'Deleted', 'summary': 'A durable decision.', 'trigger': 'manual',
                'engine': 'claude', 'targetPath': target.relative_to(root).as_posix(),
                'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}],
                'sourceRevisions': [revision]}

    def write(path):
        with workspace.card_write_lock(root), workspace.mutation_lock(root), contextlib.redirect_stdout(io.StringIO()) as out:
            assert writer._compile_locked(root, config, config['compile'], project.wiki, candidate(path), False) == 0
        return json.loads(out.getvalue())['action']

    assert write(old_source) == 'created'
    original = (json.dumps(json.loads(manifest.read_bytes()), ensure_ascii=True) + '\n').encode()
    manifest.write_bytes(original)
    assert writer.read_jsonl(manifest) == [json.loads(original)]
    target.unlink()
    tombstone = folder / 'tombstones.jsonl'
    if with_tombstone:
        assert write(independent) == 'tombstoned'
    assert tombstone.exists() == with_tombstone
    old_event = refresh.record_pending_refresh(root, old_source.relative_to(root).as_posix(), 'codex')
    normal = refresh.record_pending_refresh(root, 'docs/unrelated.md', 'codex')
    assert old_event and normal
    public_before = public.read_bytes()
    with contextlib.ExitStack() as stack:
        moving = spawn(stack, 'owner', root)
        reached(moving.stdout, 'held')
        cli = spawn(stack, 'manual', root, payload=candidate(independent))
        hook = spawn(stack, 'hook', root)
        reached(cli.stderr, 'waiting')
        reached(hook.stderr, 'waiting')
        assert not select.select([cli.stdout, hook.stdout], [], [], .1)[0] and not target.exists()
        assert manifest.read_bytes() == original and public.read_bytes() == public_before
        moving.stdin.write('project\n')
        moving.stdin.flush()
        assert moving.wait(timeout=8) == 77
        out, err = cli.communicate(timeout=12)
        assert cli.returncode == 0, err
        hook.communicate(timeout=12)
        assert hook.returncode == 0
    assert (folder / 'fixture-backup' / manifest.name).read_bytes() == original
    assert (folder / 'fixture-backup' / public.name).read_bytes() == public_before
    pending = refresh.unresolved_pending_refreshes_strict(root)
    assert normal in pending and len(pending) == 2 and any(r['sourcePath'] == 'docs/hook.md' for r in pending)
    assert target.exists() != with_tombstone
    return json.loads(out)['action']


mode, arg = sys.argv[1:3]
if mode == 'owner':
    owner(Path(arg))
elif mode == 'hook':
    observe_flock()
    assert refresh.record_pending_refresh(Path(arg), 'docs/hook.md', 'codex')
elif mode == 'manual':
    observe_flock()
    sys.argv = [__file__, '--cwd', arg]
    raise SystemExit(writer.main())
else:
    print(json.dumps(exercise(Path(arg), mode == 'has-tombstone')))
