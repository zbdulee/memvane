"""Published-card review keeps every candidate after the old eight-card limit."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import wiki_topical_publish as publisher
import wiki_topical_similarity as similarity
import wiki_topical_experiment as experiment
import wiki_topical as topical

with tempfile.TemporaryDirectory(prefix='qmd-nine-publish-') as name:
    root = Path(name).resolve()
    wiki = root / '.auto-context/wiki'
    source = root / 'source.md'
    source.write_text('Synthetic source only.\n')
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    generation = 'a' * 24
    proposed = {'cardId': 'new-card', 'lead': 'Synthetic staged card.',
                'sourceRevisions': []}
    staged = b'Synthetic staged card.\n'
    for index in range(9):
        gid = format(index + 1, '024x')
        page = wiki / 'topical-v2' / gid / f'other-{index}.md'
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(b'Synthetic published card.\n')
    seen = []
    def read_generation(_root, gid, rel):
        if rel.endswith('.evidence.json'):
            return json.dumps({'sourceRevisions': [{
                'path': 'source.md', 'sha256': source_sha}]}).encode()
        return f'Synthetic canonical {gid}.\n'.encode()
    def evaluate(_root, _card, _gid, matches):
        seen.extend(matches)
        return {'status': 'distinct_or_no_candidates',
                'pairs': [{'pairSha256': str(i)} for i in range(len(matches))]}
    with patch.object(publisher, '_project', return_value=(root, wiki, 'synthetic')), \
         patch.object(publisher, '_attested', return_value={'status': 'backend_pass'}), \
         patch.object(publisher, 'canonical_published_card', return_value=staged), \
         patch.object(experiment, 'load_staged_card', return_value=proposed), \
         patch.object(topical, 'read_generation_bytes', side_effect=read_generation), \
         patch.object(similarity, 'evaluate', side_effect=evaluate):
        result = publisher._publication_similarity(root, wiki, generation, 'new-card')
    assert result['status'] == 'distinct_or_no_candidates'
    assert len(seen) == 9
print(json.dumps({'allNineCompared': True}))
