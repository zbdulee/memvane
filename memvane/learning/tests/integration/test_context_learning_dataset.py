"""Synthetic reviewed samples, explicit CLI export, supplied predictions; no provider."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from memvane.learning.context_learning.contracts import CLASS_ORDER, digest, request
from memvane.learning.context_learning.store import canonical, capture, database
from memvane.learning.context_learning.offline import approve_review, build_manifest, delete_case, save_label
from memvane.learning.context_learning.dataset import export_dataset, inspect_export, evaluate_predictions, read_private_json
from memvane.learning.tests.support.dataset import ReviewedDataset

class Checks(ReviewedDataset, unittest.TestCase):
    def test_cli_reviewed_export_and_held_out_walkthrough(self):
        manifest=self.command('build-manifest','fixture-v1','--assignments-json',self.assignments_path,'--revisions-json',self.revisions_path)
        self.assertEqual(manifest,self.manifest)
        self.assertEqual(self.command('inspect-manifest','fixture-v1'),manifest)
        export=self.command('export-dataset','fixture-v1','--revisions-json',self.revisions_path)
        self.assertEqual(export['counts'],{'train':1,'calibration':1,'validation':1,'evaluation':3})
        self.assertEqual(export['laya_contract'],'unverified')
        self.assertEqual(export,self.command('export-dataset','fixture-v1','--revisions-json',self.revisions_path))
        self.assertEqual(export,self.command('inspect-export','fixture-v1','--revisions-json',self.revisions_path))
        directory=Path(export['directory'])
        self.assertEqual(directory.stat().st_mode & 0o777,0o700)
        for path in directory.iterdir():self.assertEqual(path.stat().st_mode & 0o777,0o600)
        row=json.loads((directory/'train.jsonl').read_text())
        self.assertEqual(set(row['input']),{'prompt','excerpt'})
        self.assertNotIn('rationale',row['input'])
        self.assertEqual(len(row['provenance']['split_family_hashes']),6)
        self.assertEqual(row['provenance']['sample_sha256'],digest(canonical(self.samples['r0'])))
        self.assertEqual(row['provenance']['label_sha256'],digest(canonical(self.labels['r0'])))
        predictions=self.predictions(export);prediction_path=self.write_json('predictions.json',predictions)
        report=self.command('evaluate','fixture-v1','--revisions-json',self.revisions_path,'--predictions-json',prediction_path)
        self.assertEqual(report['accuracy'],1)
        self.assertEqual(report['necessary_false_negative_rate'],0)
        self.assertEqual(report['macro_f1_supported_classes'],1)
        self.assertEqual(report['promotion'],'none')
        self.assertEqual(report,self.command('evaluate','fixture-v1','--revisions-json',self.revisions_path,'--predictions-json',prediction_path))
        self.assertTrue(Path(report['report_path']).is_file())
        with patch('memvane.learning.context_learning.transport.run_teacher',side_effect=AssertionError('provider forbidden')):
            self.assertEqual(evaluate_predictions(self.state,'fixture-v1',self.revisions,predictions)['cases'],3)

    def test_metrics_abstention_and_prediction_contract(self):
        export=export_dataset(self.state,'fixture-v1',self.revisions)
        good=self.predictions(export)
        changed=copy.deepcopy(good);changed['predictions'][0]['class_index']=None
        changed['predictions'][1]['class_index']=0
        report=evaluate_predictions(self.state,'fixture-v1',self.revisions,changed)
        self.assertEqual(report['confusion_matrix'],[[0,0,0,1],[1,0,0,0],[0,0,1,0]])
        self.assertAlmostEqual(report['accuracy'],1/3);self.assertAlmostEqual(report['coverage'],2/3)
        self.assertEqual(report['selective_accuracy'],.5);self.assertEqual(report['necessary_false_negative_rate'],1)
        all_abstain=copy.deepcopy(good)
        for row in all_abstain['predictions']:row['class_index']=None
        self.assertIsNone(evaluate_predictions(self.state,'fixture-v1',self.revisions,all_abstain)['selective_accuracy'])
        invalid=[]
        for update in ({'schema_version':True},{'split':'train'},{'export_sha256':digest('other')},
                {'predictions':good['predictions'][:-1]},{'predictions':[good['predictions'][0]]*3}):
            invalid.append(dict(good,**update))
        for field,value in (('class_index',True),('class_index',3),('class_index',float('nan')),
                ('input_sha256',digest('wrong')),('case_hash',digest('foreign'))):
            bad=copy.deepcopy(good);bad['predictions'][0][field]=value;invalid.append(bad)
        for update in ({'trained_splits':['train','evaluation']},{'artifact_sha256':'bad'},
                {'training_manifest_sha256':digest('unknown')},{'extra':'field'}):
            bad=copy.deepcopy(good);bad['model'].update(update);invalid.append(bad)
        for bad in invalid:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):evaluate_predictions(self.state,'fixture-v1',self.revisions,bad)
        with self.assertRaises(ValueError):evaluate_predictions(self.state,'fixture-v1',self.revisions,good,split='train')
        validation=self.predictions(export,'validation')
        report=evaluate_predictions(self.state,'fixture-v1',self.revisions,validation,split='validation')
        self.assertIsNone(report['necessary_false_negative_rate'])
        self.assertEqual(report['macro_f1_supported_classes'],1)

    def test_provisional_abstained_stale_and_deleted_excluded(self):
        provisional=dict(self.samples['r0'],request_id='provisional')
        capture(provisional,enabled=True,state_dir=self.state)
        label=dict(self.labels['r0'],request_id='provisional')
        save_label(self.state,label)
        abstained=dict(self.samples['r1'],request_id='abstained')
        capture(abstained,enabled=True,state_dir=self.state)
        label=dict(self.labels['r1'],request_id='abstained',abstain=True,relevance=None,evidence='',
            rationale={'kind':'uncertain','text':'Uncertain fixture.','span':None,'scope':'provided-excerpt'})
        approve_review(self.state,label,input_sha256=digest(canonical(abstained)),reviewer_id='fixture',reference='synthetic')
        assignments=dict(self.assignments,provisional=self.assignments['r0'],abstained=self.assignments['r1'])
        stale=dict(self.revisions,doc2=digest('new-document'))
        manifest=build_manifest(self.state,'exclusions',assignments,stale)
        self.assertEqual(len(manifest['cases']),5)
        self.assertEqual(manifest['excluded'],{'abstained':1,'stale':1})
        export=export_dataset(self.state,'exclusions',stale)
        self.assertEqual(sum(export['counts'].values()),5)
        with self.assertRaises(ValueError):export_dataset(self.state,'fixture-v1',stale)
        full=export_dataset(self.state,'fixture-v1',self.revisions);predictions=self.predictions(full)
        delete_case(self.state,'r3')
        for action in (lambda:inspect_export(self.state,'fixture-v1',self.revisions),
                lambda:export_dataset(self.state,'fixture-v1',self.revisions),
                lambda:evaluate_predictions(self.state,'fixture-v1',self.revisions,predictions)):
            with self.assertRaises(ValueError):action()
        replay=build_manifest(self.state,'replay',self.assignments,self.revisions,parent_id='fixture-v1')
        self.assertEqual(len(replay['cases']),5)
        self.assertEqual(sum(export_dataset(self.state,'replay',self.revisions)['counts'].values()),5)
        # Copies already exported are not automatically erased/unlearned.
        self.assertTrue((Path(full['directory'])/'evaluation.jsonl').is_file())

    def test_family_guards_corruption_and_empty_splits(self):
        for changes in (
            dict(self.assignments,r0=dict(self.assignments['r0'],split='evaluation')),
            dict(self.assignments,r3=dict(self.assignments['r3'],task_family='task-r0')),
            dict(self.assignments,r3=dict(self.assignments['r3'],document_families={'doc3':'family-doc0'}))):
            with self.assertRaises(ValueError):build_manifest(self.state,'leak',changes,self.revisions)
        export=export_dataset(self.state,'fixture-v1',self.revisions)
        path=Path(export['directory'])/'train.jsonl';original=path.read_text()
        path.write_text(original.replace('Synthetic request 0','altered request'))
        with self.assertRaises(ValueError):inspect_export(self.state,'fixture-v1',self.revisions)
        path.write_text(original)
        with database(self.state) as db:db.execute("UPDATE family_splits SET split='evaluation' WHERE family_hash=?",('request:'+digest('r0'),))
        with self.assertRaises(ValueError):inspect_export(self.state,'fixture-v1',self.revisions)
        with database(self.state) as db:db.execute("UPDATE family_splits SET split='train' WHERE family_hash=?",('request:'+digest('r0'),))
        with database(self.state) as db:
            stored=db.execute("SELECT body,sha256 FROM manifests WHERE version_id='fixture-v1'").fetchone()
            legacy=json.loads(stored[0]);legacy['schema_version']=1
            for case in legacy['cases']:case.pop('split_family_hashes')
            encoded=canonical(legacy)
            db.execute("UPDATE manifests SET body=?,sha256=? WHERE version_id='fixture-v1'",(encoded,digest(encoded)))
        with self.assertRaisesRegex(ValueError,'manifest_split_provenance_required'):
            export_dataset(self.state,'fixture-v1',self.revisions)
        with database(self.state) as db:
            db.execute("UPDATE manifests SET body=?,sha256=? WHERE version_id='fixture-v1'",stored)
        train_only=build_manifest(self.state,'train-only',{'r0':self.assignments['r0']},self.revisions)
        train_export=export_dataset(self.state,'train-only',self.revisions)
        with self.assertRaises(ValueError):evaluate_predictions(self.state,'train-only',self.revisions,self.predictions(train_export))
        build_manifest(self.state,'empty',{},self.revisions)
        with self.assertRaises(ValueError):export_dataset(self.state,'empty',self.revisions)
        with database(self.state) as db:db.execute("UPDATE labels SET review=NULL WHERE request_id='r0'")
        with self.assertRaises(ValueError):inspect_export(self.state,'fixture-v1',self.revisions)

    def test_private_io_and_resource_budgets(self):
        path=self.write_json('input.json',{'safe':True})
        self.assertEqual(read_private_json(path),{'safe':True})
        path.chmod(0o644)
        with self.assertRaises(ValueError):read_private_json(path)
        path.chmod(0o600)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(OSError):read_private_json(link)
        link.unlink();os.link(path,link)
        with self.assertRaises(ValueError):read_private_json(path)
        link.unlink();path.write_text('{"x":1,"x":2}')
        with self.assertRaises(ValueError):read_private_json(path)
        path.write_text('{"x":NaN}')
        with self.assertRaises(ValueError):read_private_json(path)
        path.write_text('x'*1025)
        with patch('memvane.learning.context_learning.dataset.MAX_BYTES',1024):
            with self.assertRaises(ValueError):read_private_json(path)
        with patch('memvane.learning.context_learning.dataset.MAX_CASES',1):
            with self.assertRaises(ValueError):export_dataset(self.state,'fixture-v1',self.revisions)
        with patch('memvane.learning.context_learning.dataset.MAX_BYTES',100):
            with self.assertRaises(ValueError):export_dataset(self.state,'fixture-v1',self.revisions)
        export=export_dataset(self.state,'fixture-v1',self.revisions)
        target=Path(export['directory'])/'train.jsonl'
        target.chmod(0o644)
        with self.assertRaises(ValueError):inspect_export(self.state,'fixture-v1',self.revisions)
        target.chmod(0o600)
        raw=target.read_bytes();target.unlink();target.symlink_to(path)
        with self.assertRaises(OSError):inspect_export(self.state,'fixture-v1',self.revisions)
        target.unlink();target.write_bytes(raw);target.chmod(0o600)
        outside=self.root/'outside';os.link(target,outside)
        with self.assertRaises(ValueError):inspect_export(self.state,'fixture-v1',self.revisions)

if __name__=='__main__':unittest.main()
