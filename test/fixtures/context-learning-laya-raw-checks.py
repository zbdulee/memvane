"""Pinned raw adapter tests, no Laya/tokenizer/torch/model/provider execution."""
import copy
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,'core')
fixture=runpy.run_path('test/fixtures/context-learning-dataset-checks.py')
from memvane.learning.context_learning.contracts import CLASS_ORDER, digest, request
from memvane.learning.context_learning.store import canonical, capture, database
from memvane.learning.context_learning.offline import approve_review, build_manifest, delete_case, save_label
from memvane.learning.context_learning.dataset import export_dataset, evaluate_predictions
from memvane.learning.context_learning.raw import (FORMAT, COMMIT, candidate_pin, tokenization_status,
    export_laya_raw, check_laya_raw, evaluate_laya_predictions, map_laya_predictions, _raw_payloads)
from memvane.learning.context_learning import dataset

class Checks(unittest.TestCase):
    setUp=fixture['Checks'].setUp
    tearDown=fixture['Checks'].tearDown
    write_json=fixture['Checks'].write_json
    command=fixture['Checks'].command
    predictions=fixture['Checks'].predictions

    def raw_predictions(self,metadata,split='evaluation'):
        sidecars=[json.loads(line) for line in (Path(metadata['directory'])/'provenance.jsonl').read_text().splitlines()]
        records=[]
        for row in sidecars:
            if row['split']!=split:continue
            name=row['target_class_name']
            records.append({k:row[k] for k in ('case_hash','input_sha256','state_sha256','questions_sha256')})
            records[-1]['answer']={'type':'choice','choice':name,
                'probabilities':{c:float(c==name) for c in reversed(CLASS_ORDER)},'answer_confidence':1.0,
                'confidence':1.0,'action':{'act_probability':0.1}}
        return {'schema_version':1,'format':FORMAT,'candidate':candidate_pin(),
            'laya_export_sha256':metadata['laya_export_sha256'],'split':split,
            'model':{'model_id':'synthetic-laya-not-real-weights','artifact_sha256':digest('synthetic-weights'),
                'training_manifest_sha256':metadata['manifest_sha256'],'trained_splits':['train']},
            'predictions':records}

    def prepared(self):
        neutral=export_dataset(self.state,'fixture-v1',self.revisions)
        raw=export_laya_raw(self.state,'fixture-v1',self.revisions)
        return neutral,raw

    def test_cli_raw_export_check_evaluate_walkthrough(self):
        neutral=self.command('export-dataset','fixture-v1','--revisions-json',self.revisions_path)
        raw=self.command('export-laya-raw','fixture-v1','--revisions-json',self.revisions_path)
        self.assertEqual(raw['candidate'],candidate_pin())
        self.assertEqual(raw['candidate']['commit'],COMMIT)
        self.assertEqual(raw['tokenization'],tokenization_status())
        self.assertEqual(raw['stage'],'raw-preprocessing-input')
        self.assertEqual(raw['neutral_export_sha256'],neutral['export_sha256'])
        self.assertEqual(raw,self.command('export-laya-raw','fixture-v1','--revisions-json',self.revisions_path))
        check=self.command('check-laya-raw','fixture-v1','--revisions-json',self.revisions_path)
        self.assertEqual(check['check'],'raw-contract-and-provenance-only')
        self.assertEqual(check['laya_export_sha256'],raw['laya_export_sha256'])
        path=self.write_json('laya-predictions.json',self.raw_predictions(raw))
        report=self.command('evaluate-laya','fixture-v1','--revisions-json',self.revisions_path,'--predictions-json',path)
        self.assertEqual(report['accuracy'],1)
        self.assertEqual(report['promotion'],'none')
        self.assertEqual(report['laya_adapter']['candidate'],candidate_pin())
        self.assertEqual(report['laya_adapter']['abstention_gates']['not-requested'],3)
        self.assertFalse(report['laya_adapter']['tokenization']['ready_tokenized_training_data'])
        self.assertEqual(report,self.command('evaluate-laya','fixture-v1','--revisions-json',self.revisions_path,'--predictions-json',path))
        self.assertTrue(Path(report['report_path']).is_file())
        # Dependencies absent: importing/running the pure adapter still works.
        with patch.dict(sys.modules,{'torch':None,'transformers':None,'laya':None}):
            self.assertEqual(export_laya_raw(self.state,'fixture-v1',self.revisions),raw)

    def test_criteria_name_order_and_one_hot_raw_shape(self):
        _,raw=self.prepared()
        root=Path(raw['directory'])
        for path in root.iterdir():self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(root.stat().st_mode & 0o777,0o700)
        sidecars=[json.loads(line) for line in (root/'provenance.jsonl').read_text().splitlines()]
        indexed={(s['split'],s['raw_row_sha256']):s for s in sidecars}
        for split in raw['counts']:
            for line in (root/(split+'.raw.jsonl')).read_text().splitlines():
                row=json.loads(line)
                self.assertEqual(set(row),{'state','questions','gold'})
                self.assertTrue(all(isinstance(v,str) for v in row.values()))
                state=json.loads(row['state']);questions=json.loads(row['questions']);gold=json.loads(row['gold'])
                self.assertEqual(list(state),['prompt','excerpt'])
                criteria=questions['relevance']['criteria']
                self.assertEqual(list(criteria),list(CLASS_ORDER))
                self.assertEqual(questions['relevance']['type'],'choice')
                provenance=indexed[(split,digest(line))]
                self.assertEqual(list(gold['relevance']['probabilities']),list(CLASS_ORDER))
                target=[gold['relevance']['probabilities'][name] for name in criteria]
                self.assertEqual(sum(target),1)
                self.assertEqual(CLASS_ORDER[target.index(1.0)],provenance['target_class_name'])
                self.assertEqual(digest(row['state']),provenance['state_sha256'])
                self.assertEqual(digest(row['questions']),provenance['questions_sha256'])
                self.assertNotIn('rationale',state)
        self.assertIn('not-calibrated',raw['target_semantics'])
        # Alphabetic canonical ordering is different and must never replace criteria order.
        self.assertNotEqual(list(json.loads(canonical(questions))['relevance']['criteria']),list(CLASS_ORDER))

    def test_abstention_and_name_based_mapping_to_existing_scorer(self):
        _,raw=self.prepared()
        payload=self.raw_predictions(raw)
        first=payload['predictions'][0]['answer']
        first.update(probabilities={'supporting':.35,'irrelevant':.25,'necessary':.4},answer_confidence=.4,
            abstention='abstained',abstention_threshold=.6,low_confidence=True)
        payload['predictions'][1]['answer'].update(abstention='passed',abstention_threshold=.7,low_confidence=False)
        report=evaluate_laya_predictions(self.state,'fixture-v1',self.revisions,payload)
        self.assertEqual(report['confusion_matrix'],[[0,0,0,1],[0,1,0,0],[0,0,1,0]])
        self.assertEqual(report['necessary_false_negative_rate'],1)
        self.assertAlmostEqual(report['coverage'],2/3)
        self.assertEqual(report['laya_adapter']['abstention_gates'],{'not-requested':1,'passed':1,'abstained':1,'unevaluated':0})
        with database(self.state) as db:
            manifest,checksum,rows=dataset._snapshot(db,'fixture-v1',self.revisions)
            neutral,_=dataset._payloads(manifest,checksum,rows)
            metadata,_,expected=_raw_payloads(neutral,rows)
        mapped,gates=map_laya_predictions(payload,metadata,expected)
        self.assertEqual([r['class_index'] for r in mapped['predictions']],[None,1,2])
        old=evaluate_predictions(self.state,'fixture-v1',self.revisions,mapped)
        self.assertEqual(old['confusion_matrix'],report['confusion_matrix'])
        first.update(answer_confidence=None,abstention='unevaluated')
        first.pop('low_confidence')
        self.assertEqual(evaluate_laya_predictions(self.state,'fixture-v1',self.revisions,payload)['laya_adapter']['abstention_gates']['unevaluated'],1)

    def test_schema_version_membership_and_answer_rejections(self):
        _,raw=self.prepared();good=self.raw_predictions(raw)
        invalid=[]
        for update in ({'schema_version':True},{'schema_version':2},{'format':'other'},
                {'candidate':dict(candidate_pin(),commit='0'*40)},{'candidate':dict(candidate_pin(),release='0.3.24')},
                {'laya_export_sha256':digest('other')},{'split':'train'},{'predictions':good['predictions'][:-1]},
                {'predictions':[good['predictions'][0]]*3}):
            invalid.append(dict(good,**update))
        for field,value in (('questions_sha256',digest('wrong question')),('state_sha256',digest('wrong renderer')),
                ('input_sha256',digest('wrong input')),('case_hash',digest('unknown case'))):
            bad=copy.deepcopy(good);bad['predictions'][0][field]=value;invalid.append(bad)
        for change in ({'type':'score'},{'choice':0},{'choice':'other'},
                {'probabilities':{'necessary':True,'supporting':0,'irrelevant':0}},
                {'probabilities':{'necessary':.5,'supporting':.5}},
                {'probabilities':{'necessary':.6,'supporting':.6,'irrelevant':.1}},
                {'answer_confidence':float('nan')},{'answer_confidence':.2},
                {'low_confidence':True},{'abstention':'passed'},
                {'abstention':'passed','abstention_threshold':.9,'low_confidence':True},
                {'abstention':'abstained','abstention_threshold':.9},
                {'abstention':'other'},{'private_extra':'untrusted text'}):
            bad=copy.deepcopy(good);bad['predictions'][0]['answer'].update(change);invalid.append(bad)
        for bad in invalid:
            with self.subTest():
                with self.assertRaises(ValueError):evaluate_laya_predictions(self.state,'fixture-v1',self.revisions,bad)
        with database(self.state) as db:
            manifest,checksum,rows=dataset._snapshot(db,'fixture-v1',self.revisions)
            neutral,_=dataset._payloads(manifest,checksum,rows)
            metadata,_,expected=_raw_payloads(neutral,rows)
        with self.assertRaises(ValueError):map_laya_predictions(good,None,expected)
        with patch('context_learning.dataset.MAX_BYTES',100):
            with self.assertRaisesRegex(ValueError,'raw_export_budget_exceeded'):_raw_payloads(neutral,rows)
        for change in ({'format':'other'},{'schema_version':2},{'criteria_order':sorted(CLASS_ORDER)},
                {'tokenization':dict(tokenization_status(),ready_tokenized_training_data=True)}):
            with self.assertRaises(ValueError):map_laya_predictions(good,dict(metadata,**change),expected)

    def test_revoked_stale_family_guards_and_corruption(self):
        _,raw=self.prepared();good=self.raw_predictions(raw)
        with self.assertRaises(ValueError):export_laya_raw(self.state,'fixture-v1',dict(self.revisions,doc0=digest('changed')))
        with database(self.state) as db:db.execute("UPDATE family_splits SET split='evaluation' WHERE family_hash=?",('task:'+digest('task-r0'),))
        with self.assertRaises(ValueError):check_laya_raw(self.state,'fixture-v1',self.revisions)
        with database(self.state) as db:db.execute("UPDATE family_splits SET split='train' WHERE family_hash=?",('task:'+digest('task-r0'),))
        path=Path(raw['directory'])/'train.raw.jsonl';original=path.read_text()
        item=json.loads(original);item['questions']=canonical(json.loads(item['questions']))
        path.write_text(canonical(item)+'\n')
        with self.assertRaises(ValueError):check_laya_raw(self.state,'fixture-v1',self.revisions)
        with self.assertRaises(ValueError):export_laya_raw(self.state,'fixture-v1',self.revisions)
        path.write_text(original)
        delete_case(self.state,'r3')
        for action in (lambda:check_laya_raw(self.state,'fixture-v1',self.revisions),
                lambda:export_laya_raw(self.state,'fixture-v1',self.revisions),
                lambda:evaluate_laya_predictions(self.state,'fixture-v1',self.revisions,good)):
            with self.assertRaises(ValueError):action()
        replay=build_manifest(self.state,'replay',self.assignments,self.revisions,parent_id='fixture-v1')
        export_dataset(self.state,'replay',self.revisions)
        replay_raw=export_laya_raw(self.state,'replay',self.revisions)
        self.assertEqual(replay_raw['counts']['evaluation'],2)
        self.assertTrue(path.is_file())  # No false copied-data erasure claim.

    def test_raw_budget_unicode_and_provisional_exclusion(self):
        with self.assertRaises(FileNotFoundError):export_laya_raw(self.state,'fixture-v1',self.revisions)
        self.prepared()
        provisional=dict(self.samples['r0'],request_id='provisional')
        capture(provisional,enabled=True,state_dir=self.state)
        save_label(self.state,dict(self.labels['r0'],request_id='provisional'))
        extra=dict(self.assignments,provisional=self.assignments['r0'])
        reviewed=build_manifest(self.state,'only-reviewed',extra,self.revisions)
        export_dataset(self.state,'only-reviewed',self.revisions)
        self.assertEqual(sum(export_laya_raw(self.state,'only-reviewed',self.revisions)['counts'].values()),6)
        text='검토된 권한 문서 예시'
        sample=request({'prompt':'이 문서가 요청을 해결하는 데 필요한가?'},[{
            'candidate_id':'한글-doc','revision_sha256':digest(text),'excerpt':text,'eligible':True}],host='codex',request_id='한글-request')
        capture(sample,enabled=True,state_dir=self.state)
        label=dict(self.labels['r0'],request_id=sample['request_id'],candidate_id='한글-doc',
            revision_sha256=digest(text),evidence=text,rationale={'kind':'support','text':'합성 검토.',
                'span':{'start':0,'end':len(text)},'scope':'provided-excerpt'})
        approve_review(self.state,label,input_sha256=digest(canonical(sample)),reviewer_id='synthetic',reference='offline-only')
        revisions=dict(self.revisions,**{'한글-doc':digest(text)})
        assignments=dict(self.assignments,**{'한글-request':{'split':'train','task_family':'한글-task','document_families':{'한글-doc':'한글-family'}}})
        build_manifest(self.state,'unicode',assignments,revisions);export_dataset(self.state,'unicode',revisions)
        raw=export_laya_raw(self.state,'unicode',revisions)
        decoded=[json.loads(json.loads(line)['state']) for line in (Path(raw['directory'])/'train.raw.jsonl').read_text().splitlines()]
        self.assertTrue(any(row['excerpt']==text for row in decoded))
        self.assertFalse(raw['tokenization']['truncation_validated'])
        with patch('context_learning.dataset.MAX_BYTES',100):
            with self.assertRaises(ValueError):export_laya_raw(self.state,'unicode',revisions)

if __name__=='__main__':unittest.main()
