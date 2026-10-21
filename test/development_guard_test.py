"""Small sequential guard tests: no model, host probes or heavy child jobs."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'scripts/development_guard.py'
spec=importlib.util.spec_from_file_location('development_guard',path)
guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def proc(pid,ppid,group,start='start',rss=10,cpu=2):
    return {'pid':pid,'ppid':ppid,'pgid':group,'start':start,
            'rss_bytes':rss,'cpu_percent':cpu}


class GuardChecks(unittest.TestCase):
    def test_one_global_lease_excludes_other_kinds_and_checkouts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with guard.Lease(root):
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root):pass
            with guard.Lease(root):pass

    def test_symlink_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'other';target.write_text('unchanged')
            (root/'heavy.lock').symlink_to(target)
            with self.assertRaises(guard.GuardError):
                with guard.Lease(root):pass
            self.assertEqual(target.read_text(),'unchanged')

    def test_tree_includes_detached_descendant_and_retains_reparented_identity(self):
        tree=guard.OwnedTree(500)
        rows=[proc(500,1,500),proc(501,500,501),proc(502,501,502),proc(800,1,800)]
        owned=tree.observe(rows)
        self.assertEqual(set(owned),{500,501,502})
        rows=[proc(501,1,501),proc(502,501,502),proc(800,1,800)]
        self.assertEqual(set(tree.observe(rows)),{501,502})
        rows=[proc(501,1,900,start='reused'),proc(800,1,800)]
        self.assertEqual(tree.observe(rows),{})

    def test_reused_original_group_is_never_adopted_after_its_anchor_disappears(self):
        tree=guard.OwnedTree(500)
        tree.observe([proc(500,1,500),proc(501,500,501)])
        tree.observe([proc(501,1,501)])
        owned=tree.observe([proc(500,1,500,start='new-user'),proc(502,500,500,start='new-user'),proc(501,1,501)])
        self.assertEqual(set(owned),{501})

    def test_sampling_sleep_and_config_cannot_exceed_remaining_budget(self):
        self.assertEqual(guard.sleep_budget(5,2),2)
        self.assertEqual(guard.sleep_budget(5,-1),0)
        with self.assertRaises(guard.GuardError):guard.validate_budgets(86400,6)

    def test_home_override_does_not_create_another_user_wide_lock(self):
        from types import SimpleNamespace
        with patch.dict(os.environ,{'HOME':'/tmp/isolated-test-home'}), \
             patch.object(guard.pwd,'getpwuid',return_value=SimpleNamespace(pw_dir='/real-user-home')):
            self.assertTrue(str(guard.state_root()).startswith('/real-user-home/'))

    def test_whole_tree_rss_cpu_and_process_count_are_checked(self):
        policy=guard.Policy()
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        self.assertIsNone(guard.violation(policy,sample,[],0))
        self.assertEqual(guard.violation(policy,sample,[proc(1,0,1,rss=1000),proc(2,1,1,rss=1000)],0),'tree_rss')
        self.assertEqual(guard.violation(policy,sample,[proc(1,0,1,cpu=70),proc(2,1,1,cpu=70)],0),'tree_cpu')
        self.assertEqual(guard.violation(policy,sample,[proc(i,0,i,rss=0,cpu=0) for i in range(33)],0),'tree_process_count')

    def test_os_pressure_swap_growth_relative_available_and_unknown_stop(self):
        policy=guard.Policy()
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        for changes,reason in [({'pressure':'warning'},'os_memory_pressure'),
                ({'available_percent':34},'relative_available'),
                ({'available_percent':None},'monitor_unavailable'),
                ({'swap_bytes':101},'swap_growth'),({'load_per_cpu':2},'system_load')]:
            self.assertEqual(guard.violation(policy,{**sample,**changes},[],0),reason)

    def test_ordinary_regression_does_not_use_model_resource_limits(self):
        policy=guard.Policy();policy.resource_limits=False
        sample={'total_bytes':10000,'available_percent':20,'pressure':'warning',
                'swap_bytes':200,'load_per_cpu':2,'cpu_count':2}
        rows=[proc(500,1,500,rss=2000,cpu=100)]
        self.assertIsNone(guard.violation(policy,sample,rows,0))
        self.assertEqual(guard.violation(policy,sample,
            [proc(i,0,i) for i in range(33)],0),'tree_process_count')

    def test_actual_model_test_keeps_resource_limits_and_sequential_launcher(self):
        sample={'total_bytes':10000,'available_percent':70,'pressure':'warning',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        self.assertEqual(guard.violation(guard.Policy(),sample,[],0),'os_memory_pressure')
        sample['pressure']='normal'
        self.assertEqual(guard.violation(guard.Policy(),sample,[proc(500,1,500,cpu=150)],0),'tree_cpu')
        self.assertEqual(guard.command_for('model-test',['node','--test','one.test.mjs']),
            ['node','--test','--test-concurrency=1','one.test.mjs'])

    def test_model_test_child_gets_private_run_identity(self):
        from types import SimpleNamespace
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        with tempfile.TemporaryDirectory() as folder:
            child=SimpleNamespace(pid=500,poll=lambda:0)
            args=SimpleNamespace(kind='model-test',command=['node','--test','one.test.mjs'],
                max_seconds=60,sample_seconds=2)
            with patch.object(guard,'execution_hold',return_value=False), \
                 patch.object(guard,'system_sample',return_value=sample), \
                 patch.object(guard,'processes',side_effect=[[],[proc(500,1,500)],[]]), \
                 patch.object(guard.subprocess,'Popen',return_value=child) as launch, \
                 patch.object(guard.time,'sleep'):
                self.assertEqual(guard.run_job(args,Path(folder)),0)
            env=launch.call_args.kwargs['env']
            checkpoint=guard.json.loads((Path(env['QMD_DEVELOPMENT_GUARD_RUN'])/'checkpoint.json').read_text())
            self.assertEqual(env['QMD_DEVELOPMENT_GUARD_NONCE'],checkpoint['proofNonce'])
            self.assertEqual(checkpoint['kind'],'model-test')
            self.assertTrue(checkpoint['settled'])

    def test_warning_allows_mock_job_but_refuses_actual_model_before_launch(self):
        from types import SimpleNamespace
        sample={'total_bytes':10000,'available_percent':20,'pressure':'warning',
                'swap_bytes':200,'load_per_cpu':2,'cpu_count':2}
        for kind,code in [('test',0),('model-test',75)]:
            with tempfile.TemporaryDirectory() as folder:
                child=SimpleNamespace(pid=500,poll=lambda:0)
                args=SimpleNamespace(kind=kind,command=['node','--test'],max_seconds=60,sample_seconds=2)
                with patch.object(guard,'execution_hold',return_value=False), \
                     patch.object(guard,'system_sample',return_value=sample), \
                     patch.object(guard,'processes',side_effect=[[],[proc(500,1,500,cpu=150)],[]]), \
                     patch.object(guard.subprocess,'Popen',return_value=child) as launch, \
                     patch.object(guard.time,'sleep'):
                    self.assertEqual(guard.run_job(args,Path(folder)),code)
                    self.assertEqual(launch.call_count,1 if kind=='test' else 0)

    def test_test_launcher_forces_one_file_at_a_time(self):
        self.assertEqual(guard.command_for('test',['node','--test','test/one.test.mjs']),
                         ['node','--test','--test-concurrency=1','test/one.test.mjs'])
        for args in (['node','--test','--test-concurrency=4'],
                     ['node','--test','--test-concurrency','2'],['npm','test']):
            with self.assertRaises(guard.GuardError):guard.command_for('test',args)

    def test_stop_signals_only_verified_owned_identities(self):
        tree=guard.OwnedTree(500)
        tree.observe([proc(500,1,500),proc(501,500,501),proc(800,1,800)])
        signals=[]
        guard.signal_owned(tree,[proc(500,1,500),proc(501,1,900,start='reused'),proc(800,1,800)],
                           15,lambda pid,sig:signals.append((pid,sig)))
        self.assertEqual(signals,[(500,15)])

    def test_ps_parser_does_not_collect_command_arguments(self):
        parsed=guard.parse_processes('500 1 500 1024 3.5 Fri Oct 16 12:00:00 2026 /usr/bin/python\n')
        self.assertEqual(parsed[0]['rss_bytes'],1048576)
        self.assertEqual(parsed[0]['start'],'Fri Oct 16 12:00:00 2026')
        self.assertNotIn('command',parsed[0])

    def test_checkpoint_never_marks_resource_stop_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            guard.checkpoint(root,{'status':'stopped_resource','completed':False,'reason':'tree_rss'})
            import json
            self.assertFalse(json.loads((root/'checkpoint.json').read_text())['completed'])

    def test_unsettled_job_blocks_next_launch_before_any_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);guard.put(root/'active.json',{'settled':False})
            from types import SimpleNamespace
            args=SimpleNamespace(kind='backup',command=['python3','review.py'])
            with patch.object(guard,'system_sample') as sample,patch.object(guard,'execution_hold',return_value=False):
                with self.assertRaisesRegex(guard.GuardError,'unsettled'):
                    guard.run_job(args,root)
                sample.assert_not_called()

    def test_hold_refuses_every_child_kind_before_launch(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            for kind,cmd in [('test',['node','--test']),('backup',['python3','copy.py']),('hash',['python3','hash.py']),('review',['node','--test'])]:
                with patch.object(guard,'execution_hold',return_value=True),patch.object(guard.subprocess,'Popen') as launch:
                    with self.assertRaisesRegex(guard.GuardError,'hold'):
                        guard.run_job(SimpleNamespace(kind=kind,command=cmd),Path(folder))
                    launch.assert_not_called()

    def test_cleanup_reaps_owned_root_before_declaring_descendants_unsettled(self):
        tree=guard.OwnedTree(500);tree.observe([proc(500,1,500)])
        class Child:
            reaped=False
            def poll(self):self.reaped=True;return -15
        child=Child()
        with patch.object(guard,'processes',side_effect=lambda:[] if child.reaped else [proc(500,1,500)]), \
             patch.object(guard,'signal_owned'),patch.object(guard.time,'sleep'):
            self.assertTrue(guard.cleanup(tree,child))

    def test_cancelled_or_expired_preflight_never_launches_a_child(self):
        from types import SimpleNamespace
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        for reason in ('interrupted','wall_time_budget'):
            with tempfile.TemporaryDirectory() as folder:
                handlers={}
                def install(sig,handler):
                    if callable(handler):handlers[sig]=handler
                def preflight():
                    if reason=='interrupted':handlers[guard.signal.SIGINT](guard.signal.SIGINT,None)
                    return sample
                args=SimpleNamespace(kind='backup',command=['python3','copy.py'],max_seconds=1,sample_seconds=3)
                clock=iter((0,2)) if reason=='wall_time_budget' else None
                with patch.object(guard,'execution_hold',return_value=False), \
                     patch.object(guard.signal,'signal',side_effect=install), \
                     patch.object(guard,'system_sample',side_effect=preflight), \
                     patch.object(guard,'processes',return_value=[]), \
                     patch.object(guard.subprocess,'Popen',side_effect=AssertionError('child_launched')) as launch, \
                     patch.object(guard.time,'monotonic',side_effect=(lambda:next(clock)) if clock else (lambda:0)):
                    self.assertEqual(guard.run_job(args,Path(folder)),75)
                    launch.assert_not_called()
                import json
                state=json.loads((Path(folder)/'active.json').read_text())
                self.assertEqual(state['reason'],reason)
                self.assertFalse(state['completed']);self.assertTrue(state['settled'])


if __name__=='__main__':unittest.main()
