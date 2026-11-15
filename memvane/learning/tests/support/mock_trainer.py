"""Synthetic local trainer protocol; no model, network or private data."""
import json
import os
from pathlib import Path
import sys


def write(path, body):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out: json.dump(body, out)


mode, request_path, response_path = sys.argv[1:]
request = json.loads(Path(request_path).read_text())
if mode == 'train':
    assert len(request['train']) >= 100
    assert not any('validation' in x or 'evaluation' in x for x in request)
    if Path('fail-once').exists() and not Path('failed-once').exists():
        Path('failed-once').write_text('synthetic failure')
        sys.exit(2)
    write(request['artifact_path'], {'model': 'synthetic-lexical-choice'})
    write(response_path, {'schema_version': 1, 'status': 'trained'})
elif mode == 'predict':
    model = json.loads(Path(request['artifact_path']).read_text())['model']
    predictions = []
    for case in request['cases']:
        assert 'selected_ids' not in case
        assert all('relevance' not in c for c in case['candidates'])
        selected = [case['candidates'][0]['id']] if model == 'synthetic-lexical-choice' and 'wanted' in case['prompt'] else []
        predictions.append({'request_id': case['request_id'], 'input_sha256': case['input_sha256'], 'selected_ids': selected})
    write(response_path, {'schema_version': 1, 'predictions': predictions})
else: sys.exit(3)
