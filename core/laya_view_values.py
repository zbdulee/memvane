"""Generation manifest, stage and binding value contracts; metadata only.

Manifests, fingerprints and receipts are caller-owned commitments. Derived
eligibility and seal roots are structural consistency checks, never compiler,
observation or operational authority. No raw source or wiki body is stored here.
"""
from __future__ import annotations

import fnmatch
import json
import re

import laya_blob_values as v
import laya_index_a as legacy

MAX_MEMBERS = 20000
MAX_SOURCES = 20000
MAX_WIKI_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_SKIPS = 1024
MAX_HANDLES = 4096
GEN_ID = re.compile(r'g-[0-9a-f]{32}-b[0-9a-f]{64}\Z')
PRESENCE = ('present', 'held_missing')
RESERVED = ('SCHEMA.md', 'index.md', 'log.md')
MANIFEST_FIELDS = ('scan_id', 'policy_digest', 'revocation_epoch', 'complete', 'sources', 'members')
SOURCE_FIELDS = ('path', 'fingerprint_id', 'presence')
MEMBER_FIELDS = ('path', 'blob_sha', 'display_title', 'trust_digest')
HEAD_FIELDS = ('view_id', 'gen_id', 'epoch', 'incarnation')
VIEW_FIELDS = ('view_id', 'worktree_id', 'branch_key', 'head_oid', 'checkout_digest', 'policy_digest')
STAGE_FIELDS = ('schema', 'build_id', 'incarnation', 'view', 'worktree_binding', 'settings_sha',
                'base', 'manifest_digest', 'manifest', 'skip_paths', 'files')
STAGE_SCHEMA = 'memvane-generation-stage-v1'
VECTORS = v.digest('memvane-generation-vectors-v1', {'mode': 'lexical-only', 'members': []})


def _plain(value, limit=MAX_MANIFEST_BYTES):
    raw = v.canonical(value)
    if len(raw.encode('utf8')) > limit:
        v.fail('manifest_too_large')
    return json.loads(raw)


def card_path(path):
    v.relative(path)
    if not path.endswith('.md') or path in RESERVED:
        v.fail('wiki_card_path_invalid')
    return path


def manifest(value, policy_digest):
    """Exact caller manifest -> plain values; refuses before any transaction."""
    v.exact(value, MANIFEST_FIELDS)
    v.identifier(value['scan_id'], v.UUID); v.identifier(value['policy_digest'])
    if value['policy_digest'] != policy_digest:
        v.fail('manifest_policy_mismatch')
    v.integer(value['revocation_epoch'])
    if value['complete'] is not True:
        v.fail('manifest_incomplete')
    sources, members = value['sources'], value['members']
    if (type(sources) is not list or type(members) is not list
            or len(sources) > MAX_SOURCES or len(members) > MAX_MEMBERS):
        v.fail('manifest_bounds')
    paths, ids, plain_sources = set(), set(), []
    for source in sources:
        v.exact(source, SOURCE_FIELDS)
        v.relative(source['path']); v.identifier(source['fingerprint_id'])
        if not isinstance(source['presence'], str) or source['presence'] not in PRESENCE:
            v.fail('source_presence_invalid')
        if source['path'] in paths or source['fingerprint_id'] in ids:
            v.fail('manifest_duplicate')
        paths.add(source['path']); ids.add(source['fingerprint_id'])
        plain_sources.append({k: source[k] for k in SOURCE_FIELDS})
    paths, plain_members = set(), []
    for member in members:
        v.exact(member, MEMBER_FIELDS)
        card_path(member['path']); v.identifier(member['blob_sha']); v.text(member['display_title'])
        if member['trust_digest'] is not None:
            v.identifier(member['trust_digest'])
        if member['path'] in paths:
            v.fail('manifest_duplicate')
        paths.add(member['path'])
        plain_members.append({k: member[k] for k in MEMBER_FIELDS})
    return _plain(dict(scan_id=value['scan_id'], policy_digest=value['policy_digest'],
                       revocation_epoch=value['revocation_epoch'], complete=True,
                       sources=plain_sources, members=plain_members))


def manifest_digest(value):
    return v.digest('memvane-generation-manifest-v1', value)


def expected_head(value, view_id, incarnation):
    v.exact(value, HEAD_FIELDS)
    if not isinstance(value['view_id'], str) or value['view_id'] != view_id or value['incarnation'] != incarnation:
        v.fail('view_head_conflict')
    epoch, gen = v.integer(value['epoch']), value['gen_id']
    if (epoch == 0) != (gen is None) or (gen is not None and (not isinstance(gen, str) or not GEN_ID.fullmatch(gen))):
        v.fail('view_head_invalid')
    return dict(view_id=view_id, gen_id=gen, epoch=epoch, incarnation=incarnation)


def skip_patterns(*configs):
    """First well-formed ``skipPaths`` (normalized config, then raw settings)."""
    found = False
    for config in configs:
        if not isinstance(config, dict) or 'skipPaths' not in config:
            continue
        found, items = True, config['skipPaths']
        if (isinstance(items, list) and len(items) <= MAX_SKIPS
                and all(isinstance(x, str) and 0 < len(x) <= 4096 and '\x00' not in x for x in items)):
            return sorted({x.strip('/') for x in items if x.strip('/')})
    if found:
        v.fail('skip_paths_invalid')
    return []


def skipped(path, patterns):
    """Conservative match: over-skipping only lowers derived eligibility."""
    parts = path.split('/')
    for pattern in patterns:
        if path == pattern or path.startswith(pattern + '/'):
            return True
        if '/' not in pattern and any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern + '/*'):
            return True
    return False


def fts_rows(bodies, titles=None):
    """CJK-spaced view path/title/body; identical inputs retain v6 ranking."""
    return [dict(filepath=legacy._qmd_fts_text(path),
                 title=legacy._qmd_fts_text(titles[path] if titles is not None else legacy._qmd_title(body, path)),
                 body=legacy._qmd_fts_text(body), path_key=path)
            for path, body in sorted(bodies.items())]


def view_fields(handle):
    return {k: getattr(handle, k) for k in VIEW_FIELDS}


def stage(build_id, incarnation, handle, saved, base, value, skips, files):
    return _plain(dict(schema=STAGE_SCHEMA, build_id=build_id, incarnation=incarnation,
                       view=view_fields(handle), worktree_binding=saved['binding_digest'],
                       settings_sha=saved['settings_sha'], base=base,
                       manifest_digest=manifest_digest(value), manifest=value,
                       skip_paths=skips, files=files), MAX_MANIFEST_BYTES * 2)


def check_stage(value, handle, incarnation):
    v.exact(value, STAGE_FIELDS)
    if (value['schema'] != STAGE_SCHEMA or value['view'] != view_fields(handle)
            or value['incarnation'] != incarnation
            or manifest(value['manifest'], handle.policy_digest) != value['manifest']
            or manifest_digest(value['manifest']) != value['manifest_digest']
            or skip_patterns({'skipPaths': value['skip_paths']}) != value['skip_paths']):
        v.fail('generation_binding_mismatch')
    expected_head(value['base'], handle.view_id, incarnation)
    v.identifier(value['build_id'], v.UUID)
    return value


def roots(members, sources, cards, lexical, storage):
    return dict(membership_digest=v.digest('memvane-generation-membership-v1', members),
                sources_digest=v.digest('memvane-generation-sources-v1', sources),
                trust_digest=v.digest('memvane-generation-trust-v1', [members, cards]),
                lexical_digest=lexical, fts_storage_digest=storage, vectors_digest=VECTORS,
                eligible_count=sum(m['eligible'] for m in members), vector_member_count=0)


def binding(stage_value, ordinal, capture, root_values):
    """All roots are derived before the final generation identity exists."""
    return v.digest('memvane-generation-binding-v1', dict(
        stage=v.digest('memvane-generation-stage-v1', stage_value), ordinal=ordinal,
        capture=v.digest('memvane-generation-capture-v1', capture), roots=root_values))


def gen_id(build_id, binding_digest):
    v.identifier(build_id, v.UUID); v.identifier(binding_digest)
    return f'g-{build_id}-b{binding_digest}'


def generation_fields(stage_value, ordinal, binding_digest, capture):
    view, value = stage_value['view'], stage_value['manifest']
    return dict(ordinal=ordinal, gen_id=gen_id(stage_value['build_id'], binding_digest),
                view_id=view['view_id'], parent_gen=stage_value['base']['gen_id'],
                head_oid=view['head_oid'], checkout_digest=view['checkout_digest'],
                checkout_evidence=v.canonical(capture), scan_id=value['scan_id'],
                policy_digest=view['policy_digest'], revocation_epoch=value['revocation_epoch'],
                binding_digest=binding_digest)
