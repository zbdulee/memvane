"""Disposable exact-story gate and parent QMD routing checks; no model or index."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import setup_guard
import qmd_route
import topical_story_scope as scope
import wiki_topical_backend as backend
import wiki_topical_publish as publisher


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode()
    path.write_bytes(raw)
    path.chmod(mode)
    return hashlib.sha256(raw).hexdigest()


with tempfile.TemporaryDirectory(prefix='qmd-story-scope-') as temp:
    parent = Path(temp).resolve() / 'novel'; parent.mkdir(mode=0o700)
    story = parent / 'planned-story'; story.mkdir(mode=0o700)
    shared = parent / 'shared'; shared.mkdir(mode=0o700)
    wiki = story / '.auto-context/wiki'; wiki.mkdir(parents=True, mode=0o700)
    (story / '.qmd-topical-v2-project').write_text('scoped\n')
    (story / '.qmd-topical-v2-project').chmod(0o600)
    settings_sha = write(parent / '.auto-context/settings.json', b'{}\n', 0o644)
    teacher_sha = write(shared / 'synthetic-teacher-policy.json', b'{}\n')
    auto = {'schema':'qmd-topical-auto-refresh-v1', 'enabled': True,
            'sourceRoots':scope.SOURCES, 'engine':'codex',
            'compileCfg':{'extractor':{'builtins':['codex']},
                          'verify':{'builtins':['codex'],'crossEngine':'off'}},
            'maxEstimatedCents':20}
    auto_sha = write(story / scope.AUTO_POLICY, auto)
    policy = {'schema':scope.SCHEMA,'enabled':True,'storyRoot':str(story),
              'parentRoot':str(parent),'parentSettingsSha256':settings_sha,
              'syntheticTeacherPolicySha256':teacher_sha,
              'autoRefreshPolicySha256':auto_sha,'sourceRoots':scope.SOURCES,
              'wikiCollection':scope.WIKI_COLLECTION,'engine':'codex',
              'transport':'codex-cli-subscription'}
    write(story / scope.POLICY, policy)
    import context_learning.synthetic_scope as synthetic_scope
    import context_learning.synthetic_experiment as experiment
    with patch.object(synthetic_scope, 'NOVEL_ROOT', parent), \
         patch.object(synthetic_scope, 'policy_for', return_value=object()), \
         patch.object(experiment, 'story_for', return_value=story), \
         patch.object(experiment, 'shared_for', return_value=shared):
        assert scope.read(story)['collection'] == scope.WIKI_COLLECTION
        payload = story / 'payload.json'
        payload.write_text(json.dumps({'cwd':str(story)}))
        with patch.object(setup_guard, 'status', return_value={
                'status':'ready','project':str(parent)}), contextlib.redirect_stdout(io.StringIO()) as out:
            assert setup_guard.main(['hook-root','topical-stop',str(payload),str(parent)]) == 0
        assert out.getvalue().strip() == str(story)
        source = story/'01_Settings/probe.md'
        source.parent.mkdir(exist_ok=True)
        source.write_text('Only this synthetic story is in scope.\n')
        invoked = []
        def forbidden_runner(*_args):
            invoked.append(True)
            raise AssertionError('provider_called_with_foreign_context')
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE':'1'}):
            try:
                backend.run_generation_backend(story,['01_Settings/probe.md'],
                    auto['compileCfg'],'codex',allow_backend_execution=True,
                    runner=forbidden_runner,
                    existing_wiki_candidates=[{'path':'../../private.md','lead':'private'}])
            except ValueError as exc:
                assert str(exc) == 'topical_story_candidate_unscoped'
            else: raise AssertionError('foreign_journal_context_accepted')
        assert not invoked
        missing_index = parent/'missing-index.sqlite'
        with patch.object(qmd_route,'project_paths',return_value={
                'selected':True,'INDEX_PATH':str(missing_index)}):
            try:
                scope.validate_wiki_candidates(story,[{
                    'path':'topical-v2/'+'a'*24+'/valid-card.md','lead':'story only'}])
            except ValueError as exc:
                assert str(exc) == 'unsafe_qmd_index'
            else: raise AssertionError('missing_index_accepted')
        assert not missing_index.exists()
        payload.write_text(json.dumps({'cwd':str(parent)}))
        with patch.object(setup_guard, 'status', return_value={
                'status':'ready','project':str(parent)}), contextlib.redirect_stdout(io.StringIO()) as out:
            assert setup_guard.main(['hook-root','topical-stop',str(payload),str(parent)]) == 0
        assert out.getvalue().strip() == str(story)
        with patch.object(setup_guard, 'status', return_value={
                'status':'ready','project':str(parent)}), contextlib.redirect_stdout(io.StringIO()) as out:
            assert setup_guard.main(['hook-root','topical-reconcile',str(payload),str(parent)]) == 0
        assert out.getvalue().strip() == str(story)
        nested = story/'01_Settings'; nested.mkdir(exist_ok=True)
        payload.write_text(json.dumps({'cwd':str(nested)}))
        with patch.object(setup_guard, 'status', return_value={
                'status':'ready','project':str(parent)}), contextlib.redirect_stdout(io.StringIO()) as out:
            assert setup_guard.main(['hook-root','topical-stop',str(payload),str(parent)]) == 0
        assert out.getvalue().strip() == str(story)
        payload.write_text(json.dumps({'cwd':str(story)}))
        assert publisher._project(story) == (story,wiki,scope.WIKI_COLLECTION)
        with patch.dict(os.environ, {'OPENAI_API_KEY':'forbidden-fixture-key',
                                      'QMD_EXTRACTOR_CODEX_BIN':'/bin/false',
                                      'CODEX_HOME':'/tmp/forbidden-auth'}):
            child = scope.provider_env(story)
            assert all(key not in child for key in scope.BILLED_ENV)
        route = {'selected':True,'INDEX_PATH':str(parent/'index.sqlite'),
                 'QMD_CONFIG_DIR':str(parent/'qmd-config'),
                 'XDG_CACHE_HOME':str(parent/'qmd-cache')}
        with patch.object(publisher.qmd_route,'project_paths',return_value=route) as paths, \
             patch.object(publisher.qmd_route,'binary_info',return_value={'QMD_BIN':'/bin/true'}):
            assert publisher._qmd_runtime(story)['INDEX_PATH'] == parent/'index.sqlite'
            assert paths.call_args.args[0] == parent
        sibling = parent / 'other-story'; sibling.mkdir(mode=0o700)
        assert not scope.exact_story(sibling)
        with patch.dict(os.environ, {'QMD_TOPICAL_PROJECT_ROOT':str(sibling)}), \
             patch.object(setup_guard, 'status', return_value={
                 'status':'ready','project':str(parent)}), contextlib.redirect_stdout(io.StringIO()) as out:
            assert setup_guard.main(['hook-root','topical-stop',str(payload),str(parent)]) == 3
        settings = parent/'.auto-context/settings.json'
        settings.write_text('{"changed":true}\n')
        try: scope.read(story)
        except ValueError as exc: assert str(exc) == 'topical_story_policy_pin_changed'
        else: raise AssertionError('parent_setting_change_was_accepted')
        settings.write_bytes(b'{}\n')
        current = story/scope.POLICY
        current.chmod(0o644)
        try: scope.read(story)
        except ValueError as exc: assert str(exc) == 'topical_story_policy_missing_or_unsafe'
        else: raise AssertionError('world_readable_policy_was_accepted')

print(json.dumps({'exactStory':True,'parentRoute':True,'siblingDenied':True,
                  'settingsPin':True,'privatePolicy':True}))
