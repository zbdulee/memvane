"""Registered local marketplace bindings; no host sessions or models."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path.cwd()/'core'))
from context_learning import adapter_binding as binding

MARKET=binding.MARKETPLACE
VERSION='1.0.2-reviewed'

class LocalBinding(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qmd-local-binding-')
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()/'home';self.home.mkdir(mode=0o700)
        self.market=self.home/'reviewed package'
        self.source=self.market/'plugins/memvane'
        self.source.mkdir(parents=True)
        for host in ['claude','codex']:
            self.write(self.source/f'.{host}-plugin/plugin.json',{'name':'memvane','version':VERSION})
        self.write(self.source/'package.json',{'name':'memvane','version':VERSION})
        for name,text in [('core/context_learning/laya_adapter.py','adapter = 1\n'),('core/context_learning/other.py','identity = 1\n'),('core/update.sh','true\n'),('hooks/hooks.json','{}\n')]:
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        self.write(self.market/'.claude-plugin/marketplace.json',{'name':MARKET,'plugins':[{'name':'memvane','source':'./plugins/memvane','version':VERSION}]})
        self.write(self.market/'.agents/plugins/marketplace.json',{'name':MARKET,'plugins':[{'name':'memvane','source':{'source':'local','path':'./plugins/memvane'},'version':VERSION}]})
        self.cache={}
        for host in ['claude','codex']:
            root=self.home/f'.{host}/plugins/cache'/MARKET/'memvane'/VERSION
            root.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(self.source,root);self.cache[host]=root
        self.known={MARKET:{'source':{'source':'directory','path':str(self.market)},'installLocation':str(self.market)}}
        self.registry={'version':2,'plugins':{binding.PLUGIN_ID:[{'scope':'user','version':VERSION,'installPath':str(self.cache['claude'])}]}}
        self.inventory={'installed':[{'pluginId':binding.PLUGIN_ID,'name':'memvane','marketplaceName':MARKET,'version':VERSION,'installed':True,'enabled':True,'source':{'source':'local','path':str(self.source)},'marketplaceSource':{'sourceType':'local','source':str(self.market)}}]}
        self.persist()
        self.addCleanup(patch.stopall)
        patch.object(Path,'home',return_value=self.home).start()
        patch.dict(os.environ,{'CODEX_HOME':str(self.home/'.codex')}).start()
        def cli(*args,**kwargs):
            self.assertEqual(args[0],['codex','plugin','list','--marketplace',MARKET,'--json'])
            self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
            return subprocess.CompletedProcess(args[0],0,json.dumps(self.inventory),'')
        self.cli=patch.object(binding.subprocess,'run',side_effect=cli).start()
    def write(self,p,row):
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(row));p.chmod(0o600)
    def persist(self):
        self.write(self.home/'.claude/plugins/known_marketplaces.json',self.known)
        self.write(self.home/'.claude/plugins/installed_plugins.json',self.registry)
    def adapter(self,root):return root/'core/context_learning/laya_adapter.py'
    def choose(self,source,target):
        return binding.inference_argv(['/runtime/python',str(self.adapter(source)),'/model'],self.adapter(target),deadline=time.monotonic()+5)
    def denied(self,source=None,target=None):
        with self.assertRaisesRegex(ValueError,'unsupported_adapter'):
            self.choose(source or self.cache['codex'],target or self.source)
    def test_both_local_host_directions(self):
        for source,target in [(self.cache['codex'],self.source),(self.source,self.cache['codex']),(self.cache['claude'],self.cache['codex'])]:
            self.assertEqual(self.choose(source,target),['/runtime/python',str(self.adapter(target)),'/model'])
    def test_local_registry_and_catalog_identity_required(self):
        self.known[MARKET]['source']['path']=str(self.home/'wrong');self.persist();self.denied()
    def test_unregistered_claude_cache_cannot_execute(self):
        self.denied(self.cache['codex'],self.cache['claude'])
    def test_codex_inventory_source_must_match_registered_root(self):
        self.inventory['installed'][0]['marketplaceSource']['source']=str(self.home/'wrong')
        self.denied(self.source,self.cache['codex'])
    def test_other_module_tampering_denied(self):
        (self.cache['codex']/'core/context_learning/other.py').write_text('identity=2\n');self.denied()
    def test_hook_tampering_denied(self):
        (self.cache['claude']/'hooks/hooks.json').write_text('{"tampered":true}\n');self.denied()
    def test_catalog_source_escape_denied(self):
        self.write(self.market/'.claude-plugin/marketplace.json',{'name':MARKET,'plugins':[{'name':'memvane','source':'../rogue','version':VERSION}]});self.denied()
    def test_source_symlink_denied(self):
        p=self.source/'core/context_learning/other.py';p.unlink();p.symlink_to(self.cache['codex']/'core/context_learning/other.py');self.denied()
    def test_duplicate_inventory_denied(self):
        self.inventory['installed'].append(copy.deepcopy(self.inventory['installed'][0]));self.denied(self.source,self.cache['codex'])
    def test_registry_version_mismatch_denied(self):
        self.registry['plugins'][binding.PLUGIN_ID][0]['version']='0.0.1';self.persist();self.denied()
    def test_short_external_adapter_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'unsupported_adapter'):
            binding.inference_argv(['/runtime/python',str(self.adapter(self.cache['codex'])),'/model'],Path('/x'),deadline=time.monotonic()+5)
    def test_empty_unsafe_hook_directory_denied(self):
        for root in (self.source,self.cache['claude'],self.cache['codex']):
            (root/'hooks/hooks.json').unlink()
            (root/'hooks').chmod(0o777)
        self.denied()
    def test_unsafe_hook_directory_denied(self):
        (self.source/'hooks').chmod(0o777)
        self.denied()
    def test_codex_catalog_remote_url_denied(self):
        self.write(self.market/'.agents/plugins/marketplace.json',{'name':MARKET,'plugins':[{'name':'memvane','source':{'source':'url','url':'https://github.com/zbdulee/memvane'},'version':VERSION}]})
        self.denied(self.source,self.cache['codex'])
    def test_manifest_cache_mismatch_denied(self):
        self.write(self.cache['codex']/'.claude-plugin/plugin.json',{'name':'memvane','version':'wrong'})
        self.denied()
    def test_missing_claude_registry_denied(self):
        self.registry['plugins']={};self.persist();self.denied()
    def test_local_expired_budget_does_not_start_inventory(self):
        with self.assertRaises(binding.BindingTimeout):
            binding.inference_argv(['/runtime/python',str(self.adapter(self.source)),'/model'],self.adapter(self.cache['codex']),deadline=time.monotonic()-1)
        self.cli.assert_not_called()
    def test_cross_host_code_difference_denied(self):
        for root in (self.source,self.cache['claude']):
            (root/'core/context_learning/other.py').write_text('identity = 3\n')
        self.denied(self.cache['codex'],self.source)
    def test_extra_cache_executable_denied(self):
        (self.cache['codex']/'core/unreviewed.py').write_text('extra = 1\n');self.denied()
    def test_rogue_identical_source_denied(self):
        rogue=self.home/'rogue/plugins/memvane';rogue.parent.mkdir(parents=True);shutil.copytree(self.source,rogue);self.denied(rogue,self.cache['codex'])
    def many_files(self,count=150):
        for n in range(count):(self.source/f'core/generated_{n}.py').write_text(f'value = {n}\n')
        return count+4
    def test_inventory_admission_checkpoints_are_batched(self):
        # Per-read checkpoints made a full inventory exceed the 1s foreground
        # preparation budget; the count path must stay bounded, start to end.
        import runtime_admission
        files=self.many_files();calls=[]
        with patch.object(binding,'_PACE_SECONDS',float('inf')),\
                patch.object(runtime_admission,'preparation_checkpoint',side_effect=lambda:calls.append(1)):
            rows=binding._local_code(self.source,self.home)
        self.assertEqual(len(rows),files)
        self.assertGreaterEqual(len(calls),2)
        self.assertLessEqual(len(calls),2+(4*files)//binding._PACE_STEPS)
    def test_inventory_time_bound_forces_checkpoints(self):
        import runtime_admission
        files=self.many_files(20);calls=[]
        with patch.object(binding,'_PACE_SECONDS',0),\
                patch.object(runtime_admission,'preparation_checkpoint',side_effect=lambda:calls.append(1)):
            binding._local_code(self.source,self.home)
        self.assertGreater(len(calls),files)
    def test_inventory_resource_stop_interrupts_reads(self):
        import runtime_admission
        from context_learning.local_cycle import ResourceDeferred
        files=self.many_files();calls=[]
        def checkpoint():
            calls.append(1)
            if len(calls)==3:raise ResourceDeferred('preparation_budget')
        reads=patch.object(binding,'_owned_bytes',wraps=binding._owned_bytes)
        with patch.object(binding,'_PACE_STEPS',4),patch.object(binding,'_PACE_SECONDS',float('inf')),\
                patch.object(runtime_admission,'preparation_checkpoint',side_effect=checkpoint),reads as spy:
            with self.assertRaisesRegex(ResourceDeferred,'preparation_budget'):
                binding._local_code(self.source,self.home)
        self.assertEqual(len(calls),3)
        self.assertLess(spy.call_count,files)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(LocalBinding)
    result=unittest.TextTestRunner(stream=sys.stderr).run(suite)
    if not result.wasSuccessful():raise SystemExit(1)
    print(json.dumps({'checks':result.testsRun,'modelCalls':0,'providerCalls':0}))
