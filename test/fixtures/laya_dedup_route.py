"""Synthetic selected-route exercise for the dedup CLI (no model or runtime)."""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_dedup_resolve as dedup


def exercise():
    with tempfile.TemporaryDirectory(prefix='laya-dedup-route-',
                                     dir=Path(__file__).resolve().parent) as temp:
        root = Path(temp)
        wiki = root / '.auto-context/wiki'
        pages = wiki / 'entities'
        pages.mkdir(parents=True)
        compile_dir = root / '.auto-context/compile'
        compile_dir.mkdir()
        (root / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True,
            'collections': ['raw', 'wiki'],
            'collectionPaths': {'raw': 'docs', 'wiki': '.auto-context/wiki'},
            'collectionRoles': {'raw': 'raw', 'wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {'mode': 'auto-wiki'},
        }))
        (pages / 'winner.md').write_text('---\ntitle: Winner\n---\n\nWinner.\n')
        (pages / 'loser.md').write_text('---\ntitle: Loser\n---\n\nLoser.\n')
        queue = compile_dir / 'dedup-needed.jsonl'
        pair = {'pageA': 'entities/winner.md', 'pageB': 'entities/loser.md'}
        queue.write_text(json.dumps(pair) + '\n')
        bound = ({'collections': [
            {'role': 'raw', 'sourceRoot': str(root / 'docs')},
            {'role': 'wiki', 'sourceRoot': str(wiki)}]}, {'proof': 'synthetic'})
        calls = []

        def request_sync(project, selected):
            assert project == root and selected is bound
            assert not (pages / 'loser.md').exists(), 'wake must follow deletion'
            calls.append('wake')
            if len(calls) == 1:
                raise RuntimeError('synthetic wake failure')
            return True

        def run(action, *extra):
            argv = ['wiki_dedup_resolve.py', '--cwd', str(root), '--index', '0',
                    '--action', action, *extra]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()) as output:
                result = dedup.main()
            return result, json.loads(output.getvalue())

        def recover():
            argv = ['wiki_dedup_resolve.py', '--cwd', str(root), '--recover-queue']
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                assert dedup.main() == 0

        with (patch.object(dedup.wiki_selected_laya, 'selected', return_value=bound),
              patch.object(dedup.wiki_selected_laya, 'request_sync', side_effect=request_sync),
              patch.object(dedup, 'enqueue_project_collections', side_effect=AssertionError('QMD queue used'))):
            try:
                run('merge', '--delete', 'entities/loser.md')
            except RuntimeError as exc:
                assert str(exc) == 'synthetic wake failure'
            else:
                raise AssertionError('failed wake must fail the resolve')
            assert not (pages / 'loser.md').exists()
            assert len(queue.read_text().splitlines()) == 1, 'failed wake must requeue'
            # A hard crash leaves the claimed file rather than running the
            # exception handler. The next resolver startup must reclaim it.
            queue.rename(queue.with_name(queue.name + '.claimed.99999999.synthetic'))
            queue.touch()
            recover()
            assert len(queue.read_text().splitlines()) == 1
            # The agent retries a stale pair as skip. That retry must wake Laya
            # and only then acknowledge the claimed dedup row.
            code, result = run('skip')
            assert code == 0 and result == {'action': 'skipped', 'recorded': False}
            assert queue.read_text() == ''
            assert calls == ['wake', 'wake']
            assert 'Loser.' in (compile_dir / 'dedup-deleted.jsonl').read_text()
            recovery = list((compile_dir / 'dedup-recovery').glob('*.json'))
            assert len(recovery) == 1
            assert json.loads(recovery[0].read_text())['content'].endswith('Loser.\n')
    print('selected dedup retry and legacy queue isolation passed')


if __name__ == '__main__':
    exercise()
