"""Run one PostToolUse edit with a full disk on Beta's first write."""
import builtins
from contextlib import ExitStack, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from memvane.wiki.compile.tests.integration import test_revision_skip as fixture


def observe(released):
    case = fixture.RevisionSkipChecks()
    try:
        case.setUp()
        return _run(case, released)
    finally:
        case.doCleanups()


def _run(case, released):
    worker = fixture.worker
    writer = fixture.wc
    workspace = fixture.wiki_selected_laya
    case.config['compile']['semanticDedup'] = {'enabled': False}
    candidates = [{'title': title, 'summary': 'Cards cite source.'} for title in ('Alpha', 'Beta')]
    writes = []
    disk_errors = []

    def full_disk(*args, **kwargs):
        disk_errors.append(errno.ENOSPC)
        raise OSError(errno.ENOSPC, 'fixture')

    def write(root, candidate):
        title = candidate['title']
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            if title == 'Beta' and not disk_errors:
                if released:
                    # 1.1.0 creates a temporary card before its atomic rename.
                    open_file = builtins.open

                    def open_card(path, *args, **kwargs):
                        if isinstance(path, Path) and path.name.startswith('.beta.md.tmp-'):
                            return full_disk()
                        return open_file(path, *args, **kwargs)

                    stack.enter_context(patch.object(builtins, 'open', open_card))
                else:
                    # Native first writes an obligation before publishing the card.
                    from memvane.wiki.verify import api as verify_api
                    def refused_obligation(*args, **kwargs):
                        # The verifier reports a full disk as an unwritten card.
                        disk_errors.append(errno.ENOSPC)
                        return False
                    stack.enter_context(patch.object(verify_api, 'create_first_card', refused_obligation))
            rc = writer._compile_locked(root, case.config, case.config['compile'], case.wiki,
                                        candidate, False)
        result = json.loads(output.getvalue())
        writes.append([title, result['action']])
        return None if rc else result

    answers = [({'candidates': values}, None, 0) for values in (candidates, candidates[:1])]
    with ExitStack() as stack:
        stack.enter_context(patch.object(worker, 'compile_candidate', side_effect=write))
        extractor = stack.enter_context(patch.object(worker, 'run_extractor', side_effect=answers))
        stack.enter_context(patch.object(worker, '_run_verify_pass'))
        stack.enter_context(patch.object(workspace, 'request_compile'))
        stack.enter_context(patch.object(workspace, 'request_sync'))
        queued = fixture.wiki_compile_enqueue.enqueue_payload({
            'hook_event_name': 'PostToolUse', 'cwd': str(case.root), 'engine': 'claude',
            'tool_input': {'file_path': str(case.source)},
        })
        passes = [case.run_main() for _ in range(3)]
    return {'queued': queued['queued'], 'passes': passes, 'diskErrors': disk_errors,
            'extractor': extractor.call_count, 'writes': writes,
            'cards': sorted(p.relative_to(case.wiki).as_posix() for p in case.wiki.rglob('*.md'))}


if __name__ == '__main__':
    for module in (fixture.worker, fixture.wc, fixture.wiki_compile_enqueue):
        assert Path.cwd() in Path(module.__file__).resolve().parents, module.__file__
    print(json.dumps(observe(released=True)))
