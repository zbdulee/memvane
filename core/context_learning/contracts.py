"""Versioned synthetic/offline evidence contracts; eligibility is external."""
from __future__ import annotations

import hashlib

VERSION = 1
SUPPORTED_VERSIONS = (1, 2, 3, 4)
CLASS_ORDER = ('necessary', 'supporting', 'irrelevant')
CLASSES = frozenset(CLASS_ORDER)
MAX_CANDIDATES = 8
MAX_EXPERIMENT_CANDIDATES = 30
MAX_PROMPT_CHARS = 2000
MAX_EDIT_PROMPT_CHARS = 65536
MAX_EXCERPT_CHARS = 600


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def bounded(text: str, limit: int) -> dict:
    if not isinstance(text, str):
        raise ValueError('text_required')
    return {'text': text[:limit], 'truncated': len(text) > limit,
            'original_chars': len(text), 'sha256': digest(text[:limit])}


def request(payload: dict, candidates: list, *, host: str, request_id: str, sampling=None) -> dict:
    if host not in {'claude', 'codex', 'hermes'} or not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError('invalid_host_or_request')
    if not isinstance(payload, dict) or not isinstance(payload.get('prompt'), str):
        raise ValueError('missing_prompt')
    if not payload['prompt'].strip() or not isinstance(candidates, list) or len(candidates) > (MAX_EXPERIMENT_CANDIDATES if sampling is not None and 'candidate_limit' in sampling else 16 if sampling is not None else MAX_CANDIDATES):
        raise ValueError('invalid_context')
    rows = []
    seen = set()
    for c in candidates:
        if not isinstance(c, dict):
            raise ValueError('invalid_candidate')
        cid = c.get('candidate_id')
        revision = c.get('revision_sha256')
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError('duplicate_or_missing_id')
        if not isinstance(revision, str) or len(revision) != 64 or any(x not in '0123456789abcdef' for x in revision):
            raise ValueError('invalid_revision')
        if c.get('eligible') is not True:
            raise ValueError('ineligible_candidate')
        seen.add(cid)
        rows.append({'candidate_id': cid, 'revision_sha256': revision,
                     'excerpt': bounded(c.get('excerpt'), MAX_EXCERPT_CHARS)})
    edit = payload.get('context_origin') == 'tool_edit'
    if edit and sampling is None:
        raise ValueError('edit_sampling_required')
    result = {'schema_version': 4 if edit else
              (3 if 'candidate_limit' in sampling else 2) if sampling is not None else VERSION,
              'request_id': request_id, 'host': host,
              'context_origin': 'tool_edit' if edit else 'user_prompt',
              'prompt': bounded(payload['prompt'], MAX_EDIT_PROMPT_CHARS if edit else MAX_PROMPT_CHARS),
              'candidates': rows}
    if edit:
        result['edit_event'] = payload.get('edit_event')
    if sampling is not None:
        result['sampling'] = sampling
    validate_request(result)
    return result


def validate_label(label: dict, sample: dict) -> dict:
    validate_request(sample)
    if not isinstance(label, dict) or type(label.get('schema_version')) is not int or label.get('schema_version') not in (1, 2):
        raise ValueError('unknown_schema')
    if label.get('request_id') != sample['request_id']:
        raise ValueError('unknown_request')
    candidate = next((c for c in sample['candidates'] if c['candidate_id'] == label.get('candidate_id')), None)
    if candidate is None or label.get('revision_sha256') != candidate['revision_sha256']:
        raise ValueError('unknown_id_or_revision')
    abstain = label.get('abstain')
    relevance = label.get('relevance')
    if not isinstance(abstain, bool) or (abstain and relevance is not None) or (not abstain and (not isinstance(relevance, str) or relevance not in CLASSES)):
        raise ValueError('invalid_relevance')
    evidence = label.get('evidence', '')
    if not isinstance(evidence, str) or (evidence and evidence not in candidate['excerpt']['text']) or (label.get('schema_version') == 1 and not abstain and not evidence):
        raise ValueError('invalid_evidence')
    keys = {'schema_version', 'request_id', 'candidate_id', 'revision_sha256', 'abstain', 'relevance', 'evidence'}
    if label['schema_version'] == 2:
        keys.add('rationale')
        rationale = label.get('rationale')
        if not isinstance(rationale, dict) or set(rationale) != {'kind', 'text', 'span', 'scope'}:
            raise ValueError('invalid_rationale')
        if rationale['scope'] != 'provided-excerpt' or not isinstance(rationale['text'], str) or not rationale['text'].strip() or len(rationale['text']) > 600:
            raise ValueError('invalid_rationale_scope')
        allowed = {'uncertain'} if abstain else ({'support'} if relevance != 'irrelevant' else {'absence', 'contradiction'})
        if rationale['kind'] not in allowed:
            raise ValueError('invalid_rationale_kind')
        span = rationale['span']
        if evidence:
            if not isinstance(span, dict) or set(span) != {'start', 'end'} or type(span['start']) is not int or type(span['end']) is not int or not 0 <= span['start'] < span['end'] <= len(candidate['excerpt']['text']) or candidate['excerpt']['text'][span['start']:span['end']] != evidence:
                raise ValueError('invalid_evidence_span')
        elif span is not None or rationale['kind'] in {'support', 'contradiction'}:
            raise ValueError('evidence_span_required')
        if rationale['kind'] == 'absence' and (evidence or span is not None):
            raise ValueError('absence_requires_no_quote')
    if set(label) != keys:
        raise ValueError('invalid_label_fields')
    return {k: label[k] for k in sorted(keys)}


def _validate_request(sample: dict) -> None:
    if not isinstance(sample, dict) or type(sample.get('schema_version')) is not int or sample.get('schema_version') not in SUPPORTED_VERSIONS:
        raise ValueError('unknown_schema')
    keys = {'schema_version', 'request_id', 'host', 'context_origin', 'prompt', 'candidates'}
    if sample['schema_version'] in (2, 3, 4):
        keys.add('sampling')
    edit = sample['schema_version'] == 4
    if edit:
        keys.add('edit_event')
    if (set(sample) != keys or sample['context_origin'] !=
            ('tool_edit' if edit else 'user_prompt')):
        raise ValueError('invalid_request_fields')
    if sample['host'] not in {'claude', 'codex', 'hermes'} or not isinstance(sample['request_id'], str) or not sample['request_id']:
        raise ValueError('invalid_request')
    rows = sample['candidates']
    if not isinstance(rows, list) or len(rows) > (MAX_EXPERIMENT_CANDIDATES if sample['schema_version'] in (3, 4) else 16 if sample['schema_version'] == 2 else MAX_CANDIDATES):
        raise ValueError('invalid_candidates')
    if sample['schema_version'] in (2, 3, 4):
        validate_sampling(sample['sampling'], [c['candidate_id'] for c in rows], version=sample['schema_version'])
    if edit:
        event = sample['edit_event']
        required = {'ticket_id', 'parent_sha256', 'path_sha256', 'change_index', 'change_count',
                    'segment_sha256', 'start', 'end', 'index', 'count',
                    'session_id', 'turn_id', 'tool_use_id', 'operation'}
        if (not isinstance(event, dict) or
                set(event) not in (required, required | {'deferred_capture'}) or
                ('deferred_capture' in event and
                 type(event['deferred_capture']) is not bool) or
                not isinstance(event['ticket_id'], str) or len(event['ticket_id']) != 32 or
                any(char not in '0123456789abcdef' for char in event['ticket_id']) or
                any(not isinstance(event[key], str) or len(event[key]) != 64 or
                    any(char not in '0123456789abcdef' for char in event[key])
                    for key in ('parent_sha256', 'path_sha256', 'segment_sha256')) or
                any(type(event[key]) is not int for key in
                    ('start', 'end', 'index', 'count', 'change_index', 'change_count')) or
                not 0 <= event['start'] < event['end'] or
                not 0 <= event['index'] < event['count'] <= 64 or
                not 0 <= event['change_index'] < event['change_count'] <= 8 or
                event['segment_sha256'] != digest(sample['prompt']['text']) or
                event['operation'] not in ('write', 'edit', 'delete') or
                any(not isinstance(event[key], str) or not 1 <= len(event[key]) <= 160
                    for key in ('session_id', 'turn_id', 'tool_use_id'))):
            raise ValueError('invalid_edit_event')
    seen = set()
    for item, limit in [(sample['prompt'], MAX_EDIT_PROMPT_CHARS if edit else MAX_PROMPT_CHARS)] + [(c['excerpt'], MAX_EXCERPT_CHARS) for c in rows]:
        if set(item) != {'text', 'truncated', 'original_chars', 'sha256'} or not isinstance(item['text'], str):
            raise ValueError('invalid_bounded_text')
        original = item['original_chars']
        if type(original) is not int or original < 0 or type(item['truncated']) is not bool:
            raise ValueError('invalid_truncation')
        if len(item['text']) != min(original, limit) or item['truncated'] != (original > limit) or item['sha256'] != digest(item['text']):
            raise ValueError('invalid_text_provenance')
    if not sample['prompt']['text'].strip():
        raise ValueError('missing_prompt')
    for c in rows:
        if set(c) != {'candidate_id', 'revision_sha256', 'excerpt'}:
            raise ValueError('invalid_candidate_fields')
        cid = c['candidate_id']; revision = c['revision_sha256']
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError('duplicate_id')
        seen.add(cid)
        if not isinstance(revision, str) or len(revision) != 64 or any(x not in '0123456789abcdef' for x in revision):
            raise ValueError('invalid_revision')


def validate_request(sample: dict) -> None:
    try:
        _validate_request(sample)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed_request') from exc


def validate_sampling(meta, captured, *, version=2):
    keys = {'mode','scope','phase_counts','per_phase_limit','eligible_ids','captured_ids','baseline_selected_ids','excluded','snapshot_failures','retrieval_truncated','assessment_complete','complete_within_returned_bound','index_revision'}
    bound = 30 if version in (3, 4) else 8
    ids_bound = MAX_EXPERIMENT_CANDIDATES if version in (3, 4) else 16
    if version in (3, 4):
        keys.add('candidate_limit')
        if not isinstance(meta, dict) or type(meta.get('candidate_limit')) is not int or not 1 <= meta['candidate_limit'] <= MAX_EXPERIMENT_CANDIDATES:
            raise ValueError('invalid_candidate_limit')
    revision = meta.get('index_revision') if isinstance(meta, dict) else None
    if not isinstance(meta, dict) or set(meta) != keys or meta['mode'] != ('eligible-policy-pool' if version in (3, 4) else 'eligible-returned-pool') or meta['scope'] != 'queried-phases-only' or not (revision == 'unavailable' or isinstance(revision, str) and len(revision) == 64 and all(x in '0123456789abcdef' for x in revision)) or type(meta['per_phase_limit']) is not int or meta['per_phase_limit'] != bound:
        raise ValueError('invalid_sampling')
    phases = meta['phase_counts']
    if not isinstance(phases, dict) or not phases or not set(phases) <= {'primary','raw'} or any(type(v) is not int or v < 0 for v in phases.values()):
        raise ValueError('invalid_phase_counts')
    for name in ('eligible_ids','captured_ids','baseline_selected_ids'):
        values = meta[name]
        if not isinstance(values,list) or len(values)>(16 if name=='baseline_selected_ids' else ids_bound) or any(not isinstance(v,str) or not v for v in values) or len(set(values))!=len(values):
            raise ValueError('invalid_sampling_ids')
    if version in (3, 4) and len(meta['eligible_ids']) > meta['candidate_limit']:
        raise ValueError('candidate_limit_exceeded')
    if meta['captured_ids'] != captured or not set(captured) <= set(meta['eligible_ids']):
        raise ValueError('invalid_captured_membership')
    for name in ('excluded','snapshot_failures'):
        if not isinstance(meta[name],list) or len(meta[name])>bound*2:
            raise ValueError('invalid_exclusions')
        for row in meta[name]:
            expected = {'candidate_id','reason','phase'} if name=='excluded' else {'candidate_id','reason'}
            if not isinstance(row,dict) or set(row)!=expected or any(not isinstance(v,str) or not v for v in row.values()):
                raise ValueError('invalid_exclusion')
    for name in ('retrieval_truncated','assessment_complete','complete_within_returned_bound'):
        if type(meta[name]) is not bool:
            raise ValueError('invalid_sampling_flag')
    if meta['retrieval_truncated'] != any(v>bound for v in phases.values()):
        raise ValueError('hidden_retrieval_truncation')
    expected = meta['assessment_complete'] and not (version in (3, 4) and any(x['reason']=='candidate_budget' for x in meta['excluded'])) and not meta['retrieval_truncated'] and not meta['snapshot_failures'] and set(meta['eligible_ids'])==set(captured)
    if meta['complete_within_returned_bound'] != expected:
        raise ValueError('false_completeness')
