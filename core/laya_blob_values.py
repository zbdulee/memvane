"""Value contracts for the inactive Git-blob candidate format."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import struct
import unicodedata

SHA = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{32}\Z')
MAX_BODY = 4 * 1024 * 1024
MAX_CHUNKS = 1024
MAX_SET_BYTES = 16 * 1024 * 1024


class BlobStoreError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def fail(reason):
    raise BlobStoreError(reason)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail('invalid_canonical_value')


def digest(domain, value):
    try:
        return hashlib.sha256(domain.encode('ascii') + b'\0' +
                              canonical(value).encode('utf8')).hexdigest()
    except UnicodeError:
        fail('invalid_canonical_value')


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def identifier(value, pattern=SHA):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail('invalid_identifier')
    return value


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(c in value for c in '\x00\r\n\\') or value.startswith('/')
            or any(not p or p.startswith('.') for p in value.split('/'))):
        fail('invalid_relative_path')
    return value


def repository(value):
    from laya_checkout_view import REPOSITORY_FIELDS, REPOSITORY_SCHEMA, _identity
    if (not isinstance(value, dict) or set(value) != REPOSITORY_FIELDS
            or value.get('schema') != REPOSITORY_SCHEMA):
        fail('invalid_repository_binding')
    identifier(value.get('repoId'), UUID)
    kind = value.get('kind')
    if kind == 'git':
        valid = (_identity(value.get('commonGitDir')) and value.get('projectRoot') is None
                 and value.get('gitObjectFormat') in ('sha1', 'sha256'))
    elif kind == 'non-git':
        valid = (_identity(value.get('projectRoot')) and value.get('commonGitDir') is None
                 and value.get('gitObjectFormat') is None)
    else:
        valid = False
    project_rel = value.get('projectRelative')
    if not valid or not isinstance(project_rel, str):
        fail('invalid_repository_binding')
    if project_rel != '.':
        relative(project_rel)
    return json.loads(canonical(value))


def parse_canonical(text):
    try:
        value = json.loads(text)
        if canonical(value) != text:
            fail('noncanonical_json')
        return value
    except (ValueError, TypeError, RecursionError):
        fail('invalid_canonical_json')


def integer(value, lower=0, upper=2 ** 63 - 1):
    if type(value) is not int or not lower <= value <= upper:
        fail('invalid_integer')
    return value


def text(value, limit=512):
    if (not isinstance(value, str) or not value
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') for c in value)):
        fail('invalid_text')
    try:
        if len(value.encode('utf8')) > limit:
            fail('invalid_text')
    except UnicodeError:
        fail('invalid_text')
    return value


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        fail('invalid_fields')


RECIPE_FIELDS = frozenset(('input_recipe', 'tokenizer_sha', 'chunker_sha', 'window_tokens',
    'overlap_tokens', 'window_search_tokens', 'offset_unit', 'heading_rule',
    'document_template', 'synthetic_path'))
PROFILE_FIELDS = frozenset(('model_id', 'model_sha', 'embedding_fingerprint', 'dimension',
    'metric', 'document_input_recipe', 'query_input_recipe', 'query_template'))


def recipe(spec):
    exact(spec, RECIPE_FIELDS)
    fixed = dict(input_recipe='blob-input-v1', offset_unit='utf16-code-unit',
                 heading_rule='qmd-markdown-v1', document_template='title: {title} | text: {text}',
                 synthetic_path='blobs/{blob_sha}.md')
    if any(spec[k] != value for k, value in fixed.items()):
        fail('recipe_unsupported')
    identifier(spec['tokenizer_sha']); identifier(spec['chunker_sha'])
    window = integer(spec['window_tokens'], 1, 8192)
    integer(spec['overlap_tokens'], 0, window - 1)
    integer(spec['window_search_tokens'], 0, window)
    return json.loads(canonical(spec))


def profile(spec):
    exact(spec, PROFILE_FIELDS)
    text(spec['model_id']); identifier(spec['model_sha']); identifier(spec['embedding_fingerprint'])
    integer(spec['dimension'], 1, 2048)
    fixed = dict(metric='cosine', document_input_recipe='blob-input-v1',
                 query_input_recipe='qmd-query-v1', query_template='task: search result | query: {text}')
    if any(spec[k] != value for k, value in fixed.items()):
        fail('profile_unsupported')
    return json.loads(canonical(spec))


def chunks(body, rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= MAX_CHUNKS:
        fail('chunk_set_invalid')
    source = body.encode('utf-16-le')
    normalized, previous, total = [], -1, 0
    for seq, row in enumerate(rows):
        exact(row, ('seq', 'position', 'text', 'text_sha'))
        if integer(row['seq']) != seq:
            fail('chunk_sequence_invalid')
        position = integer(row['position'], 0, len(source) // 2)
        if position <= previous or not isinstance(row['text'], str) or not row['text']:
            fail('chunk_position_invalid')
        try:
            raw = row['text'].encode('utf8')
            units = row['text'].encode('utf-16-le')
        except UnicodeError:
            fail('chunk_text_invalid')
        total += len(raw)
        if total > MAX_SET_BYTES or source[position * 2:position * 2 + len(units)] != units:
            fail('chunk_text_invalid')
        identifier(row['text_sha'])
        if hash_bytes(raw) != row['text_sha']:
            fail('chunk_text_hash_mismatch')
        normalized.append(dict(row)); previous = position
    return normalized


def chunk_digest(blob, recipe_id, rows):
    return digest('memvane-chunk-set-v1', [blob, recipe_id, rows])


def embedding_rows(rows, dimension, inputs):
    if not isinstance(rows, (list, tuple)) or len(rows) != len(inputs) or not rows:
        fail('embedding_set_invalid')
    normalized = []
    for seq, row in enumerate(rows):
        exact(row, ('seq', 'input_sha', 'embedding'))
        if integer(row['seq']) != seq:
            fail('embedding_sequence_invalid')
        identifier(row['input_sha'])
        if row['input_sha'] != inputs[seq]:
            fail('embedding_input_mismatch')
        blob = row['embedding']
        if not isinstance(blob, bytes) or len(blob) != dimension * 4:
            fail('embedding_dimension_mismatch')
        values = struct.unpack('<' + 'f' * dimension, blob)
        if not all(math.isfinite(x) for x in values) or math.fsum(x * x for x in values) <= 0:
            fail('embedding_values_invalid')
        normalized.append(dict(row))
    return normalized


def vector_digest(blob, recipe_id, profile_id, row):
    return digest('memvane-embedding-row-v1',
                  [blob, recipe_id, profile_id, row['seq'], row['input_sha'], row['embedding'].hex()])


def embedding_digest(blob, recipe_id, profile_id, rows):
    return digest('memvane-embedding-set-v1', [blob, recipe_id, profile_id,
                  [[r['seq'], r['input_sha'], r['embedding'].hex()] for r in rows]])


# ECMAScript whitespace/line terminators, matching laya_qmd_chunking.mjs.
# Python's \s/strip additionally treats NEL and U+001C..001F as whitespace and
# its MULTILINE anchors do not recognize CR/LS/PS. Those alter provider inputs.
_JS_WS = '\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
_JS_LINE = '\n\r\u2028\u2029'
_TITLE_START = r'(?:\A|(?<=[' + _JS_LINE + r']))'
_TITLE_END = r'([^' + _JS_LINE + r']+)(?=\Z|[' + _JS_LINE + r'])'
_TITLE = re.compile(_TITLE_START + r'##?[' + re.escape(_JS_WS) + r']+' + _TITLE_END)
_SECOND_TITLE = re.compile(_TITLE_START + r'##[' + re.escape(_JS_WS) + r']+' + _TITLE_END)


def markdown_title(body, blob_sha):
    identifier(blob_sha)
    match = _TITLE.search(body)
    if match is None:
        return blob_sha  # synthetic blobs/<sha>.md filename fallback
    title = match.group(1).strip(_JS_WS)
    if title in ('📝 Notes', 'Notes'):
        second = _SECOND_TITLE.search(body)
        if second is not None:
            return second.group(1).strip(_JS_WS)
    return title


FINGERPRINT_FIELDS = frozenset(('worktree_id', 'branch_key', 'path', 'source_sha',
    'byte_length', 'head_oid', 'tracked_state', 'binding_state', 'scan_id', 'observed_ns'))


def fingerprint(spec, object_format):
    exact(spec, FINGERPRINT_FIELDS)
    identifier(spec['worktree_id'], UUID); identifier(spec['scan_id'], UUID)
    relative(spec['path']); identifier(spec['source_sha'])
    integer(spec['byte_length']); integer(spec['observed_ns'])
    if spec['tracked_state'] not in ('tracked', 'untracked', 'unknown') or spec['binding_state'] not in ('observed', 'legacy_unbound'):
        fail('source_state_invalid')
    branch, head = spec['branch_key'], spec['head_oid']
    if not isinstance(branch, str) or not branch or len(branch.encode('utf8', errors='replace')) > 4096:
        fail('source_branch_invalid')
    if object_format is None:
        if branch != 'non-git' or head is not None:
            fail('source_branch_invalid')
    else:
        length = 40 if object_format == 'sha1' else 64
        if head is not None and (not isinstance(head, str) or re.fullmatch('[0-9a-f]{' + str(length) + '}', head) is None):
            fail('source_head_invalid')
        if branch.startswith('detached:'):
            if head is None or branch != 'detached:' + head:
                fail('source_branch_invalid')
        elif branch.startswith('refs/heads/'):
            tail = branch[len('refs/heads/'):]
            if (not tail or '..' in tail or '@{' in tail or tail.endswith(('.', '/'))
                    or any(ord(c) < 33 or c in '\x7f~^:?*[\\' for c in tail)
                    or any(not p or p.startswith('.') or p.endswith('.lock') for p in tail.split('/'))):
                fail('source_branch_invalid')
        else:
            fail('source_branch_invalid')
    return json.loads(canonical(spec))


def attestation(spec):
    exact(spec, ('blob_sha', 'verification_kind', 'engine_identity', 'policy_digest', 'receipt', 'sources'))
    identifier(spec['blob_sha']); text(spec['engine_identity']); identifier(spec['policy_digest'])
    kind = spec['verification_kind']
    if kind not in ('compiled', 'rebound', 'legacy_verified'):
        fail('verification_kind_invalid')
    receipt = spec['receipt']
    exact(receipt, ('schema', 'verdict', 'result_digest', 'prior_trust'))
    if receipt['schema'] != 'memvane-card-evidence-v1' or receipt['verdict'] != 'verified':
        fail('receipt_invalid')
    identifier(receipt['result_digest'])
    if kind == 'rebound':
        identifier(receipt['prior_trust'])
    elif receipt['prior_trust'] is not None:
        fail('receipt_prior_invalid')
    sources = spec['sources']
    if not isinstance(sources, list) or len(sources) > 64 or (kind == 'rebound' and not sources):
        fail('attestation_sources_invalid')
    seen = set()
    for source in sources:
        exact(source, ('fingerprint_id', 'spans'))
        identifier(source['fingerprint_id'])
        if source['fingerprint_id'] in seen:
            fail('duplicate_source_fingerprint')
        seen.add(source['fingerprint_id'])
        spans = source['spans']
        if not isinstance(spans, list) or len(spans) > 256:
            fail('source_spans_invalid')
        end = 0
        for span in spans:
            exact(span, ('start', 'end'))
            start, stop = integer(span['start']), integer(span['end'])
            if start < end or stop <= start:
                fail('source_spans_invalid')
            end = stop
    return json.loads(canonical(spec))
