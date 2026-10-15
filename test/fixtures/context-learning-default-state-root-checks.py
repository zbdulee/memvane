"""Real filesystem/config regression; no models, hooks, databases or providers."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import config
from context_learning.contracts import digest
from context_learning.seam import _private_state


def default_parent(home):
    if sys.platform == 'darwin':
        return home / 'Library/Application Support/qmd-auto-context/live-selection'
    return home / '.local/share/qmd-auto-context/live-selection'


def settings(parent=None):
    value = {'indexing': True, 'collections': ['story-wiki'],
             'collectionPaths': {'story-wiki': '.auto-context/wiki'},
             'collectionRoles': {'story-wiki': 'wiki'}, 'recallStrategy': 'wikiOnly',
             'contextLearning': {'liveSelection': True, 'capture': False, 'autoCycle': False}}
    if parent is not None:
        value['contextLearning']['stateRoot'] = str(parent)
    return value


with tempfile.TemporaryDirectory(prefix='qmd-default-state-') as temporary:
    base = Path(temporary).resolve()
    resolved = []
    for user in ('First User', 'Second User'):
        home = base / user
        project = home / 'work/Story With Spaces'
        nested = project / 'chapters'
        nested.mkdir(parents=True)
        parent = default_parent(home)
        parent.mkdir(mode=0o700, parents=True)
        state = parent / digest(str(project))
        state.mkdir(mode=0o700)
        preserved = {'active-checkpoint.json': b'{"opaque":"existing checkpoint"}\n',
                     'head.artifact': b'existing local artifact bytes',
                     'live-selector.json': b'{"opaque":"existing policy"}\n'}
        for name, data in preserved.items():
            (state / name).write_bytes(data)
        inode = state.stat().st_ino
        raw = settings()
        (project / '.auto-context').mkdir()
        (project / '.auto-context/settings.json').write_text(json.dumps(raw))
        with patch.dict(os.environ, {'HOME': str(home)}):
            normalized = config.normalize_config(raw)
            actual = _private_state(normalized, project, create=False)
            assert actual == state, ('omitted_root_must_reuse_existing_state', actual, state)
            assert _private_state(normalized, project) == state
            assert state.stat().st_ino == inode
            assert {name: (state / name).read_bytes() for name in preserved} == preserved
            explicit = config.normalize_config(settings(parent))
            assert _private_state(explicit, project, create=False) == state
            found = config.find_project_config(str(nested))
            assert found['projectRoot'] == str(project)
            assert found['config']['collections'] == ['story-wiki']
            assert found['config']['collectionPaths'] == raw['collectionPaths']
            assert found['config']['recallStrategy'] == 'wikiOnly'
            assert _private_state(found['config'], project, create=False) == state

            other = home / 'work/Other Story'
            other.mkdir()
            other_state = _private_state(normalized, other)
            assert other_state == parent / digest(str(other)) and other_state != state
            assert (other_state.stat().st_mode & 0o077) == 0

            custom = home / 'custom private state'
            custom.mkdir(mode=0o700)
            assert _private_state(config.normalize_config(settings(custom)), project) == custom / digest(str(project))
            for invalid in ('', 'relative-state', '~/private-state', None, 17):
                invalid_cfg = copy.deepcopy(raw)
                invalid_cfg['contextLearning']['stateRoot'] = invalid
                assert _private_state(config.normalize_config(invalid_cfg), project) is None
            for unsafe in (project, project / '.auto-context/wiki/private-state'):
                unsafe.mkdir(mode=0o700, parents=True, exist_ok=True)
                assert _private_state(config.normalize_config(settings(unsafe)), project) is None

            parent.chmod(0o755)
            assert _private_state(normalized, project, create=False) is None
            parent.chmod(0o700)
            link = home / 'linked-state'
            link.symlink_to(parent, target_is_directory=True)
            assert _private_state(config.normalize_config(settings(link)), project) is None
            resolved.append(actual)
        assert (project / '.auto-context/settings.json').read_text() == json.dumps(raw)
    assert resolved[0] != resolved[1]

    home = base / 'Unprepared User'
    project = home / 'work/Story'
    project.mkdir(parents=True)
    with patch.dict(os.environ, {'HOME': str(home)}):
        normalized = config.normalize_config(settings())
        assert _private_state(normalized, project, create=False) is None
        assert _private_state(normalized, project) is None
        assert not default_parent(home).exists()

print(json.dumps({'existingStateReused': True, 'differentHomes': True, 'spaces': True,
    'projectIsolation': True, 'descendantScope': True, 'explicitRootCompatible': True,
    'invalidExplicitDenied': True, 'unsafeRootsDenied': True,
    'missingPreparationUnchanged': True, 'externalCalls': 0}))
