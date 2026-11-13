"""Generation manifest, stage and binding value contracts; metadata only.

Manifests, fingerprints and receipts are caller-owned commitments. Derived
eligibility and seal roots are structural consistency checks, never compiler,
observation or operational authority. No raw source or wiki body is stored here.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time

from memvane.store import blob_values as v
import laya_store_primitives as primitives

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
SOURCE_CONTENT_SCHEMA = 'memvane-source-content-v1'
SOURCE_OBSERVATION_SCHEMA = 'memvane-source-observation-v1'
SOURCE_OBSERVATION_FIELDS = ('worktree_id', 'branch_key', 'path', 'tracked_state',
                             'binding_state', 'content_id', 'legacy_fingerprint_id')
STAGE_ITEM_FIELDS = ('path', 'action', 'input_sha', 'blob_sha', 'trust_digest', 'metadata_json')


def source_content(source_sha, byte_length):
    """Identity of observed bytes; checkout and length are not identity inputs."""
    v.identifier(source_sha)
    v.integer(byte_length)
    return dict(content_id=v.digest(SOURCE_CONTENT_SCHEMA, source_sha),
                source_sha=source_sha, byte_length=byte_length)


def check_source_content(row, source_sha, byte_length):
    """Validate a persisted content row before reusing it for an observation."""
    v.exact(row, ('content_id', 'source_sha', 'byte_length'))
    expected = source_content(source_sha, byte_length)
    if row != expected or type(row['byte_length']) is not int:
        v.fail('source_content_mismatch')
    return expected


def source_observation(*, worktree_id, branch_key, path, tracked_state, binding_state,
                       content_id, legacy_fingerprint_id):
    """Scoped identity only; the caller must separately prove the observation."""
    v.identifier(worktree_id, v.UUID)
    v.source_path(path)
    try:
        branch_valid = isinstance(branch_key, str) and 0 < len(branch_key.encode('utf8')) <= 4096
    except UnicodeError:
        branch_valid = False
    if not branch_valid:
        v.fail('source_branch_invalid')
    if tracked_state not in ('tracked', 'untracked', 'unknown') or binding_state not in ('observed', 'legacy_unbound'):
        v.fail('source_state_invalid')
    if (content_id is None) == (legacy_fingerprint_id is None):
        v.fail('source_observation_invalid')
    v.identifier(content_id if content_id is not None else legacy_fingerprint_id)
    data = dict(worktree_id=worktree_id, branch_key=branch_key, path=path,
                tracked_state=tracked_state, binding_state=binding_state,
                content_id=content_id, legacy_fingerprint_id=legacy_fingerprint_id)
    return dict(observation_id=v.digest(SOURCE_OBSERVATION_SCHEMA, data), **data)


def check_source_observation(row):
    v.exact(row, ('observation_id', *SOURCE_OBSERVATION_FIELDS))
    expected = source_observation(**{key: row[key] for key in SOURCE_OBSERVATION_FIELDS})
    if row != expected:
        v.fail('source_observation_mismatch')
    return expected


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


def check_stage_item(value):
    """Validate one normalized card delta before a durable stage write."""
    v.exact(value, STAGE_ITEM_FIELDS)
    card_path(value['path'])
    if value['action'] == 'delete':
        if any(value[key] is not None for key in ('input_sha', 'blob_sha', 'trust_digest', 'metadata_json')):
            v.fail('stage_item_invalid')
    elif value['action'] == 'put':
        v.identifier(value['input_sha']); v.identifier(value['blob_sha'])
        if value['trust_digest'] is not None:
            v.identifier(value['trust_digest'])
        v.parse_canonical(value['metadata_json'])
    else:
        v.fail('stage_item_invalid')
    return {key: value[key] for key in STAGE_ITEM_FIELDS}


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
        v.source_path(source['path']); v.identifier(source['fingerprint_id'])
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


def fts_rows(bodies, titles=None, *, deadline=None):
    """CJK-spaced view path, title and body."""
    if deadline is not None and time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')
    normalized, rows = {}, []
    for path, body in sorted(bodies.items()):
        if deadline is not None and time.monotonic() >= deadline:
            v.fail('fts_deadline_exceeded')
        if body not in normalized:
            normalized[body] = primitives._qmd_fts_text(body, deadline=deadline)
        rows.append(dict(filepath=primitives._qmd_fts_text(path, deadline=deadline),
                         title=primitives._qmd_fts_text(titles[path] if titles is not None else primitives._qmd_title(body, path), deadline=deadline),
                         body=normalized[body], path_key=path))
    if deadline is not None and time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')
    return rows


def view_fields(handle):
    return {k: getattr(handle, k) for k in VIEW_FIELDS}










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
