"""Opt-in actual local Laya protocol smoke; synthetic cards only."""
import json
import os
from pathlib import Path
import tempfile

from memvane.learning.context_learning.local_cycle import VerifiedLayaTrainer
from memvane.learning.context_learning.runtime_setup import attest_runtime, choose_runtime
from memvane.learning.context_learning.adapter import prepare_base_artifact
from memvane.learning.tests.support import REPO


python = os.environ['QMD_LAYA_PYTHON']
model = os.environ['QMD_LAYA_MODEL']
adapter = str(REPO/'memvane/learning/context_learning/adapter.py')
proof = attest_runtime(python, adapter, model)
selected = choose_runtime(mode='reuse', reuse_executable=python,
    attestation=proof['attestation'],
    base_model_sha256=proof['attestation']['base_model_sha256'])
assert selected['status'] == 'selected'

trainer = VerifiedLayaTrainer([python, adapter, model], max_seconds=120, max_rss_mib=8192)
with tempfile.TemporaryDirectory(prefix='qmd-real-laya-protocol-') as temp:
    root = Path(temp); root.chmod(0o700)
    base = root/'base.artifact'
    assert prepare_base_artifact(model,base)['base_model_sha256']==proof['attestation']['base_model_sha256']
    cases = []
    for i in range(2):
        cases.append({'request_id':'synthetic-'+str(i),'input_sha256':str(i)*64,
            'prompt':'Which synthetic town has the blue bridge?',
            'candidates':[{'id':'blue-'+str(i),'input_kind':'full_compact_wiki_body',
                'input_text':'Synthetic Arin has a blue bridge. '*25,
                'revision_sha256':'a'*64},
                {'id':'red-'+str(i),'input_kind':'full_compact_wiki_body',
                'input_text':'Synthetic Bori has a red bridge. '*25,
                'revision_sha256':'b'*64}],
            'selected_ids':['blue-'+str(i)] if i == 0 else []})
    artifact = root/'trained.artifact'
    trained, train_resources = trainer.call('train',
        {'schema_version':1,'train':cases,'artifact_path':str(artifact),
         'manifest_sha256':'c'*64},root/'train-result.json',cwd=root)
    assert trained == {'schema_version':1,'status':'trained'}
    assert artifact.is_file() and artifact.stat().st_size > 0
    heldout = [{key:value for key,value in case.items() if key != 'selected_ids'}
               for case in cases]
    for name, path in (('base',base),('trained',artifact)):
        prediction, resources = trainer.call('predict',
            {'schema_version':1,'artifact_path':str(path),'cases':heldout},
            root/(name+'-prediction.json'),cwd=root)
        assert len(prediction['predictions']) == 2
        for case, row in zip(heldout,prediction['predictions']):
            assert row['request_id'] == case['request_id']
            assert row['input_sha256'] == case['input_sha256']
            assert len(row['selected_ids']) <= 3
            assert set(row['selected_ids']) <= {c['id'] for c in case['candidates']}
        assert resources['elapsed_seconds'] > 0
    print(json.dumps({'external_calls':0,'runtime_selected':selected['mode'],
        'synthetic_proof':proof['smoke']['checks'],'device':proof['smoke']['device'],
        'body_bytes':proof['smoke']['synthetic_body_bytes'],
        'input_tokens':proof['smoke']['model_input_tokens'],
        'head_bytes':artifact.stat().st_size,'smoke_resources':proof['resources'],
        'train_resources':train_resources}))
