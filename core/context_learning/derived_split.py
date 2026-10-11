"""Read-only admission for an immutable, source-disjoint real-edit split.

This is an input adapter for isolated training. It never changes the original
case split/family columns and never sends a case to a model or gold provider.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .store import canonical


SCHEMA = 'qmd-derived-edit-split-manifest-v2'
CASE_KEYS = {'request_id', 'input_sha256', 'proposed_split', 'original_split',
             'original_family_keys_sha256', 'excluded_common_episode0_key',
             'ticket_id', 'edit_path', 'before_sha256', 'after_sha256',
             'operation', 'candidate_lineage', 'candidate_bodies_sha256',
             'gold_selected_ids', 'derived_family_keys'}
TOP_KEYS = {'schema', 'plan_sha256', 'source_audit_sha256',
            'source_dev_screen_sha256', 'cases', 'coverage',
            'derived_cross_split_family_overlap', 'historical_case_rows_modified',
            'balanced_development_available', 'training_embargo',
            'training_embargo_reason', 'excluded_gold_case_ids',
            'sealed_holdout_case_ids'}
HEX = re.compile(r'[0-9a-f]{64}\Z')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('derived_source_missing_or_unsafe')
    return path.read_bytes()


def _parse(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('derived_source_invalid_json') from exc


def _unique_ids(rows, key):
    if (not isinstance(rows, list) or not all(isinstance(row, dict)
            and isinstance(row.get(key), str) for row in rows)):
        raise ValueError('derived_case_set_invalid')
    result = [row[key] for row in rows]
    if len(result) != len(set(result)):
        raise ValueError('derived_case_set_invalid')
    return result


def validate_structure(manifest, plan, audit, screen):
    """Reject edited assignments, omitted exclusions, and family overlap."""
    if (not isinstance(manifest, dict) or set(manifest) != TOP_KEYS
            or manifest['schema'] != SCHEMA
            or manifest['derived_cross_split_family_overlap'] != []
            or manifest['historical_case_rows_modified'] is not False
            or type(manifest['training_embargo']) is not bool
            or not isinstance(manifest['training_embargo_reason'], str)
            or not isinstance(plan, dict) or plan.get('schema') != 'qmd-derived-edit-split-plan-v2'
            or not isinstance(audit, dict) or not isinstance(screen, dict)):
        raise ValueError('derived_manifest_invalid')
    cases = manifest['cases']
    ids = _unique_ids(cases, 'request_id')
    if ids != sorted(ids) or any(set(row) != CASE_KEYS for row in cases):
        raise ValueError('derived_case_set_invalid')
    assignments = plan.get('case_assignments')
    assignment_ids = _unique_ids(assignments, 'request_id')
    if (set(ids) != set(assignment_ids) or any(
            row['request_id'] != assignment['request_id']
            or row['input_sha256'] != assignment['input_sha256']
            or row['proposed_split'] != assignment['split']
            for row, assignment in zip(cases, sorted(assignments,
                key=lambda item: item['request_id'])))):
        raise ValueError('derived_assignment_changed')
    excluded = manifest['excluded_gold_case_ids']
    sealed = manifest['sealed_holdout_case_ids']
    if (not isinstance(excluded, list) or not isinstance(sealed, list)
            or len(excluded) != len(set(excluded))
            or len(sealed) != len(set(sealed))
            or excluded != plan.get('excluded_gold_case_ids')
            or sealed != plan.get('sealed_holdout_case_ids')
            or set(excluded) & (set(ids) | set(sealed))
            or set(sealed) & set(ids)
            or set(sealed) != set(audit.get('frozen_holdouts', {}))):
        raise ValueError('derived_exclusion_changed')
    if not set(plan.get('new_gold_empty_case_ids', [])) <= set(ids):
        raise ValueError('derived_exclusion_changed')
    keys = {'training': set(), 'development': set()}
    counts = {'training': 0, 'development': 0,
              'development_positive': 0, 'development_gold_empty': 0}
    for row in cases:
        split = row['proposed_split']
        families = row['derived_family_keys']
        if (split not in keys or not isinstance(families, list)
                or not families or len(families) != len(set(families))
                or families != sorted(families)
                or not all(isinstance(key, str) and key.startswith((
                    'edit-lineage:', 'candidate-source-episode:', 'question:'))
                    and HEX.fullmatch(key.rsplit(':', 1)[-1]) for key in families)
                or not isinstance(row['gold_selected_ids'], list)):
            raise ValueError('derived_family_invalid')
        keys[split].update(families)
        counts[split] += 1
        if split == 'development':
            counts['development_positive' if row['gold_selected_ids']
                   else 'development_gold_empty'] += 1
    if (not counts['training'] or not counts['development']
            or keys['training'] & keys['development']):
        raise ValueError('derived_family_overlap')
    balanced = (counts['development_positive'] >= 2
                and counts['development_gold_empty'] >= 2)
    if (manifest['coverage'] != counts
            or manifest['balanced_development_available'] is not balanced):
        raise ValueError('derived_coverage_changed')
    return cases


def _lineage(db, story, sample):
    lineage = []
    for candidate in sample['candidates']:
        cid = candidate['candidate_id']
        collection, sep, basename = cid.partition('/')
        if (collection != 'fantasy-20261008-wiki' or not sep or '/' in basename
                or basename in ('', '.', '..')):
            raise ValueError('derived_candidate_scope_changed')
        relative = '.auto-context/wiki/' + basename
        source = story / relative
        if _sha(_file(source)) != candidate['revision_sha256']:
            raise ValueError('derived_candidate_revision_changed')
        matches = db.execute('SELECT DISTINCT episode FROM experiment_generated '
                             'WHERE relative_path=? AND sha256=?',
                             (relative, candidate['revision_sha256'])).fetchall()
        if len(matches) != 1 or type(matches[0][0]) is not int or matches[0][0] < 1:
            raise ValueError('derived_candidate_lineage_changed')
        lineage.append({'candidate_id': cid,
                        'revision_sha256': candidate['revision_sha256'],
                        'generated_episode': matches[0][0]})
    return lineage


def _admit_active_case(root, project, rid, sample_json, sample, bodies, scope):
    from . import shared
    from .synthetic_scope import allowed_case
    if (not shared._case_active(root, (str(project), rid, sample_json),
                                create_private_state=False)
            or allowed_case(root, project, sample, bodies)[0] != scope):
        raise ValueError('derived_case_consent_or_source_changed')


def _check_question_family(sample, original_families):
    question = 'question:' + _sha(' '.join(
        sample['prompt']['text'].casefold().split()).encode())
    if {key for key in original_families if key.startswith('question:')} != {question}:
        raise ValueError('derived_question_family_changed')


def _check_gold_evidence(rid, input_sha, first_json, second_json, gold_json,
                         receipts, calls):
    first, second, gold = map(json.loads, (first_json, second_json, gold_json))
    if (set(first['selected_ids']) != set(second['selected_ids'])
            or gold != {'selected_ids': first['selected_ids'],
                        'input_sha256': input_sha,
                        'review': 'synthetic_independent_exact_agreement',
                        'first_usage': first.get('usage', {}),
                        'second_usage': second.get('usage', {})}):
        raise ValueError('derived_gold_or_capture_changed')
    expected = {
        f'gold:{rid}:first_running': _sha(first_json.encode()),
        f'gold:{rid}:second_running': _sha(second_json.encode()),
    }
    if ({row[0] for row in receipts} != set(expected)
            or {row[0] for row in calls} != set(expected)
            or any((stage, error, received_input, answer_sha) !=
                   ('committed', None, input_sha, expected[call_key])
                   for call_key, stage, error, received_input, answer_sha in receipts)):
        raise ValueError('derived_receipts_changed')


def prepare(root, project, story, manifest_path, manifest_sha256, source_paths):
    from .shared import read_only_validation
    with read_only_validation():
        return _prepare(root, project, story, manifest_path,
                        manifest_sha256, source_paths)


def _prepare(root, project, story, manifest_path, manifest_sha256, source_paths):
    """Return exact isolated train/dev payloads after checking live provenance.

    The caller must pin all four input files. The source files establish the
    frozen case universe; the manifest SHA pins their computed split.
    """
    from . import isolated_train, shared
    from .edit_ticket import validate_case
    from .synthetic_scope import policy_for

    root, project, story = (Path(path).resolve(strict=True)
                            for path in (root, project, story))
    raw = _file(manifest_path)
    if not isinstance(manifest_sha256, str) or _sha(raw) != manifest_sha256:
        raise ValueError('derived_manifest_sha_changed')
    if set(source_paths) != {'plan', 'audit', 'screen'}:
        raise ValueError('derived_source_set_invalid')
    source_raw = {key: _file(source_paths[key]) for key in source_paths}
    manifest = _parse(raw)
    if (manifest.get('plan_sha256') != _sha(source_raw['plan'])
            or manifest.get('source_audit_sha256') != _sha(source_raw['audit'])
            or manifest.get('source_dev_screen_sha256') != _sha(source_raw['screen'])):
        raise ValueError('derived_source_sha_changed')
    plan, audit, screen = (_parse(source_raw[key]) for key in ('plan', 'audit', 'screen'))
    if (plan.get('source_audit_sha256') != manifest['source_audit_sha256']
            or plan.get('source_dev_screen_sha256') != manifest['source_dev_screen_sha256']):
        raise ValueError('derived_plan_source_changed')
    rows = validate_structure(manifest, plan, audit, screen)
    audit_cases = {row['request_id']: row for row in audit['cases']}
    screen_cases = {row['request_id']: row for row in screen['cases']
                    if row.get('request_id') in plan['new_gold_empty_case_ids']}
    expected = {**audit_cases, **screen_cases}
    if (set(expected) != {row['request_id'] for row in rows}
            or set(screen_cases) != set(plan['new_gold_empty_case_ids'])
            or any(not row['eligible'] or row['status'] != 'gold'
                   or row['gold_selected_ids'] != [] or row['gold_calls'] != 2
                   for row in screen_cases.values())):
        raise ValueError('derived_frozen_case_universe_changed')
    scope = policy_for(root, project)
    db_path = root / shared.DATABASE_NAME
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError('derived_database_missing')
    payloads = {'training': [], 'development': []}
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1)) as db:
        selected_ids = {item['request_id'] for item in rows}
        all_active_gold = {row[0] for row in db.execute(
            "SELECT request_id FROM cases WHERE project_root=? AND status='gold' "
            "AND use_state='active'", (str(project),))}
        if not selected_ids <= all_active_gold:
            raise ValueError('derived_case_set_changed')
        omitted_gold = sorted(all_active_gold - selected_ids)
        if not (set(manifest['excluded_gold_case_ids'])
                | set(manifest['sealed_holdout_case_ids'])) <= set(omitted_gold):
            raise ValueError('derived_exclusion_changed')
        sealed_candidates, sealed_episodes, sealed_questions = set(), set(), set()
        for rid in manifest['excluded_gold_case_ids']:
            excluded = db.execute('SELECT project_root,status,use_state,input_sha256,sample,gold '
                                  'FROM cases WHERE request_id=?', (rid,)).fetchone()
            if (excluded is None or excluded[:3] != (str(project), 'gold', 'active')
                    or _sha(excluded[4].encode()) != excluded[3]
                    or json.loads(excluded[5])['input_sha256'] != excluded[3]):
                raise ValueError('derived_excluded_case_changed')
        for rid in manifest['sealed_holdout_case_ids']:
            held = db.execute('SELECT split,status,use_state,input_sha256,gold,sample,family_keys '
                              'FROM cases WHERE request_id=?', (rid,)).fetchone()
            pin = audit['frozen_holdouts'][rid]
            if (held is None or held[:4] != (pin['split'], pin['status'],
                                             pin['use_state'], pin['input_sha256'])
                    or _sha(held[4].encode()) != pin['gold_sha256']
                    or _sha(held[5].encode()) != held[3]):
                raise ValueError('derived_sealed_case_changed')
            sample = json.loads(held[5]); families = json.loads(held[6])
            question = 'question:' + _sha(' '.join(
                sample['prompt']['text'].casefold().split()).encode())
            if question not in families:
                raise ValueError('derived_sealed_family_changed')
            sealed_candidates.update(c['candidate_id'] for c in sample['candidates'])
            sealed_episodes.update(item['generated_episode']
                                   for item in _lineage(db, story, sample))
            sealed_questions.update(key for key in families if key.startswith('question:'))
        for item in rows:
            rid = item['request_id']; pin = expected[rid]
            case = db.execute('SELECT project_key,project_root,input_sha256,sample,bodies,'
                              'gold,split,status,use_state,family_keys,first,second '
                              'FROM cases WHERE request_id=?', (rid,)).fetchone()
            if (case is None or case[1] != str(project)
                    or case[2] != item['input_sha256']
                    or case[7:9] != ('gold', 'active')
                    or case[6] != item['original_split']):
                raise ValueError('derived_case_changed')
            key, _, input_sha, sample_json, bodies_json, gold_json, _, _, _, families_json, first, second = case
            sample, bodies, gold = map(json.loads, (sample_json, bodies_json, gold_json))
            if (_sha(sample_json.encode()) != input_sha
                    or _sha(families_json.encode()) != item['original_family_keys_sha256']
                    or _sha(bodies_json.encode()) != item['candidate_bodies_sha256']
                    or gold['selected_ids'] != item['gold_selected_ids']):
                raise ValueError('derived_gold_or_capture_changed')
            original_families = set(json.loads(families_json))
            _check_question_family(sample, original_families)
            false_episode = 'episode:' + _sha(canonical([key, 0]).encode())
            edit_target = 'target:' + _sha(canonical([key, 'edit:' + item['edit_path']]).encode())
            if (false_episode != item['excluded_common_episode0_key']
                    or false_episode not in original_families
                    or edit_target not in original_families):
                raise ValueError('derived_historical_family_changed')
            event = sample['edit_event']
            ticket = db.execute('SELECT relative_path,episode,before_sha256,after_sha256,'
                                'operation,request_id,input_sha256,candidate_sha256,event_json '
                                'FROM edit_tickets WHERE ticket_id=?', (item['ticket_id'],)).fetchone()
            candidate_sha = _sha(canonical([[c['candidate_id'], c['revision_sha256']]
                                            for c in sample['candidates']]).encode())
            if (ticket is None or ticket[:7] != (item['edit_path'], 0,
                    item['before_sha256'], item['after_sha256'], item['operation'],
                    rid, input_sha) or ticket[7] != candidate_sha
                    or event != {**json.loads(ticket[8]), 'ticket_id': item['ticket_id']}
                    or _sha(_file(story / item['edit_path'])) != item['after_sha256']
                    or item['after_sha256'] != pin['source_sha256']
                    or item['ticket_id'] != pin['ticket_id']):
                raise ValueError('derived_edit_lineage_changed')
            validated = validate_case(db, root, project, sample, bodies)
            _admit_active_case(root, project, rid, sample_json, sample, bodies, scope)
            lineage = _lineage(db, story, sample)
            if (lineage != item['candidate_lineage']
                    or set(bodies) != {part['candidate_id'] for part in lineage}
                    or any(part['candidate_id'] in sealed_candidates
                           or part['generated_episode'] in sealed_episodes
                           for part in lineage)
                    or sealed_questions & {key for key in original_families
                                            if key.startswith('question:')}):
                raise ValueError('derived_candidate_lineage_changed')
            families = {'edit-lineage:' + _sha(canonical([key, item['edit_path']]).encode())}
            families.update('candidate-source-episode:' + _sha(canonical(
                [key, part['generated_episode']]).encode()) for part in lineage)
            families.update(key for key in original_families if key.startswith('question:'))
            if sorted(families) != item['derived_family_keys']:
                raise ValueError('derived_family_changed')
            receipts = db.execute('SELECT call_key,stage,error_code,input_sha256,answer_sha256 '
                                  'FROM gold_attempt_receipts '
                                  'WHERE call_key LIKE ?', (f'gold:{rid}:%',)).fetchall()
            calls = db.execute('SELECT call_key FROM experiment_calls WHERE kind=? '
                               'AND call_key LIKE ?', ('gold', f'gold:{rid}:%')).fetchall()
            _check_gold_evidence(rid, input_sha, first, second,
                                 gold_json, receipts, calls)
            shared._case((key, rid, input_sha, sample_json, bodies_json, gold_json))
            provenance = {'validated_ticket': list(validated),
                          'before_sha256': item['before_sha256'],
                          'edit_event': event}
            payloads[item['proposed_split']].append({**shared._case(
                (key, rid, input_sha, sample_json, bodies_json, gold_json)),
                'capture_provenance': provenance})
    for value in payloads.values():
        value.sort(key=lambda case: case['request_id'])
    train = isolated_train._train_cases_payload(payloads['training'])
    dev = isolated_train._predict_cases_payload(payloads['development'])
    return {'manifest': manifest, 'manifest_sha256': manifest_sha256,
            'train': payloads['training'], 'development': payloads['development'],
            'omitted_active_gold_case_ids': omitted_gold,
            'train_payload_sha256': _sha(canonical(train).encode()),
            'development_payload_sha256': _sha(canonical(dev).encode()),
            'source_sha256': {key: _sha(raw) for key, raw in source_raw.items()}}
