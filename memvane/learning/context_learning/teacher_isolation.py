"""Teacher isolation contract: argv, env and working-folder checks before launch.

``assert_isolated`` must pass before a teacher CLI starts. It looks only at the
argv, the child environment, the working folder and the executable; it never
starts a process. The required arguments are literal constants here, checked
independently of what the adapters build, so a flag dropped from an adapter
is caught. There is no argument or environment variable that skips a check.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

REASON = 'teacher_isolation_incomplete'

CLAUDE_BUILTIN_PLUGINS = ('cc-plugin-agents-md@builtin', 'cc-plugin-plugin-authoring@builtin',
                          'cc-plugin-telemetry@builtin')
CLAUDE_SETTINGS = ('{"disableAllHooks":true,"enabledPlugins":{'
                   '"cc-plugin-agents-md@builtin":false,'
                   '"cc-plugin-plugin-authoring@builtin":false,'
                   '"cc-plugin-telemetry@builtin":false}}')
# flag -> exact value (None: a flag without a value). '--model' and
# '--system-prompt' are compared with the caller's expected values.
CLAUDE_REQUIRED = {
    '-p': None, '--model': None, '--effort': 'low', '--safe-mode': None,
    '--tools': '', '--disallowedTools': '*', '--disable-slash-commands': None,
    '--strict-mcp-config': None, '--mcp-config': '{"mcpServers":{}}',
    '--setting-sources': '', '--settings': CLAUDE_SETTINGS, '--system-prompt': None,
    '--no-session-persistence': None, '--no-chrome': None, '--permission-prompts': 'none',
    '--output-format': 'stream-json', '--input-format': 'stream-json', '--verbose': None,
    '--include-hook-events': None, '--max-turns': '1',
}
CLAUDE_VALUE_FLAGS = frozenset({'--model', '--effort', '--tools', '--disallowedTools',
                                '--mcp-config', '--setting-sources', '--settings',
                                '--system-prompt', '--permission-prompts', '--output-format',
                                '--input-format', '--max-turns'})
CLAUDE_FORBIDDEN = ('--add-dir', '--plugin-dir', '--agents', '--append-system-prompt',
                    '--chrome', '--resume', '--continue', '--bare', '-r', '-c')

CODEX_OVERRIDES = (
    'model_reasoning_effort="low"', 'approval_policy="never"', 'project_doc_max_bytes=0',
    'web_search="disabled"', 'features.plugins=false', 'features.apps=false',
    'features.browser_use=false', 'features.in_app_browser=false',
    'features.computer_use=false', 'features.shell_tool=false', 'features.multi_agent=false',
    'features.multi_agent_v2=false', 'features.view_image=false',
    'features.skip_host_skill_discovery=true', 'features.memories=false',
    'features.goals=false', 'model_provider="openai"',
    'suppress_unstable_features_warning=true', 'skills.include_instructions=false',
)
CODEX_REQUIRED = {
    '--ignore-user-config': None, '--ignore-rules': None, '--ephemeral': None,
    '--skip-git-repo-check': None, '--sandbox': 'read-only', '--json': None,
    '--color': 'never', '--model': None,
}
CODEX_VALUE_FLAGS = frozenset({'--sandbox', '--color', '--model', '-c'})
CODEX_FORBIDDEN = ('--add-dir', '-C', '--cd', '--trust', '--profile', '-p', '--oss',
                   '--image', '-i', '--worktree', '--enable', '--full-auto', '--yolo')

ENV_BASE = frozenset({'HOME', 'PATH', 'TMPDIR', 'LANG', 'USER', 'LOGNAME', 'QMD_SANDBOX'})
CLAUDE_ENV_FIXED = {'CLAUDE_CODE_EFFORT_LEVEL': 'low', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '1200',
                    'CLAUDE_CODE_MAX_RETRIES': '0', 'CLAUDE_CODE_AUTO_CONNECT_IDE': '0'}
FOLDER_MARKERS = ('.git', 'CLAUDE.md', 'AGENTS.md', '.claude', '.codex', '.mcp.json')


class IsolationRefused(ValueError):
    """``str(exc)`` is the host reason; ``detail`` names the failed check."""

    def __init__(self, detail):
        super().__init__(REASON)
        self.detail = detail


def _forbidden(token, forbidden):
    return token in forbidden or token.startswith('--dangerously')


def _pairs(tokens, value_flags, forbidden):
    pairs = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(token, str):
            raise IsolationRefused('argv_invalid')
        if _forbidden(token, forbidden) or _forbidden(token.split('=', 1)[0], forbidden):
            raise IsolationRefused('argv_forbidden:' + token.split('=', 1)[0][:40])
        if token in value_flags:
            if index + 1 >= len(tokens) or not isinstance(tokens[index + 1], str):
                raise IsolationRefused('argv_missing_value:' + token)
            pairs.append((token, tokens[index + 1]))
            index += 2
            continue
        pairs.append((token, None))
        index += 1
    return pairs


def _check_required(pairs, required, expected):
    seen = {}
    for flag, value in pairs:
        if flag not in required:
            if flag == '-c':
                continue
            raise IsolationRefused('argv_unexpected:' + flag[:40])
        if flag in seen:
            raise IsolationRefused('argv_repeated:' + flag)
        seen[flag] = value
    for flag, value in required.items():
        if flag not in seen:
            raise IsolationRefused('argv_missing:' + flag)
        want = expected.get(flag, value)
        if seen[flag] != want:
            raise IsolationRefused('argv_value:' + flag)


def check_argv(host, argv, *, model, system_prompt=None):
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        raise IsolationRefused('argv_invalid')
    if host == 'claude':
        if not isinstance(system_prompt, str) or not system_prompt:
            raise IsolationRefused('argv_system_prompt_unknown')
        pairs = _pairs(list(argv[1:]), CLAUDE_VALUE_FLAGS, CLAUDE_FORBIDDEN)
        _check_required(pairs, CLAUDE_REQUIRED,
                        {'--model': model, '--system-prompt': system_prompt})
        return
    if host != 'codex':
        raise IsolationRefused('host_unknown')
    tokens = list(argv[1:])
    if tokens[0] != 'exec':
        raise IsolationRefused('argv_missing:exec')
    if tokens[-1] != '-':
        raise IsolationRefused('argv_missing:-')
    pairs = _pairs(tokens[1:-1], CODEX_VALUE_FLAGS, CODEX_FORBIDDEN)
    if any(flag in ('exec', '-') for flag, _ in pairs):
        raise IsolationRefused('argv_unexpected:positional')
    _check_required(pairs, CODEX_REQUIRED, {'--model': model})
    overrides = [value for flag, value in pairs if flag == '-c']
    if len(overrides) != len(set(overrides)):
        raise IsolationRefused('argv_repeated:-c')
    for override in CODEX_OVERRIDES:
        if override not in overrides:
            raise IsolationRefused('argv_missing:-c ' + override.split('=', 1)[0])
    if len(overrides) != len(CODEX_OVERRIDES):
        raise IsolationRefused('argv_unexpected:-c')


def check_env(host, env, *, parent=None):
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                        for k, v in env.items()):
        raise IsolationRefused('env_invalid')
    allowed = set(ENV_BASE)
    if host == 'codex':
        allowed.add('CODEX_HOME')
    elif host == 'claude':
        allowed.update(CLAUDE_ENV_FIXED)
        for key, value in CLAUDE_ENV_FIXED.items():
            if env.get(key) != value:
                raise IsolationRefused('env_value:' + key)
    extra = sorted(set(env) - allowed)
    if extra:
        raise IsolationRefused('env_unexpected:' + extra[0][:40])
    if env.get('QMD_SANDBOX') != '1':
        raise IsolationRefused('env_value:QMD_SANDBOX')
    for key in ('HOME', 'PATH'):
        if not env.get(key):
            raise IsolationRefused('env_missing:' + key)
    if parent is not None:
        # Authentication continues through the user's own HOME/CODEX_HOME.
        for key in ('HOME', 'CODEX_HOME') if host == 'codex' else ('HOME',):
            if env.get(key) != parent.get(key):
                raise IsolationRefused('env_value:' + key)


def check_cwd(cwd):
    path = Path(cwd)
    try:
        info = os.lstat(path)
        entries = os.listdir(path)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IsolationRefused('cwd_unavailable') from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise IsolationRefused('cwd_unsafe')
    if entries:
        raise IsolationRefused('cwd_not_empty')
    for folder in {*([path, *path.parents] if path.is_absolute() else []),
                   resolved, *resolved.parents}:
        for marker in FOLDER_MARKERS:
            if os.path.lexists(folder / marker):
                raise IsolationRefused('cwd_marker:' + marker)


def check_executable(path, *, project=None):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise IsolationRefused('executable_unsafe')
    try:
        resolved = Path(path).resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise IsolationRefused('executable_unavailable') from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022):
        raise IsolationRefused('executable_unsafe')
    if project is not None:
        root = Path(project).resolve()
        if resolved == root or root in resolved.parents:
            raise IsolationRefused('executable_in_project')


def assert_isolated(host, argv, env, cwd, *, model, system_prompt=None, parent=None,
                    project=None):
    """Raise IsolationRefused unless every launch condition holds; start nothing."""
    check_argv(host, argv, model=model, system_prompt=system_prompt)
    check_executable(argv[0], project=project)
    check_env(host, env, parent=parent)
    check_cwd(cwd)
