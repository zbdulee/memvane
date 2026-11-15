"""Pin complete reviewed question families before local selection training.

The manifest remains the authority for family splits. Only train records expose
gold to the trainer; held-out requests become unlabeled inference inputs.
"""
import json

from .contracts import digest, validate_label
from .compact_input import MAX_COMPACT_BODY_BYTES, MAX_SOURCE_BYTES
from .dataset import _manifest, _snapshot
from .offline import SPLITS
from .store import canonical, database


def build_queue(state_dir, version_id, current_revisions):
    if not isinstance(current_revisions, dict):
        raise ValueError('invalid_revisions')
    with database(state_dir) as db:
        manifest, manifest_sha = _manifest(db, version_id)
        if manifest['schema_version'] != 2:
            raise ValueError('split_provenance_required')
        _snapshot(db, version_id, current_revisions)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_gold'").fetchone():
            raise ValueError('missing_selection_gold')
        members = db.execute('SELECT request_id,candidate_id FROM manifest_cases WHERE version_id=?', (version_id,)).fetchall()
        split_by_request = {}
        for (rid, cid), pinned in zip(sorted(members), manifest['cases']):
            if pinned['case_hash'] != digest(canonical([rid, cid])):
                raise ValueError('manifest_membership_mismatch')
            old = split_by_request.setdefault(rid, pinned['split'])
            if old != pinned['split']:
                raise ValueError('request_split_conflict')
        if len(members) != len(manifest['cases']):
            raise ValueError('manifest_membership_mismatch')
        result = {name: [] for name in sorted(SPLITS)}
        for rid, split in sorted(split_by_request.items()):
            row = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()
            gold = db.execute('SELECT body,review FROM selection_gold WHERE request_id=?', (rid,)).fetchone()
            if row is None or gold is None:
                raise ValueError('missing_reviewed_request')
            sample = json.loads(row[0]); choice = json.loads(gold[0]); review = json.loads(gold[1])
            if sample['prompt']['truncated']:
                raise ValueError('incomplete_model_input')
            if sample['schema_version'] >= 2 and not sample['sampling']['complete_within_returned_bound']:
                raise ValueError('incomplete_candidate_pool')
            if choice.get('input_sha256') != digest(canonical(sample)) or review.get('selection_sha256') != digest(gold[0]):
                raise ValueError('selection_changed')
            candidates = []
            for candidate in sample['candidates']:
                cid = candidate['candidate_id']
                if (rid, cid) not in members:
                    raise ValueError('incomplete_reviewed_pool')
                if current_revisions.get(cid) != candidate['revision_sha256']:
                    raise ValueError('stale_source')
                label_row = db.execute('SELECT body,review FROM labels WHERE request_id=? AND candidate_id=?', (rid, cid)).fetchone()
                if not label_row or not label_row[1]:
                    raise ValueError('missing_reviewed_label')
                label = validate_label(json.loads(label_row[0]), sample)
                if label['abstain']:
                    raise ValueError('abstained_label')
                compact_row = db.execute('SELECT body FROM compact_inputs WHERE request_id=? AND candidate_id=?',
                    (rid, cid)).fetchone()
                if compact_row:
                    compact = json.loads(compact_row[0])
                    if set(compact) != {'input_kind','source_sha256','source_bytes','body_sha256','body_text','truncated'} or compact['input_kind'] != 'full_compact_wiki_body' or compact['source_sha256'] != candidate['revision_sha256'] or compact['truncated'] is not False or type(compact['source_bytes']) is not int or compact['source_bytes'] > MAX_SOURCE_BYTES or not isinstance(compact['body_text'], str) or not compact['body_text'].strip() or len(compact['body_text'].encode()) > MAX_COMPACT_BODY_BYTES or compact['body_sha256'] != digest(compact['body_text']):
                        raise ValueError('compact_input_integrity_mismatch')
                    input_text = compact['body_text']; input_kind = compact['input_kind']
                elif sample['schema_version'] == 1 and not candidate['excerpt']['truncated']:
                    input_text = candidate['excerpt']['text']; input_kind = 'legacy_complete_excerpt'
                else:
                    raise ValueError('missing_full_compact_body')
                candidates.append({'id': cid, 'revision_sha256': candidate['revision_sha256'],
                    'input_text': input_text, 'input_kind': input_kind,
                    'relevance': label['relevance']})
            selected = choice['selected_ids']
            if not isinstance(selected, list) or len(selected) > 3 or not set(selected) <= {c['id'] for c in candidates}:
                raise ValueError('invalid_selection_gold')
            # A selected irrelevant card is a review conflict; do not train through it.
            if any(c['id'] in selected and c['relevance'] == 'irrelevant' for c in candidates):
                raise ValueError('selection_pair_conflict')
            result[split].append({'request_id': rid, 'input_sha256': choice['input_sha256'],
                'prompt': sample['prompt']['text'], 'prompt_truncated': sample['prompt']['truncated'],
                'candidates': candidates, 'selected_ids': selected,
                'family_hashes': sorted({k for p in manifest['cases'] if p['case_hash'] in
                    {digest(canonical([rid, c['id']])) for c in candidates} for k in p['split_family_hashes']})})
        return {'schema_version': 1, 'manifest_sha256': manifest_sha, 'version_id': version_id,
                'splits': result, 'queue_sha256': digest(canonical(result))}
