#!/usr/bin/env python3
"""Disposable synthetic Git fixture for docs/release-agent-qa.md.

Creates, mutates and removes files only below one caller-provided new empty
directory under /tmp or the current workspace. It never invokes QMD, a host
agent, a model, an API, a plugin hook, an installer or the network, never runs
QA itself, and never writes global/system Git configuration or hooks. Every Git
call uses an empty fixture-local hooks directory and fixture-local config.

  python3 -B test/fixtures/release-qa-fixture.py create  <new-empty-dir>
  python3 -B test/fixtures/release-qa-fixture.py show    <fixture-dir>
  python3 -B test/fixtures/release-qa-fixture.py stage   <fixture-dir> <action>
  python3 -B test/fixtures/release-qa-fixture.py cleanup <fixture-dir>

Stage actions simulate user-side work only: upstream-change, pull,
switch-feature, switch-main, worktree-add, dirty, deny-read, restore-read,
wiki-source-edit. Evidence must live outside the fixture; cleanup deletes the
whole fixture directory after verifying its ownership marker.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import time

SCHEMA = 'qmd-release-qa-fixture-v1'
MARKER = '.qa-fixture-owner'
MANIFEST = 'MANIFEST.json'
NO_HOOKS = '.qa-no-hooks'
GIT_CONFIG = '.qa-gitconfig'
GIT_TIMEOUT = 30
MIN_GIT = (2, 32)  # GIT_CONFIG_GLOBAL
CONFIG_MARKERS = ('.auto-context/settings.json', '.auto-context.json',
                  '.agents/qmd-recall.json')
DENIED_HOME_DIRS = ('.claude', '.codex', '.hermes', '.config/qmd',
                    '.cache/qmd', 'Library')
TOKEN_NAMES = ('ALPHA_MAIN', 'ALPHA_FEATURE', 'ALPHA_WORKTREE', 'BETA_V1', 'BETA_V2',
               'GAMMA_DELETE_ME', 'DELTA_ADDED', 'FEATURE_ONLY',
               'WORKTREE_ONLY', 'DIRTY_TRACKED', 'UNTRACKED_NEW',
               'WIKI_FACT_R1', 'WIKI_FACT_R2', 'AGENT_NOTE')
ACTIONS = ('upstream-change', 'pull', 'switch-feature', 'switch-main',
           'worktree-add', 'dirty', 'deny-read', 'restore-read',
           'wiki-source-edit')


class FixtureError(Exception):
    pass


def fail(reason, **detail):
    raise FixtureError(json.dumps({'ok': False, 'reason': reason, **detail}))


def within(child, root):
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return child != root


def allowed_roots():
    roots = [Path('/tmp').resolve()]
    workspace, home = Path.cwd().resolve(), Path.home().resolve()
    if workspace != Path('/') and workspace != home and not within(home, workspace):
        roots.append(workspace)
    return roots


def check_location(target):
    if not any(within(target, root) for root in allowed_roots()):
        fail('outside_allowed_roots', target=str(target),
             allowed=[str(root) for root in allowed_roots()])
    if any(part in ('.git', '.agents', '.codex', '.claude', '.auto-context')
           for part in target.parts):
        fail('inside_repository_or_host_state', target=str(target))
    home = Path.home().resolve()
    for name in DENIED_HOME_DIRS:
        denied = home / name
        if target == denied or within(target, denied):
            fail('inside_host_or_runtime_state', target=str(target))
    for ancestor in [target.parent, *target.parent.parents]:
        for marker in CONFIG_MARKERS:
            if (ancestor / marker).exists():
                # Project discovery walks upward; an inherited config would
                # contaminate fresh-install and opt-in scenarios.
                fail('ancestor_has_auto_context_config',
                     path=str(ancestor / marker))


def new_target(raw):
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts or not path.name:
        fail('target_must_be_absolute_new_directory', target=raw)
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        fail('parent_missing', target=raw)
    target = parent / path.name
    check_location(target)
    if target.is_symlink():
        fail('target_is_symlink', target=str(target))
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            fail('target_not_empty_directory', target=str(target))
        if target.stat().st_uid != os.getuid():
            fail('target_not_owned', target=str(target))
        target.chmod(0o700)
        return target, False
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        fail('target_appeared_concurrently', target=str(target))
    return target, True


def existing_fixture(raw):
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        fail('fixture_path_invalid', target=raw)
    root = path.resolve(strict=True)
    check_location(root)
    for name in (MANIFEST, MARKER):
        entry = root / name
        if (entry.is_symlink() or not entry.is_file()
                or entry.stat().st_uid != os.getuid()
                or entry.stat().st_nlink != 1):
            fail('unsafe_fixture_marker', path=str(entry))
    try:
        manifest = json.loads((root / MANIFEST).read_text())
        owner = json.loads((root / MARKER).read_text())
    except (OSError, ValueError):
        fail('not_a_release_qa_fixture', target=str(root))
    if (manifest.get('schema') != SCHEMA or manifest.get('root') != str(root)
            or owner.get('fixtureId') != manifest.get('fixtureId')
            or owner.get('uid') != os.getuid()):
        fail('fixture_marker_mismatch', target=str(root))
    for name in ('seed', 'project', 'upstream.git'):
        entry = root / name
        if entry.is_symlink() or not entry.is_dir() or not within(entry.resolve(), root):
            fail('fixture_directory_changed', path=str(entry))
    return root, manifest


def inside(root, relative):
    target = (root / relative)
    resolved_parent = target.parent.resolve()
    if not (resolved_parent == root or within(resolved_parent, root)):
        fail('write_escapes_fixture', path=str(target))
    if target.is_symlink():
        fail('write_target_is_symlink', path=str(target))
    if target.exists() and (not target.is_file() or target.stat().st_uid != os.getuid()
                            or target.stat().st_nlink != 1):
        fail('write_target_unsafe', path=str(target))
    return target


def write(root, relative, text):
    target = inside(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def git_env(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': str(root / GIT_CONFIG),
                'GIT_TERMINAL_PROMPT': '0', 'GIT_ALLOW_PROTOCOL': 'file',
                'LC_ALL': 'C'})
    return env


def git(root, cwd, *args):
    command = ['git', '-c', 'core.hooksPath=' + str(root / NO_HOOKS),
               '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false',
               '-c', 'tag.gpgsign=false', '-c', 'protocol.allow=never',
               '-c', 'protocol.file.allow=always',
               '-c', 'user.name=Release QA Fixture',
               '-c', 'user.email=release-qa@example.invalid', *args]
    try:
        done = subprocess.run(command, cwd=str(cwd), env=git_env(root),
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        fail('git_timeout', args=list(args), seconds=GIT_TIMEOUT)
    if done.returncode != 0:
        fail('git_failed', args=list(args), stderr=done.stderr[-2000:])
    return done.stdout.strip()


def check_git_version():
    try:
        out = subprocess.run(['git', '--version'], capture_output=True,
                             text=True, timeout=GIT_TIMEOUT,
                             stdin=subprocess.DEVNULL).stdout
        version = tuple(int(p) for p in out.split()[2].split('.')[:2])
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        fail('git_unavailable')
    if version < MIN_GIT:
        fail('git_too_old', found=out.strip(), minimum='2.32')


def pin_local_config(root, repo):
    # Local config outlives this helper, so an agent's own git commands in the
    # fixture also skip any global hooks of the QA account.
    for key, value in (('core.hooksPath', str(root / NO_HOOKS)),
                       ('core.fsmonitor', 'false'),
                       ('commit.gpgsign', 'false'),
                       ('user.name', 'Release QA Fixture'),
                       ('user.email', 'release-qa@example.invalid')):
        git(root, repo, 'config', '--local', key, value)


def doc(title, sentence, token):
    return ('# %s\n\nSynthetic release QA document. %s %s.\n'
            % (title, sentence, token))


def commit_all(root, repo, message):
    git(root, repo, 'add', '-A')
    git(root, repo, 'commit', '-q', '-m', message)


def load_manifest(root):
    return json.loads((root / MANIFEST).read_text())


def save_manifest(root, manifest):
    temporary = inside(root, MANIFEST + '.tmp')
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    os.replace(str(temporary), str(root / MANIFEST))


def create(raw):
    check_git_version()
    root, created = new_target(raw)
    try:
        return build(root)
    except BaseException:
        # Only this call wrote below root, so a partial fixture is discarded.
        for child in list(root.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(str(child))
            else:
                child.unlink()
        if created:
            root.rmdir()
        raise


def build(root):
    rid = secrets.token_hex(4)
    tokens = {name: 'qz%s%s' % (rid, name.lower().replace('_', ''))
              for name in TOKEN_NAMES}
    (root / NO_HOOKS).mkdir(mode=0o700)
    (root / 'templates').mkdir(mode=0o700)
    write(root, GIT_CONFIG, '')
    write(root, MARKER, json.dumps({'fixtureId': rid, 'uid': os.getuid()}))
    template = '--template=' + str(root / 'templates')
    upstream, seed, project = root / 'upstream.git', root / 'seed', root / 'project'
    git(root, root, 'init', '-q', '--bare', '--initial-branch=main', template,
        str(upstream))
    git(root, root, 'init', '-q', '--initial-branch=main', template, str(seed))
    pin_local_config(root, seed)
    write(seed, 'README.md', '# Release QA fixture\n\nSynthetic, disposable.\n')
    write(seed, 'docs/qa/alpha.md', doc('Alpha', 'The alpha codename on main is',
                                         tokens['ALPHA_MAIN']))
    write(seed, 'docs/qa/beta.md', doc('Beta', 'The beta revision marker is',
                                        tokens['BETA_V1']))
    write(seed, 'docs/qa/gamma.md', doc('Gamma', 'The gamma marker to be deleted is',
                                         tokens['GAMMA_DELETE_ME']))
    write(seed, 'docs/qa/wiki-source.md',
          doc('Wiki source', 'The wiki source fact for revision one is',
              tokens['WIKI_FACT_R1']))
    commit_all(root, seed, 'qa: base')
    git(root, seed, 'remote', 'add', 'origin', str(upstream))
    git(root, seed, 'switch', '-q', '-c', 'qa/feature')
    write(seed, 'docs/qa/alpha.md', doc('Alpha', 'The alpha codename on feature is',
                                         tokens['ALPHA_FEATURE']))
    write(seed, 'docs/qa/feature-only.md',
          doc('Feature only', 'The feature-only marker is', tokens['FEATURE_ONLY']))
    commit_all(root, seed, 'qa: feature branch')
    git(root, seed, 'switch', '-q', '-c', 'qa/worktree', 'main')
    write(seed, 'docs/qa/alpha.md', doc('Alpha', 'The alpha codename in the worktree is',
                                         tokens['ALPHA_WORKTREE']))
    write(seed, 'docs/qa/worktree-only.md',
          doc('Worktree only', 'The worktree-only marker is', tokens['WORKTREE_ONLY']))
    commit_all(root, seed, 'qa: worktree branch')
    git(root, seed, 'switch', '-q', 'main')
    git(root, seed, 'push', '-q', 'origin', 'main', 'qa/feature', 'qa/worktree')
    git(root, root, 'clone', '-q', template, str(upstream), str(project))
    pin_local_config(root, project)
    git(root, project, 'branch', '-q', '--track', 'qa/feature', 'origin/qa/feature')
    git(root, project, 'branch', '-q', '--track', 'qa/worktree', 'origin/qa/worktree')
    manifest = {'schema': SCHEMA, 'fixtureId': rid, 'root': str(root),
                'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'paths': {'project': str(project),
                          'worktree': str(root / 'wt-worktree'),
                          'upstream': str(upstream), 'seed': str(seed)},
                'tokens': tokens,
                'events': [{'action': 'create', 'at': time.time(),
                            'projectHead': git(root, project, 'rev-parse', 'HEAD')}]}
    save_manifest(root, manifest)
    return manifest


def stage(raw, action):
    if action not in ACTIONS:
        fail('unknown_action', action=action, allowed=list(ACTIONS))
    root, manifest = existing_fixture(raw)
    tokens = manifest['tokens']
    seed, project = root / 'seed', root / 'project'
    worktree = root / 'wt-worktree'
    if action == 'upstream-change':
        write(seed, 'docs/qa/delta.md', doc('Delta', 'The added delta marker is',
                                             tokens['DELTA_ADDED']))
        write(seed, 'docs/qa/beta.md', doc('Beta', 'The beta revision marker is',
                                            tokens['BETA_V2']))
        os.unlink(str(inside(seed, 'docs/qa/gamma.md')))
        commit_all(root, seed, 'qa: upstream add/update/delete')
        git(root, seed, 'push', '-q', 'origin', 'main')
    elif action == 'pull':
        git(root, project, 'pull', '-q', '--ff-only', 'origin', 'main')
    elif action == 'switch-feature':
        git(root, project, 'switch', '-q', 'qa/feature')
    elif action == 'switch-main':
        git(root, project, 'switch', '-q', 'main')
    elif action == 'worktree-add':
        if worktree.exists():
            fail('worktree_exists', path=str(worktree))
        git(root, project, 'worktree', 'add', '-q', str(worktree), 'qa/worktree')
    elif action == 'dirty':
        alpha = inside(project, 'docs/qa/alpha.md')
        if not alpha.is_file():
            fail('dirty_requires_alpha_checkout', path=str(alpha))
        with alpha.open('a') as handle:
            handle.write('\nUncommitted dirty marker %s.\n' % tokens['DIRTY_TRACKED'])
        write(project, 'docs/qa/untracked-note.md',
              doc('Untracked note', 'The untracked marker is', tokens['UNTRACKED_NEW']))
    elif action in ('deny-read', 'restore-read'):
        beta = inside(project, 'docs/qa/beta.md')
        beta.chmod(0 if action == 'deny-read' else 0o644)
    elif action == 'wiki-source-edit':
        write(project, 'docs/qa/wiki-source.md',
              doc('Wiki source', 'The wiki source fact for revision two is',
                  tokens['WIKI_FACT_R2']))
    manifest['events'].append({'action': action, 'at': time.time(),
                               'projectHead': git(root, project, 'rev-parse', 'HEAD'),
                               'projectBranch': git(root, project, 'branch',
                                                    '--show-current')})
    save_manifest(root, manifest)
    return manifest['events'][-1]


def cleanup(raw):
    root, manifest = existing_fixture(raw)
    for current, directories, files in os.walk(str(root), followlinks=False):
        for name in files + directories:
            path = Path(current) / name
            if not path.is_symlink() and not path.stat().st_mode & stat.S_IRUSR:
                path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(str(root))
    return {'ok': True, 'removed': str(root), 'fixtureId': manifest['fixtureId']}


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[1]
    try:
        if command == 'create' and len(argv) == 3:
            result = create(argv[2])
        elif command == 'show' and len(argv) == 3:
            result = existing_fixture(argv[2])[1]
        elif command == 'stage' and len(argv) == 4:
            result = stage(argv[2], argv[3])
        elif command == 'cleanup' and len(argv) == 3:
            result = cleanup(argv[2])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except FixtureError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
