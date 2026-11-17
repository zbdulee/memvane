"""Synthetic installed-host updates through maintenance and shared entry paths."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import time
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment, project_learning_setup as setup
from memvane.learning.context_learning import adapter, dataset, host_inventory
from memvane.learning.context_learning import learning_profile, project_scope, selector_heal, shared
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.store import canonical


class RenewalFixture(FixtureCase):
    def write(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(canonical(value)+'\n', encoding='utf8')
        path.chmod(0o600)

    def read(self, name):
        return dataset.read_private_json(self.root/name)

    def bundle(self, host, version, marker):
        root=self.home/('.'+host)/'plugins/cache/memvane-marketplace/memvane'/version
        anchor=root/'memvane/learning/context_learning/adapter.py'
        self.write(anchor, marker)
        anchor.write_bytes(Path(adapter.__file__).read_bytes())
        self.write(root/'memvane/marker.py',marker)
        for path in (root/'package.json', root/('.'+host+'-plugin/plugin.json')):
            self.write(path, {'name':'memvane','version':version})
            path.chmod(0o644)
        return root

    def snapshot(self, root, home):
        # The bundle-content boundary is synthetic; selection, actual file
        # hashing, authorization envelopes, setup, CAS and entry dispatch are real.
        root=Path(root)
        if self.home not in root.parents:root=self.running
        anchor=root/'memvane/learning/context_learning/adapter.py'
        sha=file_sha(anchor)
        files={'learning/context_learning/adapter.py':sha,'marker.py':file_sha(root/'memvane/marker.py')}
        return files, tuple(('memvane/'+name,value) for name,value in sorted(files.items()))

    def code(self):
        return project_scope.proposal_sha256(self.snapshot(self.running,self.home)[0])

    def select(self, host='codex', version='1.0.16', marker='B'):
        self.running=self.bundle(host,version,marker)
        self.host=host
        shared.__file__=str(self.running/'memvane/learning/context_learning/shared.py')
        row={'name':'memvane','pluginId':'memvane@memvane-marketplace',
             'marketplaceName':'memvane-marketplace','version':version,'installed':True,'enabled':True,
             'source':{'source':'github','repo':'dulee/memvane'}}
        # Repository identity comes from the production allowlist.
        from memvane.learning.context_learning import adapter_binding
        row['source']['repo']=adapter_binding.REPOSITORY
        self.codex=row
        plugins=self.home/'.claude/plugins'
        self.write(plugins/'known_marketplaces.json', {'memvane-marketplace':{
            'source':row['source'],'installLocation':str(plugins/'marketplaces/memvane-marketplace')}})
        croot=self.bundle('claude',version,marker)
        self.claude={'id':'memvane@memvane-marketplace','enabled':True,'version':version,
                     'installPath':str(croot),'scope':'user'}
        self.write(plugins/'installed_plugins.json',{'plugins':{self.claude['id']:[{
            'scope':'user','version':version,'installPath':str(croot)}]}})

    def listing(self, argv, account, project):
        return {'installed':[deepcopy(self.codex)]} if argv[0]=='codex' else [deepcopy(self.claude)]

    def setUp(self):
        super().setUp()
        original_shared=shared.__file__
        self.addCleanup(setattr,shared,'__file__',original_shared)
        self.project=self.home/'project'
        self.settings=self.project/'.memvane/settings.json'
        self.write(self.settings,{'name':'fixture'})
        self.install=self.home/'install'
        self.install.mkdir(mode=0o700)
        self.store=self.install/'store.sqlite'
        self.store.touch(mode=0o600)
        self.decision={'projectRoot':str(self.project),'journalPath':str(self.install/'journal'),
                       'storePath':str(self.store),'installId':'fixture','state':'candidate'}
        self.root=default_enrollment.default_namespace(self.project,'fixture')
        self.model=self.home/'model'
        for name in adapter.MODEL_FILES:self.write(self.model/name, {})
        self.write(self.model/'rl_agent_config.json',{
            'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
        self.runtime=self.home/'python'
        self.runtime.write_text('synthetic interpreter',encoding='utf8')
        self.runtime.chmod(0o700)
        self.select(host=getattr(self,'initial_host','codex'),version='1.0.15',marker='A')
        stack=ExitStack()
        self.addCleanup(stack.close)
        for target,kwargs in (
            ('memvane.journal.api.select_route',{'return_value':self.decision}),
            ('memvane.journal.api.verify_selected_route_for_write',{'return_value':{'installId':'fixture'}}),
            ('memvane.journal.api.inspect_journal',{'return_value':{'maxPluginVersion':'1.0.15'}}),
            ('memvane.journal.api.publish_plugin_version',{}),
            ('memvane.learning.runtime_activation.ensure_pending',{}),
            ('memvane.learning.context_learning.host_inventory._project_listing',{'side_effect':self.listing}),
            ('memvane.learning.context_learning.adapter_binding._codex_installed',{'side_effect':lambda *a:deepcopy(self.codex)}),
            ('memvane.learning.context_learning.project_scope._code_snapshot',{'side_effect':self.snapshot}),
            ('memvane.learning.context_learning.project_scope.code_identity',{'side_effect':self.code}),
            ('memvane.learning.context_learning.inference_identity.code_identity',{'side_effect':self.code}),
            ('memvane.learning.context_learning.selector_heal.running_adapter',{'side_effect':lambda:self.running/'memvane/learning/context_learning/adapter.py'}),
            ('memvane.learning.context_learning.selector_heal.official_runtime',{'return_value':(str(self.runtime),str(self.model))}),
            ('memvane.learning.context_learning.selector_heal.pressure',{'return_value':None}),
            ('memvane.learning.context_learning.selector_heal.heal',{'return_value':{'status':'selector_ready'}}),
            ('memvane.learning.context_learning.selector_heal.warm',{'return_value':{'status':'selector_warm_current'}}),
            ('memvane.diagnostics.api.emit',{}),
        ):stack.enter_context(mock.patch(target,**kwargs))
        self.root.parent.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.assertEqual(default_enrollment.prepare(self.project,self.install,'fixture',lease_fd=-1)['status'],'selected')
        self.write(self.root/'laya-inference-manifest.json',{str(self.runtime):[[],file_sha(self.runtime)]})
        self.original=self.snapshot_targets()
        self.settings_before=(self.settings.read_bytes(),self.settings.stat().st_ino)

    def snapshot_targets(self):
        names=('shared-policy.json','live-selector.json',project_scope.NAME,learning_profile.INSTALL,learning_profile.NAME,setup.NAME)
        return {name:(self.root/name).read_bytes() for name in names}

    def maintenance(self):
        entry=Path(__file__).resolve().parents[3]/'entry/run.py'
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(sys,'argv',[str(entry),'selector-maintenance',str(self.project),'force']), redirect_stdout(out),redirect_stderr(err):
            with self.assertRaises(SystemExit) as ended:runpy.run_path(str(entry),run_name='__main__')
        self.assertEqual(ended.exception.code,0,err.getvalue())
        return out.getvalue(),err.getvalue()

    def renew(self):
        from memvane.learning.default_renewal import renew
        lease=selector_heal._prepare_lease(None)
        try:return renew(self.project,self.install,'fixture',lease_fd=lease)
        finally:os.close(lease)

    def interrupted(self, boundary):
        from memvane.learning import default_renewal as renewal
        restore=renewal._restore_bytes
        count=[0]
        def publish(*args,**kwargs):
            restore(*args,**kwargs)
            count[0]+=1
            if count[0]==boundary:raise InterruptedError('synthetic write interruption')
        self.select()
        with mock.patch.object(renewal,'_restore_bytes',side_effect=publish):
            with self.assertRaises(InterruptedError):self.renew()
        self.assertTrue((self.root/'project-learning-renewal.json').is_file())
