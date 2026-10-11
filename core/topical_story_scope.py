"""Exact synthetic-story opt-in for topical authoring in the shared novel QMD store.

This module never creates an opt-in. Its private policy binds the existing
parent consent and settings, one story, one wiki collection and the approved
subscription adapter. Other topical projects keep their isolated path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

POLICY = '.qmd-topical-story-policy.json'
SCHEMA = 'qmd-topical-story-scope-v1'
SOURCES = ['01_Settings', '02_Characters', '03_Plot', '04_Manuscript']
SOURCE_COLLECTIONS = {
    '01_Settings': 'fantasy-20261008-settings',
    '02_Characters': 'fantasy-20261008-characters',
    '03_Plot': 'fantasy-20261008-plot',
    '04_Manuscript': 'fantasy-20261008-manuscript',
}
WIKI_COLLECTION = 'fantasy-20261008-wiki'
AUTO_POLICY = '.topical-auto-refresh.json'
BILLED_ENV = ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
              'OPENAI_API_BASE', 'OPENAI_API_HOST', 'OPENAI_ORG_ID',
              'OPENAI_PROJECT_ID', 'AZURE_OPENAI_API_KEY',
              'AZURE_OPENAI_ENDPOINT', 'QMD_EXTRACTOR_CODEX_BIN', 'CODEX_HOME')


def _private_bytes(path: Path, *, settings=False):
    if path.is_symlink() or not path.is_file():
        raise ValueError('topical_story_policy_missing_or_unsafe')
    info = path.stat()
    unsafe_bits = 0o022 if settings else 0o077
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & unsafe_bits or info.st_nlink != 1 or info.st_size > 65536):
        raise ValueError('topical_story_policy_missing_or_unsafe')
    return path.read_bytes()


def exact_story(root):
    """Recognize the one planned story without authorizing it."""
    from context_learning.synthetic_scope import NOVEL_ROOT
    from context_learning.synthetic_experiment import story_for
    try:
        parent = NOVEL_ROOT.resolve(strict=True)
        candidate = Path(root).absolute()
        if parent not in candidate.parents:
            return False
        story = story_for(parent).resolve(strict=True)
        return candidate == story and parent in story.parents
    except (OSError, ValueError):
        return False


def story_for_cwd(cwd):
    """Resolve an actual host working directory inside the planned story."""
    from context_learning.synthetic_scope import NOVEL_ROOT
    from context_learning.synthetic_experiment import story_for
    try:
        parent = NOVEL_ROOT.resolve(strict=True)
        path = Path(cwd).absolute()
        if parent not in path.parents:
            return None
        story = story_for(parent).resolve(strict=True)
        path.relative_to(story)
        path.resolve(strict=True).relative_to(story)
        return story
    except (OSError, ValueError):
        return None


def read(root):
    """Validate every live pin before any source scan, provider, or QMD write."""
    from context_learning.synthetic_scope import NOVEL_ROOT, policy_for
    from context_learning.synthetic_experiment import shared_for
    import wiki_topical as topical

    root = Path(root).absolute()
    if not exact_story(root) or root.is_symlink() or not topical.opted_in(root):
        raise ValueError('topical_story_not_opted_in')
    parent = NOVEL_ROOT.resolve(strict=True)
    if not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('topical_story_unsafe_root')
    policy_for(shared_for(parent), parent, for_inflight_verification=True)
    raw = _private_bytes(root / POLICY)
    try:
        policy = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError('topical_story_policy_invalid') from exc
    keys = {'schema', 'enabled', 'storyRoot', 'parentRoot', 'parentSettingsSha256',
            'syntheticTeacherPolicySha256', 'autoRefreshPolicySha256',
            'sourceRoots', 'wikiCollection', 'engine', 'transport'}
    if (not isinstance(policy, dict) or set(policy) != keys
            or policy['schema'] != SCHEMA or policy['enabled'] is not True
            or policy['storyRoot'] != str(root) or policy['parentRoot'] != str(parent)
            or policy['sourceRoots'] != SOURCES
            or policy['wikiCollection'] != WIKI_COLLECTION
            or policy['engine'] != 'codex'
            or policy['transport'] != 'codex-cli-subscription'):
        raise ValueError('topical_story_policy_invalid')
    settings = _private_bytes(parent / '.auto-context/settings.json', settings=True)
    teacher = _private_bytes(shared_for(parent) / 'synthetic-teacher-policy.json')
    auto = _private_bytes(root / AUTO_POLICY)
    for name, content in (('parentSettingsSha256', settings),
                          ('syntheticTeacherPolicySha256', teacher),
                          ('autoRefreshPolicySha256', auto)):
        if policy[name] != hashlib.sha256(content).hexdigest():
            raise ValueError('topical_story_policy_pin_changed')
    try:
        auto_cfg = json.loads(auto)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError('topical_story_auto_policy_invalid') from exc
    if (not isinstance(auto_cfg, dict) or auto_cfg.get('enabled') is not True
            or auto_cfg.get('engine') != 'codex'
            or auto_cfg.get('sourceRoots') != SOURCES
            or auto_cfg.get('compileCfg') != {
                'extractor': {'builtins': ['codex']},
                'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}):
        raise ValueError('topical_story_auto_policy_invalid')
    wiki = root / '.auto-context/wiki'
    if (wiki.is_symlink() or not wiki.is_dir() or wiki.stat().st_uid != os.getuid()
            or wiki.stat().st_mode & 0o077):
        raise ValueError('topical_story_wiki_unsafe')
    return {'story': root, 'project': parent, 'wiki': wiki,
            'collection': WIKI_COLLECTION, 'policySha256': hashlib.sha256(raw).hexdigest()}


def provider_env(root):
    """Use the normal signed-in Codex CLI account, excluding API-key routes."""
    read(root)
    return {key:value for key,value in os.environ.items() if key not in BILLED_ENV}


def validate_wiki_candidates(root, candidates):
    """Recheck journal-supplied context against this story before model egress."""
    scope = read(root)
    if candidates is None or candidates == []:
        return
    if not isinstance(candidates, list) or len(candidates) > 8:
        raise ValueError('topical_story_candidate_unscoped')
    for row in candidates:
        if not isinstance(row, dict) or set(row) != {'path','lead'}:
            raise ValueError('topical_story_candidate_unscoped')
        relative, lead = row['path'], row['lead']
        if (not isinstance(relative, str) or not isinstance(lead, str)
                or not re.fullmatch(r'topical-v2/[0-9a-f]{24}/[a-z0-9][a-z0-9-]*\.md', relative)):
            raise ValueError('topical_story_candidate_unscoped')
    import qmd_route
    import sqlite_read
    import wiki_topical as topical
    import wiki_topical_publish as publisher
    import wiki_topical_experiment as experiment
    route = qmd_route.project_paths(scope['project'], isolated=True, reject_conflicts=True)
    if route.get('selected') is not True:
        raise ValueError('topical_story_candidate_unscoped')
    with sqlite_read.connect(route['INDEX_PATH'], timeout=1) as index:
        for row in candidates:
            relative, lead = row['path'], row['lead']
            _prefix, gid, filename = relative.split('/')
            cid = filename[:-3]
            page = scope['wiki'] / relative
            if (page.is_symlink() or page.parent.is_symlink()
                    or page.parent.parent.is_symlink() or not page.is_file()):
                raise ValueError('topical_story_candidate_unscoped')
            card = experiment.load_staged_card(scope['story'], gid, cid)
            publisher._attested(scope['story'], gid, cid)
            if lead != card['lead'][:600]:
                raise ValueError('topical_story_candidate_unscoped')
            revision = hashlib.sha256(page.read_bytes()).hexdigest()
            rows = index.execute('SELECT hash FROM documents WHERE collection=? AND path=? '
                                 'AND active=1', (scope['collection'],relative)).fetchall()
            if rows != [(revision,)]:
                raise ValueError('topical_story_candidate_unscoped')
