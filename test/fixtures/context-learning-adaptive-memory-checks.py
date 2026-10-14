"""No live model/process/OS probe; disposable resource and analytical tests."""
import json
import math
from contextlib import nullcontext
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import shared, local_cycle, laya_adapter

class Scalar:
    def __init__(self, value, backward=None): self.value=value; self.action=backward
    def __mul__(self,n): return Scalar(self.value*n,lambda: self.action(n))
    def detach(self): return self
    def cpu(self): return self
    def item(self): return self.value
    def all(self): return self
    def backward(self): self.action()

class TinyHead:
    """Analytical binary cross entropy; no torch/model/runtime imports."""
    def __init__(self): self.weight=.2; self.grad=0; self.calls=[]
    def __call__(self, **batch):
        self.calls.append(batch['items'])
        return type('Logits',(),{'shape':(len(batch['items']),2),
                               'float':lambda _:batch['items']})(), None

class Optimizer:
    def __init__(self,model): self.model=model; self.steps=0; self.zeros=0
    def zero_grad(self,**kw): self.model.grad=0; self.zeros+=1
    def step(self): self.model.weight-=.01*self.model.grad; self.steps+=1

class Accumulation(unittest.TestCase):
    def test_weighted_loss_all_pairs_one_optimizer_step(self):
        self.assertTrue(hasattr(laya_adapter,'_train_group'),'missing accumulation implementation')
        items=[{'x':1,'gold_label':1},{'x':2,'gold_label':0},{'x':3,'gold_label':1}]
        results=[]
        for microbatch in (1,2,3):
            model=TinyHead();optimizer=Optimizer(model)
            class Torch:
                long='long'
                @staticmethod
                def tensor(values,**kw):return values
                @staticmethod
                def isfinite(value):return Scalar(math.isfinite(value.value))
            class F:
                @staticmethod
                def cross_entropy(batch,targets):
                    ps=[1/(1+math.exp(-model.weight*r['x'])) for r in batch]
                    loss=sum(-math.log(p if y else 1-p) for p,y in zip(ps,targets))/len(batch)
                    grad=sum((p-y)*r['x'] for p,y,r in zip(ps,targets,batch))/len(batch)
                    return Scalar(loss,lambda scale=1:setattr(model,'grad',model.grad+grad*scale))
            param=type('Param',(),{'device':'cpu','grad':None})()
            with patch.object(laya_adapter,'_batch',side_effect=lambda group,*_: {'items':group}):
                loss=laya_adapter._train_group(Torch,F,model,None,[param],optimizer,
                    items,microbatch,limits=None)
            self.assertEqual(optimizer.zeros,1);self.assertEqual(optimizer.steps,1)
            self.assertEqual(sum(len(group) for group in model.calls),3)
            self.assertLessEqual(max(map(len,model.calls)),microbatch)
            results.append((model.weight,loss))
        for actual in results[1:]:
            self.assertAlmostEqual(actual[0],results[0][0],places=12)
            self.assertAlmostEqual(actual[1],results[0][1],places=12)

    def test_train_max_updates_counts_effective_group_and_tail(self):
        items=[{'x':n+1,'gold_label':n%2,'ids':[1]*10,
                'candidate_id':str(n)} for n in range(5)]
        counts=[]
        for cap in (1,None):
            model=TinyHead(); param=types.SimpleNamespace(device='cpu',requires_grad=True,grad=None)
            param.model=model
            class Weight:
                def detach(self):return Scalar(model.weight)
            Scalar.clone=lambda self:Scalar(self.value)
            model.scorer=[types.SimpleNamespace(weight=Weight())]
            model.parameters=lambda:iter([param])
            model.train=model.eval=lambda:None
            model.encoder=types.SimpleNamespace(eval=lambda:None)
            optimizers=[]
            def optimizer(params,**kw):
                opt=Optimizer(model);optimizers.append(opt);return opt
            torch=types.ModuleType('torch');torch.__path__=[]
            torch.manual_seed=lambda _:None;torch.long='long'
            torch.optim=types.SimpleNamespace(SGD=optimizer)
            torch.tensor=lambda values,**kw:values
            torch.isfinite=lambda scalar:Scalar(math.isfinite(scalar.value))
            torch.equal=lambda x,y:x.value==y.value
            nn=types.ModuleType('torch.nn');nn.__path__=[]
            functional=types.ModuleType('torch.nn.functional')
            def cross_entropy(batch,targets):
                ps=[1/(1+math.exp(-model.weight*r['x'])) for r in batch]
                loss=sum(-math.log(p if y else 1-p) for p,y in zip(ps,targets))/len(batch)
                grad=sum((p-y)*r['x'] for p,y,r in zip(ps,targets,batch))/len(batch)
                return Scalar(loss,lambda scale=1:setattr(model,'grad',model.grad+grad*scale))
            functional.cross_entropy=cross_entropy;nn.functional=functional;torch.nn=nn
            limits={'microbatchSize':1,'effectiveBatchSize':2,'device':'cpu',
                    'modelIdentitySha256':'a'*64,'reserveFraction':.1}
            with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.dict(sys.modules,
                    {'torch':torch,'torch.nn':nn,'torch.nn.functional':functional}), patch.object(
                    laya_adapter,'model_identity',return_value='a'*64), patch.object(
                    laya_adapter,'_device',return_value='cpu'), patch.object(
                    laya_adapter,'_load',return_value=(model,None,{'max_len':8192})), patch.object(
                    laya_adapter,'_pairs',return_value=items.copy()), patch.object(
                    laya_adapter,'_batch',side_effect=lambda group,*_: {'items':group}), patch.object(
                    laya_adapter,'_save_head') as save:
                result=laya_adapter.train(tmp,{'schema_version':1,
                    'artifact_path':str(Path(tmp)/'head.artifact'),
                    'model_input_view':laya_adapter.EDIT_INPUT_VIEW,
                    'record_step_losses':True,'training_limits':limits},max_updates=cap)
            self.assertEqual(optimizers[0].steps,1 if cap==1 else 3)
            self.assertEqual([s['pairs'] for s in result['step_losses']],[2] if cap==1 else [2,2,1])
            self.assertEqual(len(model.calls),2 if cap==1 else 5)
            self.assertTrue(all(len(group)==1 for group in model.calls))
            save.assert_called_once()

    def test_profile_change_and_unknown_mps_never_continue(self):
        limits={'microbatchSize':1,'effectiveBatchSize':2,'device':'mps',
                'modelIdentitySha256':'a'*64,'reserveFraction':.1}
        for sha,device in [('b'*64,'mps'),('a'*64,'cpu')]:
            with self.assertRaisesRegex(ValueError,'training_memory_profile_changed'):
                laya_adapter._training_limits({'training_limits':limits},sha,device)
        torch=types.SimpleNamespace(mps=types.SimpleNamespace(
            driver_allocated_memory=lambda:901,recommended_max_memory=lambda:1000))
        with self.assertRaisesRegex(ValueError,'training_memory_budget'):
            laya_adapter._check_mps_budget(torch,limits)
        torch.mps.recommended_max_memory=lambda:0
        with self.assertRaisesRegex(ValueError,'training_memory_unavailable'):
            laya_adapter._check_mps_budget(torch,limits)

    def test_validation_prediction_also_honors_microbatch_one(self):
        model=TinyHead();model.parameters=lambda:iter([types.SimpleNamespace(device='cpu')])
        items=[{'x':n,'ids':[1]*10,'request_id':'r','candidate_id':str(n)} for n in range(4)]
        limits={'microbatchSize':1,'effectiveBatchSize':2,'device':'cpu',
                'modelIdentitySha256':'a'*64,'reserveFraction':.1}
        class Probabilities:
            def __init__(self,n):self.n=n
            def __getitem__(self,_):return self
            def tolist(self):return [.9]*self.n
        torch=types.SimpleNamespace(no_grad=nullcontext,
            softmax=lambda values,*_:Probabilities(len(values)))
        with patch.dict(sys.modules,{'torch':torch}), patch.object(
                laya_adapter,'_pairs',return_value=items), patch.object(
                laya_adapter,'_batch',side_effect=lambda group,*_: {'items':group}):
            result=laya_adapter.predict_loaded(model,None,
                {'max_len':8192,'model_input_view':laya_adapter.EDIT_INPUT_VIEW},
                {'schema_version':1,'model_input_view':laya_adapter.EDIT_INPUT_VIEW,
                 'cases':[{'request_id':'r','input_sha256':'a'*64}]},memory_limits=limits)
        self.assertEqual([len(group) for group in model.calls],[1,1,1,1])
        self.assertEqual(result['predictions'][0]['selected_ids'],['0','1','2'])

class Memory(unittest.TestCase):
    def report(self, pressure='System-wide memory free percentage: 44%', rc=0,
               vm_rc=0, adaptive=None):
        def command(argv, **kw):
            if argv[0].endswith('vm_stat'):
                return subprocess.CompletedProcess(argv, vm_rc,
                    'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                    'Pages free: 327680.\nPages inactive: 0.\nPages speculative: 0.\n')
            if argv[-1] == 'hw.memsize':
                return subprocess.CompletedProcess(argv, 0, str(8*1024**3))
            if argv[-1] == 'vm.swapusage':
                return subprocess.CompletedProcess(argv, 0, 'used = 10.0M')
            return subprocess.CompletedProcess(argv, rc, pressure)
        policy = {'maxLoadPerCpu': 1.5, 'minAvailableMib': 1024, 'maxRssMib':4096}
        if adaptive is not None: policy['adaptiveMemory'] = adaptive
        with patch.object(shared.sys, 'platform', 'darwin'), patch.object(
                shared.Path, 'is_file', return_value=False), patch.object(
                shared.subprocess, 'run', side_effect=command), patch.object(
                shared.os, 'getloadavg', return_value=(0.1, 0, 0)):
            return shared.resource_report(policy, detailed=True)

    def profile(self, **kw):
        return {'device':'cpu', 'fixedMib':1024, 'perPairMib':2048,
                'estimateSource':'synthetic estimate, not measured',
                'modelIdentitySha256':'a'*64, **kw}

    def test_actual_macos_44_and_old_spelling(self):
        for output in ('System-wide memory free percentage: 44%',
                       'System-wide free percentage: 44%'):
            report = self.report(output)
            self.assertEqual(report['memory_pressure'], {'system_free_percent':44})
            self.assertEqual(report['query_status']['memory_pressure'], 'ok')

    def test_unknown_and_denied_are_distinct(self):
        self.assertEqual(self.report('unexpected')['query_status']['memory_pressure'], 'unrecognized')
        self.assertEqual(self.report('denied', rc=1)['query_status']['memory_pressure'], 'failed')
        self.assertIsNone(self.report(vm_rc=1)['available_mib'])
        self.assertFalse(self.report(vm_rc=1)['ready'])

    def test_adaptive_reduces_two_to_one_without_reducing_floor(self):
        report = self.report(adaptive=self.profile())
        self.assertTrue(report['ready'], report)
        self.assertEqual(report['training_limits']['microbatchSize'], 1)
        self.assertEqual(report['memory_admission']['reserve_mib'], 1024)
        self.assertEqual(report['training_limits']['effectiveBatchSize'], 2)

    def test_minimum_one_unknown_profile_and_pressure_defer(self):
        for profile in (self.profile(fixedMib=5000), {}, self.profile(device='other')):
            report = self.report(adaptive=profile)
            self.assertFalse(report['ready'], report)
        for raw, rc in [('unexpected',0), ('denied',1),
                        ('System-wide memory free percentage: 5%',0)]:
            report=self.report(raw,rc=rc,adaptive=self.profile())
            self.assertFalse(report['ready'], report)

    def test_ram_fraction_and_explicit_floor_both_bind(self):
        report=self.report(adaptive=self.profile(reserveFraction=.5))
        self.assertFalse(report['ready'], report)
        self.assertEqual(report['memory_admission']['reserve_mib'],4096)

    def test_legacy_policy_retains_its_floor_and_no_profile_requirement(self):
        self.assertTrue(self.report()['ready'])
        self.assertNotIn('training_limits',self.report())

    def test_no_launch_and_durable_defer(self):
        report=self.report(adaptive=self.profile(fixedMib=5000))
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.object(
                local_cycle.subprocess,'Popen',side_effect=AssertionError('must not launch')):
            trainer=local_cycle.LocalTrainer([sys.executable],pressure_probe=lambda:report)
            with self.assertRaisesRegex(ValueError,'trainer_system_pressure'):
                trainer.call('train',{'schema_version':1},Path(tmp)/'result.json',cwd=tmp)
            receipt=json.loads((Path(tmp)/'resource-defer.json').read_text())
            self.assertEqual(receipt['status'],'deferred_resource')
            self.assertFalse(receipt['completionAck'])
            self.assertFalse((Path(tmp)/'result.json').exists())

    def test_unknown_pressure_probe_is_durably_deferred(self):
        for probe in (lambda: None, lambda: {'ready':'yes'}):
            with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.object(
                    local_cycle.subprocess,'Popen',side_effect=AssertionError('no launch')):
                trainer=local_cycle.LocalTrainer([sys.executable],pressure_probe=probe)
                with self.assertRaises(local_cycle.ResourceDeferred):
                    trainer.call('train',{},Path(tmp)/'result.json',cwd=tmp)
                self.assertFalse(json.loads((Path(tmp)/'resource-defer.json').read_text())['completionAck'])

    def test_verified_mismatch_or_missing_proof_never_auto_attests(self):
        from context_learning import runtime_setup
        report=self.report(adaptive=self.profile())
        for sha in ('b'*64,'a'*64):
            with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.object(
                    laya_adapter,'model_identity',return_value=sha), patch.object(
                    runtime_setup,'probe_runtime',side_effect=AssertionError('no import probe')), patch.object(
                    runtime_setup,'attest_runtime',side_effect=AssertionError('no smoke')):
                trainer=local_cycle.VerifiedLayaTrainer([sys.executable,
                    str(Path(laya_adapter.__file__).resolve()),tmp],pressure_probe=lambda:report)
                with self.assertRaises(local_cycle.ResourceDeferred):
                    trainer.call('train',{},Path(tmp)/'result.json',cwd=tmp)

    def test_isolated_preflight_preserves_output_only_boundary(self):
        from context_learning import isolated_train
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.object(
                shared,'resource_report',return_value={'ready':False}):
            plan={'policy':{},'root':Path(tmp),'output':Path(tmp)/'new-output'}
            with self.assertRaises(ValueError):isolated_train._run_plan(plan,lambda:plan)
            self.assertFalse((Path(tmp)/'resource-defer.json').exists())
            self.assertFalse(plan['output'].exists())

if __name__ == '__main__': unittest.main()
