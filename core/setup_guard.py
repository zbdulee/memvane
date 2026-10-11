#!/usr/bin/env python3
"""Read-only compatibility gate for a plugin code update before explicit setup.

This does not disable a host plugin or change its trust settings. It prevents
new hook/worker code from touching an existing project's old storage layout.
Only an explicit, proved setup activation can select the project v2 pointer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import config as qmd_config

MAX_HOOK_BYTES = 2 * 1024 * 1024


def status(cwd):
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        return {'status': 'ready', 'reason': 'synthetic_fixture'}
    found = qmd_config.find_project_config(str(cwd))
    root = Path(found.get('projectRoot') or cwd).resolve()
    fmt = found.get('configFormat')
    if fmt == 'local-optout':
        return {'status': 'optout', 'reason': 'local_optout', 'project': str(root)}
    # config.find_project_config deliberately treats unreadable JSON as absent
    # for ordinary hooks. During an upgrade that fallback must not authorize a
    # new writer, including when a valid parent config would otherwise win.
    nearest = None
    for directory in qmd_config._project_search_dirs(cwd):
        for candidate, _, _ in qmd_config._candidate_configs(directory):
            if candidate.exists() or candidate.is_symlink():
                nearest = candidate
                break
        if nearest is not None: break
    if nearest is not None and (nearest.is_symlink() or str(nearest) != found.get('configPath')):
        return {'status': 'setup_required', 'reason': 'unreadable_project_config', 'project': str(nearest.parent)}
    auto = root / '.auto-context'
    settings = auto / 'settings.json'
    pointer = auto / 'qmd-index-active.json'
    journal = auto / 'install-update-journal.json'
    if auto.is_symlink() or any(path.is_symlink() for path in (settings, pointer, journal)):
        return {'status': 'setup_required', 'reason': 'unsafe_setup_state', 'project': str(root)}
    if fmt in ('auto-context-json', 'agents-legacy'):
        return {'status': 'setup_required', 'reason': 'legacy_config', 'project': str(root)}
    if fmt == 'auto-context-dir':
        if pointer.exists():
            try:
                if pointer.stat().st_size > 65536 or json.loads(pointer.read_text()).get('schema') != 'qmd-index-pointer-v1':
                    raise ValueError('invalid_pointer')
            except (OSError, ValueError, AttributeError):
                return {'status': 'setup_required', 'reason': 'invalid_project_pointer', 'project': str(root)}
        try:
            import qmd_route
            qmd_route.project_paths(root, isolated=True, reject_conflicts=True)
        except (OSError, ValueError, KeyError, TypeError):
            return {'status': 'setup_required', 'reason': 'plugin_qmd_store_not_selected', 'project': str(root)}
    elif settings.exists():
        return {'status': 'setup_required', 'reason': 'invalid_modern_settings', 'project': str(root)}
    if journal.is_file():
        try:
            value = json.loads(journal.read_text()) if journal.stat().st_size <= 65536 else None
            if (not isinstance(value, dict) or value.get('schema') != 'qmd-install-update-v1'
                    or value.get('phase') not in ('activated', 'rolled_back')):
                return {'status': 'setup_required', 'reason': 'setup_transition_pending', 'project': str(root)}
        except (OSError, ValueError):
            return {'status': 'setup_required', 'reason': 'invalid_setup_journal', 'project': str(root)}
    return {'status': 'ready', 'project': str(root)}


def hook_project(action, payload_path, fallback):
    if action.startswith('topical-'):
        explicit = os.environ.get('QMD_TOPICAL_PROJECT_ROOT') or os.environ.get('QMD_TOPICAL_SANDBOX_ROOT')
        if explicit: return explicit
    return hook_payload_cwd(payload_path, fallback)


def hook_payload_cwd(payload_path, fallback):
    raw = Path(payload_path)
    if raw.stat().st_size > MAX_HOOK_BYTES: return fallback
    try:
        payload = json.loads(raw.read_text())
        cwd = payload.get('cwd') if isinstance(payload, dict) else None
        return cwd if isinstance(cwd, str) and Path(cwd).is_absolute() else fallback
    except (OSError, ValueError):
        return fallback


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == 'check':
        state = status(args[1])
    elif len(args) == 4 and args[0] == 'hook':
        state = status(hook_project(args[1], args[2], args[3]))
    elif len(args) == 4 and args[0] == 'hook-root':
        requested = Path(hook_project(args[1], args[2], args[3])).absolute()
        state = status(requested)
        if state['status'] != 'ready': return 3
        import wiki_topical
        import topical_story_scope
        actual = Path(hook_payload_cwd(args[2], args[3])).absolute()
        story = topical_story_scope.story_for_cwd(actual)
        if story is None and args[1] in ('topical-stop', 'topical-reconcile'):
            # A host session can be rooted at the shared novel parent while it
            # edits this story. The worker still scans only the pinned story.
            from context_learning.synthetic_scope import NOVEL_ROOT
            from context_learning.synthetic_experiment import story_for
            if actual == NOVEL_ROOT.absolute() and requested == actual:
                parent = NOVEL_ROOT.resolve(strict=True)
                story = story_for(parent).resolve(strict=True)
        if story is not None:
            if requested != story and requested != story.parent and not topical_story_scope.story_for_cwd(requested):
                return 3
            topical_story_scope.read(story)
            print(story)
            return 0
        root = Path(state.get('project') or requested).resolve()
        if not wiki_topical.opted_in(root): return 3
        print(root)
        return 0
    else:
        return 2
    return 0 if state['status'] == 'ready' else 3


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3)
