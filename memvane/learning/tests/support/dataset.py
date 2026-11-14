"""Reviewed synthetic learning samples and the learning CLI run in process."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile

from memvane.learning import context_learning_cli as cli
from memvane.learning.context_learning.contracts import digest, request
from memvane.learning.context_learning.offline import approve_review, build_manifest, save_label
from memvane.learning.context_learning.store import canonical, capture


class ReviewedDataset:
    """Six reviewed synthetic samples, one per split/category, and the learning CLI in process."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='qmd-dataset-fixture-')
        self.root=Path(self.tmp.name).resolve()
        self.state=self.root/'private';self.state.mkdir(mode=0o700)
        self.revisions={};self.assignments={};self.labels={};self.samples={}
        for i,(split,category) in enumerate((('train','necessary'),('calibration','supporting'),
                ('validation','irrelevant'),('evaluation','necessary'),('evaluation','supporting'),('evaluation','irrelevant'))):
            rid='r'+str(i);cid='doc'+str(i);text='Synthetic excerpt '+str(i)
            sampling={'mode':'eligible-returned-pool','scope':'queried-phases-only','phase_counts':{'primary':1},
                'per_phase_limit':8,'eligible_ids':[cid],'captured_ids':[cid],'baseline_selected_ids':[cid],
                'excluded':[],'snapshot_failures':[],'retrieval_truncated':False,'assessment_complete':True,
                'complete_within_returned_bound':True,'index_revision':'unavailable'}
            sample=request({'prompt':'Synthetic request '+str(i)},[{'candidate_id':cid,'revision_sha256':digest(text),
                'excerpt':text,'eligible':True}],host='codex',request_id=rid,sampling=sampling)
            label={'schema_version':2,'request_id':rid,'candidate_id':cid,'revision_sha256':digest(text),
                'abstain':False,'relevance':category,'evidence':text if category!='irrelevant' else '',
                'rationale':{'kind':'support' if category!='irrelevant' else 'absence','text':'Synthetic reviewed justification.',
                    'span':{'start':0,'end':len(text)} if category!='irrelevant' else None,'scope':'provided-excerpt'}}
            self.assertEqual(capture(sample,enabled=True,state_dir=self.state),'stored')
            save_label(self.state,label)
            approve_review(self.state,label,input_sha256=digest(canonical(sample)),reviewer_id='fixture-reviewer',reference='synthetic-fixture')
            self.samples[rid]=sample;self.labels[rid]=label;self.revisions[cid]=digest(text)
            self.assignments[rid]={'split':split,'task_family':'task-'+rid,'document_families':{cid:'family-'+cid}}
        self.manifest=build_manifest(self.state,'fixture-v1',self.assignments,self.revisions)
        self.assignments_path=self.write_json('assignments.json',self.assignments)
        self.revisions_path=self.write_json('revisions.json',self.revisions)

    def tearDown(self):self.tmp.cleanup()

    def write_json(self,name,body):
        path=self.root/name
        path.write_text(canonical(body));path.chmod(0o600)
        return path

    def command(self,*args):
        out=io.StringIO()
        with redirect_stdout(out):
            cli.main(['--state-dir',str(self.state),*map(str,args)])
        self.assertNotIn('Traceback',out.getvalue())
        return json.loads(out.getvalue())

    def predictions(self,export,split='evaluation'):
        rows=[json.loads(line) for line in (Path(export['directory'])/(split+'.jsonl')).read_text().splitlines()]
        return {'schema_version':1,'export_sha256':export['export_sha256'],'split':split,
            'model':{'model_id':'synthetic-predictor','artifact_sha256':digest('synthetic-no-real-weights'),
                'training_manifest_sha256':export['manifest_sha256'],'trained_splits':['train']},
            'predictions':[{'case_hash':r['case_hash'],'input_sha256':r['input_sha256'],
                            'class_index':r['target_class_index']} for r in rows]}
