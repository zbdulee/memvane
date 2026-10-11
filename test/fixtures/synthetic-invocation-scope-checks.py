"""Synthetic-only invocation boundary; no QMD, provider, or private story data."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


with tempfile.TemporaryDirectory(prefix='qmd-invocation-scope-', dir='/tmp') as temp:
    repository = Path.cwd()
    home = Path(temp).resolve()
    os.environ['HOME'] = str(home)
    sys.path.insert(0, str(repository / 'core'))
    import config
    import hook_budget
    import index_enqueue
    import posttool
    import update_hook_queue
    import wiki_compile_enqueue
    from context_learning import synthetic_scope as scope
    from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME

    novel = home / 'work/novel'
    story = novel / PLANNED_BUNDLE_NAME
    docs = novel / 'docs'
    idea = novel / 'idea'
    ordinary = novel / 'ordinary-story'
    for directory in (story, docs, idea, ordinary):
        directory.mkdir(parents=True)
    root_settings = novel / '.auto-context/settings.json'
    root_settings.parent.mkdir()
    paths = scope.project_collection_paths(novel)
    root_settings.write_text(json.dumps({
        'indexing': True, 'collections': list(paths), 'collectionPaths': paths,
        'sharedLearning': {'enabled': True, 'root': str(novel / '.qmd-fantasy-shared-20261008'),
                           'trialSelection': True},
    }))
    other_settings = ordinary / '.auto-context/settings.json'
    other_settings.parent.mkdir()
    other_settings.write_text(json.dumps({
        'indexing': True, 'collections': ['ordinary-wiki'],
        'collectionPaths': {'ordinary-wiki': '.auto-context/wiki'},
    }))
    found = config.find_project_config(str(story))
    assert found['projectRoot'] == str(novel)
    assert scope.invocation_in_generated_story(found, str(story))
    assert scope.invocation_in_generated_story(found, str(story), ['04_Manuscript/new.md'])
    assert not scope.invocation_in_generated_story(found, None)
    assert not scope.invocation_in_generated_story(found, str(novel))
    assert not scope.invocation_in_generated_story(found, str(docs))
    assert not scope.invocation_in_generated_story(found, str(idea))
    assert not scope.invocation_in_generated_story(found, str(story), [])
    assert not scope.invocation_in_generated_story(found, str(story), ['04_Manuscript/new.md', str(docs / 'x.md')])
    assert not scope.invocation_in_generated_story(found, str(story), [str(idea / 'x.md')])
    assert not scope.invocation_in_generated_story(found, str(docs), [str(story / '04_Manuscript/new.md')])
    assert scope.invocation_in_generated_story(config.find_project_config(str(ordinary)), str(ordinary))
    linked = story / 'linked-docs'
    linked.symlink_to(docs, target_is_directory=True)
    assert not scope.invocation_in_generated_story(found, str(story), ['linked-docs/x.md'])
    with patch.object(scope, 'NOVEL_ROOT', home / 'absent-novel'):
        assert scope.invocation_in_generated_story(config.find_project_config(str(ordinary)), str(ordinary))

    fixture = home / 'query.json'
    fixture.write_text('{"results":[]}')
    log = home / 'recall.jsonl'
    env = {**os.environ, 'QMD_QUERY_FIXTURE': str(fixture), 'QMD_RECALL_LOG': str(log),
           'QMD_SETUP_GUARD_FIXTURE': ''}
    prompt = 'Please find the established fantasy lore and characters from the latest episode.'
    for cwd, expected in ((docs, 'invocation_outside_generated_story'),
                          (idea, 'invocation_outside_generated_story'),
                          (novel, 'invocation_outside_generated_story'),
                          (story, 'project_index_invalid'),
                          (ordinary, 'project_index_invalid')):
        result = subprocess.run([sys.executable, str(repository / 'core/recall.py')],
            input=json.dumps({'prompt': prompt, 'cwd': str(cwd)}), text=True,
            capture_output=True, env=env, check=False)
        assert result.returncode == 0 and not result.stdout, (cwd, result.returncode, result.stdout, result.stderr)
        observed = json.loads(log.read_text().splitlines()[-1])
        assert observed['reason'] == expected, (cwd, observed)
    result = subprocess.run([sys.executable, str(repository / 'core/recall.py')],
        input=json.dumps({'prompt': prompt}), text=True, capture_output=True,
        env={**env, 'QMD_QUERY_FIXTURE': str(fixture)}, cwd=story, check=False)
    assert result.returncode == 0 and not result.stdout, (result.returncode, result.stdout, result.stderr)
    assert json.loads(log.read_text().splitlines()[-1])['reason'] == 'invocation_outside_generated_story'

    edit = {'hook_event_name': 'PostToolUse', 'cwd': str(story),
            'tool_name': 'Write', 'tool_input': {'file_path': str(docs / 'x.md'),
                                                'content': 'This is a long synthetic edit sample.'}}
    with patch.object(hook_budget, 'arm', return_value=None), \
            patch.object(sys, 'stdin', io.StringIO(json.dumps(edit))), \
            patch.object(posttool.subprocess, 'Popen', side_effect=AssertionError('recall_spawned')):
        assert posttool.main() == 0
    with patch.object(sys, 'stdin', io.StringIO(json.dumps(edit))), \
            patch.object(index_enqueue, 'enqueue', side_effect=AssertionError('index_enqueued')):
        assert index_enqueue.main() == 0
    with patch.object(update_hook_queue, '_private_queue', side_effect=AssertionError('queue_created')):
        update_hook_queue.enqueue(json.dumps({'cwd': str(docs)}))
    with patch.object(sys, 'stdin', io.StringIO(json.dumps(edit))), \
            patch.object(wiki_compile_enqueue, 'compile_gate', side_effect=AssertionError('compile_checked')):
        assert wiki_compile_enqueue.main() == 0

    manager_log = home / 'manager.log'
    recall_log = home / 'recall-called'
    index_log = home / 'index-called'
    manager = home / 'fake-manager.sh'
    manager.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$HOME/manager.log"\n')
    for path in (recall_log, index_log):
        script = home / (path.name + '.py')
        script.write_text('from pathlib import Path\nPath(%r).write_text("called")\n' % str(path))
    hook_env = {**env, 'QMD_SETUP_GUARD_FIXTURE': '1',
                'QMD_BACKEND_MANAGER': str(manager),
                'QMD_CORE_RECALL_SCRIPT': str(home / 'recall-called.py'),
                'QMD_CORE_INDEX_SCRIPT': str(home / 'index-called.py'),
                'QMD_RECALL_LOG': '', 'QMD_QUERY_FIXTURE': ''}
    dispatcher = repository / 'hooks/run-hook'
    for action, payload in (
            ('recall', {'cwd': str(docs), 'prompt': prompt}),
            ('recall', {'cwd': str(docs), 'effective_cwd': str(ordinary), 'prompt': prompt}),
            ('index', edit), ('compile', edit)):
        result = subprocess.run(['bash', str(dispatcher), action, 'codex'],
            input=json.dumps(payload), text=True, capture_output=True,
            cwd=docs, env=hook_env, check=False)
        assert result.returncode == 0 and not result.stdout, (action, result.stderr)
    assert not any(path.exists() for path in (manager_log, recall_log, index_log))
    result = subprocess.run(['bash', str(dispatcher), 'recall', 'codex'],
        input=json.dumps({'cwd': str(story), 'prompt': prompt}), text=True,
        capture_output=True, cwd=story, env=hook_env, check=False)
    assert result.returncode == 0 and recall_log.read_text() == 'called', result.stderr

    sys.path.insert(0, str(repository))
    from hermes_adapter import core_bridge as bridge
    with patch.object(bridge, '_setup_allowed', return_value=True), \
            patch.object(bridge, '_run_quiet') as quiet, \
            patch.object(bridge, '_run') as run, \
            patch.object(bridge, '_ensure_background') as background:
        bridge.session_update(cwd=str(docs))
        bridge.recall_context(user_message=prompt, cwd=str(docs))
        bridge.pre_edit_gate(tool_name='write_file', args={'path': str(docs / 'x.md')}, cwd=str(docs))
        bridge.post_edit_sync(tool_name='write_file', args={'path': str(docs / 'x.md')},
                              result='{}', status='ok', cwd=str(docs))
        previous = os.getcwd()
        try:
            os.chdir(story)
            bridge.session_update()
        finally:
            os.chdir(previous)
        quiet.assert_not_called()
        run.assert_not_called()
        background.assert_not_called()
        bridge.session_update(cwd=str(story))
        assert quiet.call_count == 3 and run.call_count == 1

print('synthetic-invocation-scope-ok')
