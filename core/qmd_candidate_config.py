"""Pure fail-closed proof for a disposable candidate QMD ``index.yml``.

QMD 2.5.3 runs a YAML collection's ``update`` value through ``bash -c``
during ``qmd update``. A candidate generation must therefore never hand QMD
config bytes whose collection block was not proved to carry only the fields
this plugin writes. This module decides that from bytes alone: it reads no
file, writes nothing, starts no process and selects no path.

It does not parse YAML. It accepts only the narrow shape QMD writes after
``collection add`` over a staged baseline (the same shape
``qmd_shared_store._registered_config_extension`` accepts)::

    collections:
      <name>:
        path: <absolute path>
        pattern: "**/*.md"
    <baseline tail, byte-identical>

Every byte outside the collection block is bound to a caller-supplied
approved baseline hash computed over ``collections: {}\\n`` + tail, so a
changed model or any other edit outside the block fails. Each name/path must
be in an explicit approved map (or one derived from the shared registry).
Anything else, including YAML forms that would mean the same thing, is
rejected: aliases, anchors, tags, flow mappings, nested values, extra
collection fields, comments, ambiguous scalars, duplicate names.

Constraints the caller still owns (this proof cannot see them):
- Paths are compared as strings. Symlinks are not resolved; pass resolved
  approved paths and resolved candidate directories.
- The proof is over bytes. The caller must write exactly those bytes to a
  fresh private ``QMD_CONFIG_DIR/index.yml`` and re-check the hash right
  before invoking QMD (time-of-check/time-of-use is outside this module).
- The baseline tail is trusted because its hash was approved; it gets only a
  coarse line-shape check here.
- Fail-closed means some valid YAML that QMD could write differently (block
  sequences in the tail, quoted names, other key orders) is rejected.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import posixpath
import re
from types import MappingProxyType
from typing import NamedTuple


PATTERN = '**/*.md'
PATH_KEYS = ('INDEX_PATH', 'QMD_CONFIG_DIR', 'XDG_CACHE_HOME')
MAX_CONFIG_BYTES = 1048576  # Same bound as the shared collection registry.
MAX_COLLECTIONS = 4096
MAX_PATH_CHARS = 4096

_SHA = re.compile(r'[0-9a-f]{64}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
# Tab/CR/BOM/C0/C1 and the separators str.splitlines() would also split on.
_FORBIDDEN = re.compile('[\x00-\x09\x0b-\x1f\x7f-\x9f  ﻿]')
_UPDATE = re.compile(r'''[ ]*(?:-[ ]+)?(?:update|"update"|'update')[ ]*:''')
_SINGLE = re.compile(r"'((?:[^']|'')*)'")
_FIELD = re.compile(r'    ([^ :]+):(?: (.*))?')
_TAIL_TOP = re.compile(r'([A-Za-z_][A-Za-z0-9_]*):(?: (.+))?')
_TAIL_INDENTED = re.compile(r'(?:  )+([A-Za-z_][A-Za-z0-9_.-]*):(?: (.+))?')
# Names a YAML core/1.1 resolver turns into non-strings (int, float, date,
# bool, null). QMD's key would then differ from the approved name.
_AMBIGUOUS_NAME = re.compile(
    r'[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?'
    r'|0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+'
    r'|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt._-].*)?')
_AMBIGUOUS_WORDS = frozenset(
    ('y', 'yes', 'n', 'no', 'true', 'false', 'on', 'off', 'null'))


class CandidateConfigProof(NamedTuple):
    ok: bool
    reason: str
    config_sha256: str | None
    baseline_sha256: str | None
    collections: Mapping


class CandidateEnvironment(NamedTuple):
    ok: bool
    reason: str
    proof: CandidateConfigProof
    env: Mapping | None


_EMPTY = MappingProxyType({})


def _reject(reason):
    return CandidateConfigProof(False, reason, None, None, _EMPTY)


def _ambiguous_name(name):
    return bool(_AMBIGUOUS_NAME.fullmatch(name)) or name.lower() in _AMBIGUOUS_WORDS


def _safe_abs(path):
    return (isinstance(path, str) and 1 < len(path) <= MAX_PATH_CHARS
            and path.startswith('/') and not path.startswith('//')
            and posixpath.normpath(path) == path
            and '\n' not in path and not _FORBIDDEN.search(path))


def _overlaps(first, second):
    return (first == second or first.startswith(second + '/')
            or second.startswith(first + '/'))


def _approved_map(approved):
    if not isinstance(approved, Mapping) or len(approved) > MAX_COLLECTIONS:
        return None
    result = {}
    for name, path in approved.items():
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or _ambiguous_name(name) or not _safe_abs(path)):
            return None
        result[name] = path
    return result


def approved_from_registry(registry, scopes=None):
    """Indexed name -> path from a shared registry dict; None if invalid.

    Pure: the caller reads the registry. Shape validation is the shared
    store's own ``_valid_registry`` so the two cannot drift. ``scopes``
    optionally restricts owners to approved project roots.
    """
    import qmd_shared_store
    try:
        valid = qmd_shared_store._valid_registry(registry)
    except ValueError:
        return None
    if scopes is not None:
        if isinstance(scopes, (str, bytes)):
            return None
        try:
            scopes = frozenset(scopes)
        except TypeError:
            return None
    return MappingProxyType({
        name: owner['path'] for name, owner in valid.items()
        if owner['indexed'] is True and (scopes is None or owner['scope'] in scopes)})


def _decode(raw):
    if not isinstance(raw, (bytes, bytearray)):
        return None, 'config_not_bytes'
    raw = bytes(raw)
    if len(raw) > MAX_CONFIG_BYTES:
        return None, 'config_too_large'
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None, 'config_not_utf8'
    if _FORBIDDEN.search(text):
        return None, 'config_forbidden_character'
    if not text.endswith('\n'):
        return None, 'config_no_trailing_newline'
    return text, None


def _scalar(value):
    """One-line scalar as a str, or None for any form not proved equivalent."""
    if value is None or not value:
        return None
    if value.startswith('"'):
        if not value.endswith('"'):
            return None  # Trailing comment or garbage after the quote.
        try:
            parsed = json.loads(value)
        except ValueError:
            return None  # YAML-only escapes such as \x41 are rejected too.
        if not isinstance(parsed, str) or _FORBIDDEN.search(parsed):
            return None
        return parsed
    if value.startswith("'"):
        match = _SINGLE.fullmatch(value)
        return match.group(1).replace("''", "'") if match else None
    # Plain: only an absolute path can be plain, which excludes every YAML
    # indicator start (alias, anchor, tag, flow, block scalar, quote).
    if (value != value.strip() or not value.startswith('/') or value.endswith(':')
            or '#' in value or ': ' in value):
        return None
    return value


def _tail_reason(tail):
    for line in tail:
        match = _TAIL_TOP.fullmatch(line)
        if match:
            if match.group(1) == 'collections':
                return 'baseline_tail_invalid'
            value = match.group(2)
        else:
            match = _TAIL_INDENTED.fullmatch(line)
            if not match:
                return 'baseline_tail_invalid'
            value = match.group(2)
        if value is not None and (value != value.strip() or value[0] in '&*!|>%@`'):
            return 'baseline_tail_invalid'
    return None


def _entries(section, allowed):
    collections = {}
    index = 0
    while index < len(section):
        heading = section[index]
        if not heading.startswith('  ') or heading.startswith('   '):
            return None, 'collection_entry_malformed'
        rest = heading[2:]
        if not rest.endswith(':'):
            return None, 'collection_entry_malformed'  # Inline/flow/anchored value.
        name = rest[:-1]
        if not _NAME.fullmatch(name):
            return None, 'collection_name_invalid'
        if _ambiguous_name(name):
            return None, 'collection_name_ambiguous'
        if name in collections:
            return None, 'duplicate_collection'
        end = index + 1
        while end < len(section) and section[end].startswith('   '):
            end += 1
        values, keys = {}, []
        for line in section[index + 1:end]:
            if line.startswith('     '):
                return None, 'collection_nested_value'
            match = _FIELD.fullmatch(line)
            if not match:
                return None, 'collection_entry_malformed'
            keys.append(match.group(1))
            values[match.group(1)] = match.group(2)
        if set(keys) - {'path', 'pattern'}:
            return None, 'collection_field_unexpected'
        if keys != ['path', 'pattern']:
            return None, 'collection_entry_malformed'
        path = _scalar(values['path'])
        if path is None or not _safe_abs(path):
            return None, 'collection_path_invalid'
        raw_pattern = values['pattern']
        if (raw_pattern is None or raw_pattern[:1] not in ('"', "'")
                or _scalar(raw_pattern) != PATTERN):
            return None, 'collection_pattern_invalid'
        if name not in allowed:
            return None, 'collection_not_approved'
        if allowed[name] != path:
            return None, 'collection_path_not_approved'
        collections[name] = path
        if len(collections) > MAX_COLLECTIONS:
            return None, 'too_many_collections'
        index = end
    return collections, None


def prove(raw, *, baseline_sha256, approved, required_names=None):
    """Decide whether ``raw`` is safe for a disposable candidate update.

    ``approved`` is name -> absolute path (see ``approved_from_registry``).
    ``required_names``, when given, must equal the proved collection set
    exactly (e.g. the candidate index's physical ``store_collections``).
    Never raises for content; every failure is a reason string.
    """
    if not isinstance(baseline_sha256, str) or not _SHA.fullmatch(baseline_sha256):
        return _reject('baseline_sha_invalid')
    allowed = _approved_map(approved)
    if allowed is None:
        return _reject('approved_map_invalid')
    required = None
    if required_names is not None:
        if isinstance(required_names, (str, bytes)):
            return _reject('approved_map_invalid')
        try:
            required = frozenset(required_names)
        except TypeError:
            return _reject('approved_map_invalid')
        if not required <= set(allowed):
            return _reject('approved_map_invalid')
    text, reason = _decode(raw)
    if reason:
        return _reject(reason)
    lines = text.split('\n')[:-1]
    if any(_UPDATE.match(line) for line in lines):
        return _reject('shell_update_command')
    if lines[0] == 'collections: {}':
        section, tail = [], lines[1:]
    elif lines[0] == 'collections:':
        boundary = next((i for i, line in enumerate(lines[1:], 1)
                         if not line.startswith(' ')), len(lines))
        section, tail = lines[1:boundary], lines[boundary:]
        if not section:
            return _reject('collections_heading_invalid')
    else:
        return _reject('collections_heading_invalid')
    reason = _tail_reason(tail)
    if reason:
        return _reject(reason)
    baseline = ('collections: {}\n' + ''.join(line + '\n' for line in tail)).encode('utf-8')
    if hashlib.sha256(baseline).hexdigest() != baseline_sha256:
        return _reject('baseline_mismatch')
    collections, reason = _entries(section, allowed)
    if reason:
        return _reject(reason)
    if required is not None and set(collections) != required:
        return _reject('collection_set_mismatch')
    return CandidateConfigProof(True, 'proved', hashlib.sha256(bytes(raw)).hexdigest(),
                                baseline_sha256, MappingProxyType(dict(collections)))


def candidate_environment(raw, *, baseline_sha256, approved, index_path,
                          config_dir, cache_home, required_names=None):
    """Immutable candidate env only after the config proof passes.

    Re-runs ``prove`` itself so a hand-built proof cannot unlock it. The
    three paths are caller-chosen; this only checks they are absolute,
    normalized, distinct and disjoint from every approved source path, so a
    candidate run cannot write its index/config/cache inside a source tree
    (and cleaning the candidate cannot remove a source). It never invokes QMD.
    """
    proof = prove(raw, baseline_sha256=baseline_sha256, approved=approved,
                  required_names=required_names)
    if not proof.ok:
        return CandidateEnvironment(False, proof.reason, proof, None)
    paths = (index_path, config_dir, cache_home)
    if (not all(_safe_abs(path) for path in paths) or len(set(paths)) != len(paths)
            or index_path == config_dir + '/index.yml'):
        return CandidateEnvironment(False, 'candidate_path_invalid', proof, None)
    sources = _approved_map(approved).values()
    if any(_overlaps(path, source) for path in paths for source in sources):
        return CandidateEnvironment(False, 'candidate_path_overlaps_source', proof, None)
    env = MappingProxyType(dict(zip(PATH_KEYS, paths)))
    return CandidateEnvironment(True, 'proved', proof, env)
