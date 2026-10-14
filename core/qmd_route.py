"""Single managed QMD executable and plugin-wide database routing contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import qmd_runtime
import qmd_shared_store
import runtime_update

PATH_KEYS = ('INDEX_PATH', 'QMD_CONFIG_DIR', 'XDG_CACHE_HOME')


def _absolute_executable(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('qmd_binary_missing')
    return path


def binary_info(environ=None):
    """Resolve the command and the actual Node entry from one runtime proof."""
    env = environ if environ is not None else os.environ
    explicit = env.get('QMD_BIN')
    active = qmd_runtime.managed_root(env.get('HOME')) / 'active.json'
    managed = None
    if active.is_file() or active.is_symlink():
        managed = qmd_runtime.select(qmd_runtime.managed_root(env.get('HOME')))
    if env.get('QMD_SETUP_GUARD_FIXTURE') != '1':
        # An external qmd command with the same API must not become the
        # executable for the plugin's dedicated database.
        if managed is None or (explicit and Path(explicit) != Path(managed['wrapper'])):
            raise ValueError('managed_qmd_not_selected')
    if managed and (not explicit or Path(explicit) == Path(managed['wrapper'])):
        proof = managed['proof']
        binary = _absolute_executable(managed['wrapper'])
        package_bin = _absolute_executable(proof['qmd'])
        node = _absolute_executable(proof['node'])
        override = env.get('QMD_NODE_BIN')
        if override and Path(override).resolve() != node.resolve():
            raise ValueError('managed_qmd_node_conflict')
        entry = Path(managed['entry']) if managed.get('entry') else package_bin.parent.parent / 'dist/cli/qmd.js'
        if not entry.is_file() or entry.is_symlink():
            # bin/qmd is a JavaScript entry in the pinned package too.
            entry = package_bin
        return {'QMD_BIN': str(binary), 'QMD_ENTRY': str(entry),
                'QMD_NODE_BIN': str(node), 'QMD_PACKAGE_BIN': str(package_bin),
                'managed': True}
    if explicit:
        binary = _absolute_executable(explicit)
    else:
        found = shutil.which('qmd', path=env.get('PATH'))
        if not found: raise ValueError('qmd_binary_missing')
        binary = _absolute_executable(Path(found).resolve())
    real = binary.resolve()
    entry = real.parent.parent / 'dist/cli/qmd.js'
    if not entry.is_file(): entry = real
    return {'QMD_BIN': str(binary), 'QMD_ENTRY': str(entry),
            'QMD_NODE_BIN': env.get('QMD_NODE_BIN') or '',
            'QMD_PACKAGE_BIN': str(real), 'managed': False}


def project_paths(project_root, *, environ=None, isolated=False,
                  reject_conflicts=False, fixture_env=False):
    """Resolve the plugin-wide tuple, or explicit isolated test fixtures."""
    env = environ if environ is not None else os.environ
    root = Path(project_root).resolve()
    if not fixture_env and env.get('QMD_SETUP_GUARD_FIXTURE') != '1':
        selected = qmd_shared_store.select(env.get('HOME'))
        qmd_shared_store.check_project(root,selected,home=env.get('HOME'))
        if reject_conflicts:
            for key in PATH_KEYS:
                inherited = env.get(key)
                if inherited and Path(inherited).resolve() != Path(selected[key]).resolve():
                    raise ValueError('plugin_qmd_store_env_conflict')
        return selected
    # Old project pointers and explicit paths are accepted only by isolated
    # tests. They never authorize a production fallback to the user's QMD DB.
    selected = runtime_update.select_runtime(root)
    if selected:
        if reject_conflicts:
            for key in PATH_KEYS:
                inherited = env.get(key)
                if inherited and Path(inherited).resolve() != Path(selected[key]).resolve():
                    raise ValueError('project_index_env_conflict')
        return {**{key: str(selected[key]) for key in PATH_KEYS},
                'selected': True, 'generation': selected['generation']}
    if isolated and not fixture_env:
        raise ValueError('isolated_qmd_runtime_required')
    if isolated:
        paths = {}
        for key in PATH_KEYS:
            value = env.get(key)
            if not value or not Path(value).is_absolute():
                raise ValueError('isolated_qmd_runtime_required')
            path = Path(value)
            if path.is_symlink() or root not in path.resolve().parents:
                raise ValueError('qmd_runtime_outside_project')
            paths[key] = str(path)
    else:
        cache = Path(env.get('XDG_CACHE_HOME') or Path.home() / '.cache')
        index = Path(env.get('INDEX_PATH') or cache / 'qmd/index.sqlite')
        config = Path(env.get('QMD_CONFIG_DIR') or
            Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'qmd')
        if any(not path.is_absolute() for path in (cache,index,config)):
            raise ValueError('invalid_qmd_runtime_path')
        paths = {'INDEX_PATH': str(index), 'QMD_CONFIG_DIR': str(config),
                 'XDG_CACHE_HOME': str(cache)}
    return {**paths, 'selected': False, 'generation': None}


if __name__ == '__main__':
    try:
        command = sys.argv[1]
        if command == 'resolve-bin' and len(sys.argv) == 2:
            print(binary_info()['QMD_BIN'])
        elif command == 'daemon-spec' and len(sys.argv) == 2:
            info = binary_info()
            values = (info['QMD_ENTRY'], info['QMD_NODE_BIN'], info['QMD_PACKAGE_BIN'])
            if any(any(c in value for c in ('\x1f','\t','\n','\r')) for value in values):
                raise ValueError('unsafe_qmd_runtime_path')
            print('\x1f'.join(values))
        elif command == 'resolve-env' and len(sys.argv) == 3:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
                selected = runtime_update.select_runtime(sys.argv[2])
            else:
                selected = project_paths(sys.argv[2], isolated=True,
                                         reject_conflicts=True)
            if selected:
                values = [selected[key] for key in PATH_KEYS]
                if any(any(c in value for c in ('\t','\n','\r')) for value in values):
                    raise ValueError('unsafe_qmd_runtime_path')
                print('\t'.join(values))
        elif command == 'resolve-shared-env' and len(sys.argv) == 2:
            selected = qmd_shared_store.select(os.environ.get('HOME'))
            if selected:
                values = [selected[key] for key in PATH_KEYS]
                if any(any(c in value for c in ('\t','\n','\r')) for value in values):
                    raise ValueError('unsafe_qmd_runtime_path')
                print('\t'.join(values))
        elif command == 'register-config' and len(sys.argv) == 3:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
                import config as qmd_config
                found = qmd_config.find_project_config(sys.argv[2])
                if (found['projectRoot'] != str(Path(sys.argv[2]).resolve())
                        or found['config'].get('indexing') is not True):
                    raise ValueError('plugin_qmd_collection_scope_invalid')
                qmd_shared_store.register_config(found['projectRoot'],
                                                  found['config'], home=os.environ.get('HOME'))
        elif command == 'verify-prune' and len(sys.argv) == 4:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
                import config as qmd_config
                scope = Path(sys.argv[2]).resolve()
                found = qmd_config.find_project_config(str(scope))
                if found['projectRoot'] != str(scope):
                    raise ValueError('plugin_qmd_collection_scope_invalid')
                qmd_shared_store.verify_missing_collection(
                    scope, sys.argv[3], found['config'], home=os.environ.get('HOME'))
        elif command == 'retire-pruned' and len(sys.argv) == 3:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
                import config as qmd_config
                scope = Path(sys.argv[2]).resolve()
                found = qmd_config.find_project_config(str(scope))
                if found['projectRoot'] != str(scope):
                    raise ValueError('plugin_qmd_collection_scope_invalid')
                qmd_shared_store.retire_pruned(
                    scope, found['config'], home=os.environ.get('HOME'))
        elif command == 'verify-shared-enrollment' and len(sys.argv) == 2:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
                qmd_shared_store.assert_all_enrolled(home=os.environ.get('HOME'))
        elif command == 'verify-collection' and len(sys.argv) == 5:
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
                import config as qmd_config
                import resolve_paths
                scope = Path(sys.argv[2]).resolve()
                found = qmd_config.find_project_config(str(scope))
                if found['projectRoot'] != str(scope):
                    raise ValueError('plugin_qmd_collection_scope_invalid')
                owned = qmd_shared_store.owned_collections(
                    scope, found['config'], home=os.environ.get('HOME'))
                resolved = resolve_paths.resolve_paths(str(scope), json.dumps(found['config']))
                wanted = {e['name']: str((scope / e['path']).resolve())
                          for e in resolved.get('indexEntries', [])}
                if (sys.argv[3] not in owned
                        or wanted.get(sys.argv[3]) != str(Path(sys.argv[4]).resolve())):
                    raise ValueError('plugin_qmd_collection_scope_invalid')
        else:
            raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise SystemExit(1)
