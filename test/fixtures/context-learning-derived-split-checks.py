"""Synthetic, read-only admission failures; no runtime, host, or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from context_learning import derived_split, derived_train
from context_learning import shared, synthetic_scope
from context_learning.seam import _private_state
from context_learning.store import canonical


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def rejects(call, reason):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == reason, (str(exc), reason)
    else:
        raise AssertionError('changed split reached admission')


def case(rid, split, family):
    return {'request_id': rid, 'input_sha256': sha(rid),
            'proposed_split': split, 'original_split': 'train',
            'original_family_keys_sha256': sha('original-' + rid),
            'excluded_common_episode0_key': 'episode:' + sha('zero'),
            'ticket_id': rid, 'edit_path': '01_Settings/qmd-probes/' + rid + '.md',
            'before_sha256': None, 'after_sha256': sha('after-' + rid),
            'operation': 'write', 'candidate_lineage': [],
            'candidate_bodies_sha256': sha('bodies-' + rid),
            'gold_selected_ids': [] if split == 'development' else ['wiki/a.md'],
            'derived_family_keys': ['edit-lineage:' + sha(family)]}


rows = [case('a', 'training', 'train'), case('b', 'development', 'dev')]
plan = {'schema': 'qmd-derived-edit-split-plan-v2',
        'case_assignments': [{'request_id': row['request_id'],
                              'input_sha256': row['input_sha256'],
                              'split': row['proposed_split']} for row in rows],
        'excluded_gold_case_ids': ['excluded'], 'sealed_holdout_case_ids': ['sealed'],
        'new_gold_empty_case_ids': []}
audit = {'frozen_holdouts': {'sealed': {}}}
screen = {'cases': []}
manifest = {'schema': derived_split.SCHEMA,
            'plan_sha256': sha('plan'), 'source_audit_sha256': sha('audit'),
            'source_dev_screen_sha256': sha('screen'),
            'cases': rows, 'coverage': {'training': 1, 'development': 1,
                                       'development_positive': 0,
                                       'development_gold_empty': 1},
            'derived_cross_split_family_overlap': [],
            'historical_case_rows_modified': False,
            'balanced_development_available': False,
            'training_embargo': True, 'training_embargo_reason': 'review pending',
            'excluded_gold_case_ids': ['excluded'],
            'sealed_holdout_case_ids': ['sealed']}
assert derived_split.validate_structure(manifest, plan, audit, screen) == rows

changed = copy.deepcopy(manifest)
changed['cases'][0]['input_sha256'] = sha('tampered')
rejects(lambda: derived_split.validate_structure(changed, plan, audit, screen),
        'derived_assignment_changed')

changed = copy.deepcopy(manifest)
changed['cases'][1]['derived_family_keys'] = changed['cases'][0]['derived_family_keys']
rejects(lambda: derived_split.validate_structure(changed, plan, audit, screen),
        'derived_family_overlap')

changed = copy.deepcopy(manifest)
changed['excluded_gold_case_ids'] = []
rejects(lambda: derived_split.validate_structure(changed, plan, audit, screen),
        'derived_exclusion_changed')

changed = copy.deepcopy(manifest)
changed['coverage']['development_gold_empty'] = 0
rejects(lambda: derived_split.validate_structure(changed, plan, audit, screen),
        'derived_coverage_changed')

pins = {'trainPayloadSha256': sha('train'),
        'developmentPayloadSha256': sha('development'),
        'omittedActiveGoldCaseIds': ['excluded', 'sealed']}
payload = {'train_payload_sha256': sha('train'),
           'development_payload_sha256': sha('development'),
           'omitted_active_gold_case_ids': ['excluded', 'sealed']}
derived_train._check_payload_pins(payload, pins)
rejects(lambda: derived_train._check_payload_pins({**payload,
        'development_payload_sha256': sha('changed')}, pins),
        'derived_payload_mapping_mismatch')
rejects(lambda: derived_train._check_payload_pins({**payload,
        'omitted_active_gold_case_ids': ['excluded']}, pins),
        'derived_payload_mapping_mismatch')

with patch.object(shared, '_case_active', return_value=False):
    rejects(lambda: derived_split._admit_active_case('/tmp/root', '/tmp/story',
        'a', '{}', {}, {}, 'scope'), 'derived_case_consent_or_source_changed')
with (patch.object(shared, '_case_active', return_value=True),
      patch.object(synthetic_scope, 'allowed_case',
                   side_effect=ValueError('synthetic_capture_not_from_source'))):
    rejects(lambda: derived_split._admit_active_case('/tmp/root', '/tmp/story',
        'a', '{}', {}, {}, 'scope'), 'synthetic_capture_not_from_source')

normalized = 'question:' + sha('same question')
derived_split._check_question_family({'prompt': {'text': ' Same  QUESTION '}},
                                     {normalized})
rejects(lambda: derived_split._check_question_family(
    {'prompt': {'text': ' Same  QUESTION '}}, {'question:' + sha('wrong')}),
    'derived_question_family_changed')

first = canonical({'selected_ids': ['wiki/a.md'], 'usage': {'input_tokens': 3}})
second = canonical({'selected_ids': ['wiki/a.md'], 'usage': {'input_tokens': 4}})
gold = canonical({'selected_ids': ['wiki/a.md'], 'input_sha256': sha('input'),
                  'review': 'synthetic_independent_exact_agreement',
                  'first_usage': {'input_tokens': 3},
                  'second_usage': {'input_tokens': 4}})
rid = 'request'
receipts = [(f'gold:{rid}:first_running', 'committed', None, sha('input'), sha(first)),
            (f'gold:{rid}:second_running', 'committed', None, sha('input'), sha(second))]
calls = [(row[0],) for row in receipts]
derived_split._check_gold_evidence(rid, sha('input'), first, second, gold,
                                   receipts, calls)
bad_receipts = list(receipts)
bad_receipts[0] = (*bad_receipts[0][:4], sha('wrong answer'))
rejects(lambda: derived_split._check_gold_evidence(rid, sha('input'), first,
        second, gold, bad_receipts, calls), 'derived_receipts_changed')
bad_gold = canonical({**json.loads(gold), 'first_usage': {}})
rejects(lambda: derived_split._check_gold_evidence(rid, sha('input'), first,
        second, bad_gold, receipts, calls), 'derived_gold_or_capture_changed')

with tempfile.TemporaryDirectory(prefix='qmd-derived-split-', dir='/tmp') as tmp:
    root = Path(tmp)
    for name in ('project', 'story', 'shared'):
        (root / name).mkdir()
    (root / 'private').mkdir(mode=0o700)
    config = {'contextLearning': {'stateRoot': str(root / 'private')},
              'collectionPaths': {'synthetic': '01_Settings'}}
    assert _private_state(config, root / 'project', create=False) is None
    assert list((root / 'private').iterdir()) == []
    path = root / 'manifest.json'
    path.write_text(json.dumps(manifest))
    rejects(lambda: derived_split.prepare(root / 'shared', root / 'project',
        root / 'story', path, sha('wrong'), {}), 'derived_manifest_sha_changed')

print(json.dumps({'validated': True, 'rejectedMutations': 12,
                  'trainingCalls': 0}))
