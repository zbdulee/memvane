"""Project-effective MemVane selection, independent of unrelated host entries."""
from pathlib import Path
from . import host_inventory

SCHEMA = 'memvane-host-self-v1'
LEGACY_KEYS = frozenset((
    '.claude/settings.json', '.claude/settings.local.json',
    '.claude/plugins/known_marketplaces.json', '.claude/plugins/installed_plugins.json',
    '.codex/config.toml', '.codex/plugins/installed_plugins.json',
    '.codex/plugins/known_marketplaces.json', '.codex/plugins/marketplaces.json',
    '.codex/plugins/config.json', 'project/.claude/settings.json',
    'project/.claude/settings.local.json', 'project/.codex/config.toml'))


def _source(value):
    if not isinstance(value, dict) or not isinstance(value.get('source'), str):
        raise ValueError('project_scope_source_invalid')
    result = {key: value[key] for key in ('source', 'repo', 'url', 'path') if key in value}
    if any(not isinstance(item, str) or not item for item in result.values()):
        raise ValueError('project_scope_source_invalid')
    return result


def selection(project, *, host=None):
    from .selector_heal import running_adapter
    from .project_scope import _path
    home = Path.home().resolve()
    if host is None:
        adapter = running_adapter()
        try:
            first = adapter.relative_to(home).parts[0]
        except (ValueError, IndexError) as exc:
            raise ValueError('project_scope_installation_invalid') from exc
        if first not in ('.claude', '.codex'):
            # Registered directory sources execute on Claude; their actual
            # root is still proved by the CLI and its directory/cache check.
            host = 'claude'
        else:
            host = first[1:]
    if host not in ('claude', 'codex'):
        raise ValueError('project_scope_installation_invalid')
    project = _path(project, directory=True)
    resolver = (host_inventory._project_claude_selection if host == 'claude'
                else host_inventory._project_codex_selection)
    root, marketplace, item, origin = resolver(home, project)
    root = _path(root, directory=True)
    if host == 'claude':
        source = _source(origin.get('source'))
        location = origin.get('installLocation')
        marketplace_source = None
    else:
        source = _source(item.get('source'))
        location = None
        if origin is not None and not isinstance(origin, dict):
            raise ValueError('project_scope_source_invalid')
        marketplace_source = ({key: origin[key] for key in ('sourceType', 'source') if key in origin}
                              if origin else None)
    if location is not None and (not isinstance(location, str) or not Path(location).is_absolute()):
        raise ValueError('project_scope_source_invalid')
    scope = item.get('scope', 'user')
    recorded = item.get('projectPath')
    if scope not in ('user', 'project', 'local') or (recorded is not None
            and not host_inventory._same_directory(recorded, project)):
        raise ValueError('project_scope_installation_invalid')
    projection = {'schema': SCHEMA, 'host': host, 'pluginId': 'memvane@'+marketplace,
                  'scope': scope, 'projectPath': str(project) if recorded is not None else None,
                  'marketplace': {'id': marketplace, 'source': source,
                                  'installLocation': location, 'marketplaceSource': marketplace_source}}
    return [{'host': host, 'root': str(root)}], projection


def is_legacy(value):
    return isinstance(value, dict) and set(value) == LEGACY_KEYS
