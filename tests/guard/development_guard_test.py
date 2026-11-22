"""Small sequential guard tests: no model, host probes or heavy child jobs."""
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[2]/'scripts/development_guard.py'
spec=importlib.util.spec_from_file_location('development_guard',path)
guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def proc(pid,ppid,group,start='start',rss=10,cpu=2):
    return {'pid':pid,'ppid':ppid,'pgid':group,'start':start,
            'rss_bytes':rss,'cpu_percent':cpu}


class GuardChecks(unittest.TestCase):
    def test_review_and_hash_use_guard_without_model_resource_limits(self):
        from types import SimpleNamespace
        argv=['development_guard.py','--kind','review','--','codex','exec','-s','read-only','-']
        with patch.object(sys,'argv',argv), patch.object(guard,'state_root',return_value=Path('/synthetic')), \
             patch.object(guard,'run_job',return_value=0) as run:
            self.assertEqual(guard.main(),0)
        args=run.call_args.args[0]
        self.assertEqual(args.kind,'review')
        self.assertEqual(args.command,['codex','exec','-s','read-only','-'])
        sample={'total_bytes':10000,'available_percent':20,'pressure':'warning',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        for kind,command in [('review',args.command),('hash',['git','status'])]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder, \
                 patch.object(guard,'execution_hold',return_value=False), \
                 patch.object(guard,'system_sample',return_value=sample), \
                 patch.object(guard,'processes',side_effect=[[],[proc(500,1,500)],[]]), \
                 patch.object(guard.subprocess,'Popen',return_value=SimpleNamespace(pid=500,poll=lambda:0)), \
                 patch.object(guard.time,'sleep'):
                self.assertEqual(guard.run_job(SimpleNamespace(kind=kind,command=command,
                    max_seconds=60,sample_seconds=2),Path(folder)),0)
                state=guard.json.loads((Path(folder)/'active.json').read_text())
                self.assertEqual(state['kind'],kind)
                self.assertFalse(state['resourceLimits'])
                self.assertTrue(state['settled'] and state['completed'])

    def test_one_global_lease_excludes_other_kinds_and_checkouts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with guard.Lease(root):
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root):pass
            with guard.Lease(root):pass

    def test_ordinary_tests_share_three_slots_and_a_fourth_is_busy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with guard.Lease(root,shared=True) as a, guard.Lease(root,shared=True) as b, \
                 guard.Lease(root,shared=True) as c:
                self.assertEqual(sorted((a.slot,b.slot,c.slot)),[0,1,2])
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root,shared=True):pass
                freed=b.slot;b.__exit__(None,None,None)
                with guard.Lease(root,shared=True) as again:self.assertEqual(again.slot,freed)
            self.assertEqual(guard.TEST_SLOTS,3)

    def test_shared_tests_and_exclusive_jobs_exclude_each_other(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with guard.Lease(root,shared=True):
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root):pass
            with guard.Lease(root):
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root,shared=True):pass
            with guard.Lease(root,shared=True):pass

    def test_older_copy_exclusive_heavy_lock_still_excludes_shared_tests(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def older_copy():
                # What an older checkout's guard does: LOCK_EX on heavy.lock only.
                fd=os.open(root/'heavy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);return fd
            fd=older_copy()
            try:
                with self.assertRaisesRegex(guard.GuardError,'busy'):
                    with guard.Lease(root,shared=True):pass
            finally:os.close(fd)
            with guard.Lease(root,shared=True):
                with self.assertRaises(BlockingIOError):older_copy()

    def test_shared_lease_rejects_unsafe_slot_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'other';target.write_text('unchanged')
            (root/'test-slot-0.lock').symlink_to(target)
            with self.assertRaises(guard.GuardError):
                with guard.Lease(root,shared=True):pass
            self.assertEqual(target.read_text(),'unchanged')

    def test_lease_mode_follows_kind_exclusive_flag_and_calibration(self):
        from types import SimpleNamespace
        cases=[(SimpleNamespace(kind='test'),True),
               (SimpleNamespace(kind='test',exclusive=True),False),
               (SimpleNamespace(kind='test',calibration_envelope='/x.json'),False),
               (SimpleNamespace(kind='model-test'),False),(SimpleNamespace(kind='backup'),False),
               (SimpleNamespace(kind='hash'),False),(SimpleNamespace(kind='review'),False)]
        for args,shared in cases:
            with self.subTest(args=args):self.assertEqual(guard.shared_test(args),shared)
        argv=['development_guard.py','--kind','test','--exclusive','--','node','--test']
        with patch.object(sys,'argv',argv), patch.object(guard,'state_root',return_value=Path('/synthetic')), \
             patch.object(guard,'run_job',return_value=0) as run:
            self.assertEqual(guard.main(),0)
        self.assertTrue(run.call_args.args[0].exclusive)

    def test_shared_runs_keep_state_per_slot_and_exclusive_runs_check_every_slot(self):
        from types import SimpleNamespace
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        def launch(root,args):
            child=SimpleNamespace(pid=500,poll=lambda:0)
            with patch.object(guard,'execution_hold',return_value=False), \
                 patch.object(guard,'system_sample',return_value=sample), \
                 patch.object(guard,'processes',side_effect=[[],[proc(500,1,500)],[]]), \
                 patch.object(guard.subprocess,'Popen',return_value=child) as popen, \
                 patch.object(guard.time,'sleep'):
                return guard.run_job(args,root),popen
        test=lambda:SimpleNamespace(kind='test',command=['node','--test'],max_seconds=60,sample_seconds=2)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            # Two neighbours are running: their unsettled slot state does not block a third.
            for slot in (0,1):guard.put(root/f'active-test-slot-{slot}.json',{'settled':False,'status':'running'})
            with guard.Lease(root,shared=True), guard.Lease(root,shared=True):
                code,popen=launch(root,test())
                self.assertEqual(code,0)
                self.assertEqual(len(popen.call_args.kwargs['pass_fds']),2)
            state=guard.json.loads((root/'active-test-slot-2.json').read_text())
            self.assertTrue(state['settled'] and state['completed'])
            self.assertEqual(state['slot'],2)
            self.assertFalse((root/'active.json').exists())
            # A free slot that still records an unsettled run is a leftover and blocks.
            with self.assertRaisesRegex(guard.GuardError,'unsettled'):launch(root,test())
            # Exclusive work sees leftovers in any slot.
            guard.put(root/'active-test-slot-0.json',{'settled':True})
            with self.assertRaisesRegex(guard.GuardError,'unsettled'):
                launch(root,SimpleNamespace(kind='hash',command=['git','status'],max_seconds=60,sample_seconds=2))
            guard.put(root/'active-test-slot-1.json',{'settled':True})
            code,popen=launch(root,SimpleNamespace(kind='test',exclusive=True,command=['node','--test'],
                max_seconds=60,sample_seconds=2))
            self.assertEqual(code,0)
            self.assertEqual(len(popen.call_args.kwargs['pass_fds']),1)
            state=guard.json.loads((root/'active.json').read_text())
            self.assertTrue(state['settled'] and state['completed'])
            self.assertNotIn('slot',state)

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
        self.assertIsNone(guard.violation(policy,{**sample,'pressure':'warning'},[],0))
        self.assertIsNone(guard.violation(policy,{**sample,'swap_bytes':1000},[],0))
        for changes,reason in [({'pressure':'critical'},'os_memory_pressure'),
                ({'available_percent':19},'relative_available'),
                ({'available_percent':None},'monitor_unavailable'),
                ({'swap_bytes':1001},'swap_growth'),({'load_per_cpu':2},'system_load')]:
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
        self.assertIsNone(guard.violation(guard.Policy(),sample,[],0))
        self.assertIsNone(guard.violation(guard.Policy(),{**sample,'available_percent':20},[],0))
        self.assertEqual(guard.violation(guard.Policy(),{**sample,'available_percent':19},[],0),'relative_available')
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

    def test_test_child_gets_a_private_temp_runtime_root_never_an_inherited_bad_one(self):
        from types import SimpleNamespace
        from memvane.runtime.api import in_platform_temp
        sample={'total_bytes':10000,'available_percent':70,'pressure':'normal',
                'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}
        for inherited in ('relative/root','/usr','/tmp',None):
            with self.subTest(inherited=inherited), tempfile.TemporaryDirectory() as folder:
                child=SimpleNamespace(pid=500,poll=lambda:0)
                args=SimpleNamespace(kind='test',command=['node','--test','one.test.mjs'],
                    max_seconds=60,sample_seconds=2)
                env_patch={'MEMVANE_TEST_RUNTIME_ROOT':inherited or ''}
                with patch.dict(guard.os.environ,env_patch), \
                     patch.object(guard,'execution_hold',return_value=False), \
                     patch.object(guard,'system_sample',return_value=sample), \
                     patch.object(guard,'processes',side_effect=[[],[proc(500,1,500)],[]]), \
                     patch.object(guard.subprocess,'Popen',return_value=child) as launch, \
                     patch.object(guard.time,'sleep'):
                    self.assertEqual(guard.run_job(args,Path(folder)),0)
                root=launch.call_args.kwargs['env']['MEMVANE_TEST_RUNTIME_ROOT']
                self.assertNotEqual(root,inherited)
                self.assertTrue(os.path.isabs(root) and in_platform_temp(root),root)
                # The run's own root is removed once the run settles.
                self.assertFalse(os.path.exists(root))
        # A usable inherited root (private dir in the platform temp area) is kept.
        with tempfile.TemporaryDirectory(dir=guard.trusted_temp_dir()) as keep:
            os.chmod(keep,0o700)
            self.assertTrue(guard.usable_test_runtime_root(os.path.realpath(keep)))
            os.chmod(keep,0o755)
            self.assertFalse(guard.usable_test_runtime_root(os.path.realpath(keep)))

    def test_low_available_allows_mock_job_but_refuses_actual_model(self):
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

    def _run(self,folder,rows,samples,child,**patches):
        """One guarded ``hash`` job over scripted process listings and system samples."""
        from types import SimpleNamespace
        args=SimpleNamespace(kind='hash',command=['git','status'],max_seconds=60,sample_seconds=2)
        with patch.object(guard,'execution_hold',return_value=False), \
             patch.object(guard,'system_sample',side_effect=samples), \
             patch.object(guard,'processes',side_effect=rows), \
             patch.object(guard.subprocess,'Popen',return_value=child), \
             patch.object(guard.time,'sleep'):
            code=guard.run_job(args,Path(folder))
        return code,guard.json.loads((Path(folder)/'active.json').read_text())

    SAMPLE={'total_bytes':10000,'available_percent':70,'pressure':'normal',
            'swap_bytes':0,'load_per_cpu':0,'cpu_count':4}

    def test_two_failed_observations_in_a_row_do_not_stop_the_run(self):
        from types import SimpleNamespace
        lost=guard.GuardError('monitor_unavailable')
        alive=[proc(500,1,500)]
        # preflight, sample, two failures (process list, then system sample), recovery, exit.
        rows=iter([[],alive,lost,alive,alive,[]])
        samples=iter([self.SAMPLE,self.SAMPLE,lost,self.SAMPLE,self.SAMPLE])
        def step(source):
            value=next(source)
            if isinstance(value,Exception):raise value
            return value
        polls=iter([None,None,0])
        child=SimpleNamespace(pid=500,poll=lambda:next(polls))
        with tempfile.TemporaryDirectory() as folder, patch.object(guard,'cleanup') as cleanup:
            code,state=self._run(folder,lambda:step(rows),lambda:step(samples),child)
            cleanup.assert_not_called()
        self.assertEqual(code,0)
        self.assertEqual((state['status'],state['settled'],state['monitorFailures']),('passed',True,2))

    def test_three_failed_observations_in_a_row_stop_and_settle_the_tree(self):
        from types import SimpleNamespace
        lost=guard.GuardError('monitor_unavailable')
        alive=[proc(500,1,500),proc(501,500,501)]
        killed=[]
        state={'listing':iter([[],alive,lost,lost,lost])}
        def rows():
            # Loop listings first; cleanup then sees the tree until both are signalled.
            try:value=next(state['listing'])
            except StopIteration:return [] if len(killed)>=2 else alive
            if isinstance(value,Exception):raise value
            return value
        child=SimpleNamespace(pid=500,poll=lambda:None)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(guard.os,'kill',side_effect=lambda pid,sig:killed.append((pid,sig))):
            code,saved=self._run(folder,rows,lambda:self.SAMPLE,child)
        self.assertEqual(code,75)
        self.assertEqual((saved['status'],saved['reason'],saved['settled'],saved['monitorFailures']),
                         ('stopped','monitor_unavailable',True,3))
        self.assertEqual(sorted(killed),[(500,guard.signal.SIGTERM),(501,guard.signal.SIGTERM)])

    def test_a_malformed_sample_counts_as_a_failed_observation(self):
        from types import SimpleNamespace
        bad=dict(self.SAMPLE,pressure='unknown')
        alive=[proc(500,1,500)]
        rows=iter([[],alive,alive,[]])
        polls=iter([None,0])
        child=SimpleNamespace(pid=500,poll=lambda:next(polls))
        args=SimpleNamespace(kind='backup',command=['python3','copy.py'],max_seconds=60,sample_seconds=2)
        with tempfile.TemporaryDirectory() as folder, patch.object(guard,'execution_hold',return_value=False), \
             patch.object(guard,'system_sample',side_effect=[self.SAMPLE,bad,self.SAMPLE,self.SAMPLE]), \
             patch.object(guard,'processes',side_effect=lambda:next(rows)), \
             patch.object(guard.subprocess,'Popen',return_value=child), \
             patch.object(guard.time,'sleep'), patch.object(guard,'cleanup') as cleanup:
            self.assertEqual(guard.run_job(args,Path(folder)),0)
            cleanup.assert_not_called()
            state=guard.json.loads((Path(folder)/'active.json').read_text())
        self.assertEqual((state['status'],state['monitorFailures']),('passed',1))

    def test_only_the_full_suite_may_ask_for_two_or_three_files_at_a_time(self):
        for form in (['--test-concurrency=3'],['--test-concurrency','2']):
            args=['node','--test',*form,'one.test.mjs']
            self.assertEqual(guard.command_for('test',args,True),args)
            with self.assertRaises(guard.GuardError):guard.command_for('test',args)
            with self.assertRaises(guard.GuardError):guard.command_for('model-test',args,False)
        for form in (['--test-concurrency=4'],['--test-concurrency','0'],['--test-concurrency=x']):
            with self.assertRaises(guard.GuardError):
                guard.command_for('test',['node','--test',*form],True)
        self.assertEqual(guard.command_for('test',['node','--test','--test-concurrency=3','a'],True,1),
                         ['node','--test','--test-concurrency=1','a'])
        self.assertEqual(guard.command_for('test',['node','--test','--test-concurrency','2','a'],True,1),
                         ['node','--test','--test-concurrency','1','a'])
        self.assertEqual([guard.test_concurrency(c) for c in (
            ['node','--test','--test-concurrency=3'],['node','--test','--test-concurrency','2'],
            ['node','--test'])],[3,2,1])

    def _full(self,folder,pressure,command,exclusive=True):
        from types import SimpleNamespace
        child=SimpleNamespace(pid=500,poll=lambda:0)
        args=SimpleNamespace(kind='test',command=command,max_seconds=60,sample_seconds=2,
                             exclusive=exclusive)
        with patch.object(guard,'execution_hold',return_value=False), \
             patch.object(guard,'system_sample',return_value=dict(self.SAMPLE,pressure=pressure)), \
             patch.object(guard,'processes',side_effect=[[],[proc(500,1,500)],[]]), \
             patch.object(guard.subprocess,'Popen',return_value=child) as launch, \
             patch.object(guard.time,'sleep'):
            code=guard.run_job(args,Path(folder))
        name='active.json' if exclusive else 'active-test-slot-0.json'
        return code,launch,guard.json.loads((Path(folder)/name).read_text())

    def test_the_full_suite_runs_three_files_at_a_time_only_under_normal_pressure(self):
        command=['node','--test','--test-concurrency=3','one.test.mjs']
        for pressure,applied in (('normal',3),('warning',1)):
            with tempfile.TemporaryDirectory() as folder:
                code,launch,state=self._full(folder,pressure,command)
                self.assertEqual(code,0)
                self.assertEqual(launch.call_args.args[0],
                                 ['node','--test',f'--test-concurrency={applied}','one.test.mjs'])
                self.assertEqual((state['testConcurrencyRequested'],state['testConcurrency'],
                                  state['pressure']),(3,applied,pressure))

    def test_a_focused_test_stays_one_file_at_a_time(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(guard.GuardError):
                self._full(folder,'normal',['node','--test','--test-concurrency=3'],exclusive=False)
            code,launch,state=self._full(folder,'normal',['node','--test','one.test.mjs'],exclusive=False)
            self.assertEqual(launch.call_args.args[0],['node','--test','--test-concurrency=1','one.test.mjs'])
            self.assertEqual((state['testConcurrencyRequested'],state['testConcurrency']),(1,1))

    def _samples(self,folder):
        run=next((Path(folder)/'runs').iterdir())
        return [guard.json.loads(line) for line in (run/'samples.jsonl').read_text().splitlines()]

    def test_a_failed_observation_is_written_to_the_samples(self):
        from types import SimpleNamespace
        lost=guard.GuardError('monitor_unavailable')
        alive=[proc(500,1,500)]
        rows=iter([[],alive,alive,alive,[]])
        samples=iter([self.SAMPLE,self.SAMPLE,lost,self.SAMPLE,self.SAMPLE])
        def step(source):
            value=next(source)
            if isinstance(value,Exception):raise value
            return value
        polls=iter([None,None,0])
        child=SimpleNamespace(pid=500,poll=lambda:next(polls))
        with tempfile.TemporaryDirectory() as folder:
            code,state=self._run(folder,lambda:step(rows),lambda:step(samples),child)
            lines=self._samples(folder)
        self.assertEqual((code,state['monitorFailures']),(0,1))
        failed=[line for line in lines if line.get('monitor_failed')]
        self.assertEqual([(line['reason'],line['owned_count']) for line in failed],
                         [('monitor_unavailable',1)])
        self.assertEqual(len(lines),5)

    def test_five_failed_observations_in_the_last_ten_stop_the_run(self):
        from types import SimpleNamespace
        lost=guard.GuardError('monitor_unavailable')
        alive=[proc(500,1,500)]
        # Never three in a row: fail, fail, ok, repeated.
        pattern=[lost,lost,self.SAMPLE]*3
        samples=iter([self.SAMPLE,*pattern])
        def sample():
            value=next(samples)
            if isinstance(value,Exception):raise value
            return value
        listed=[]
        def rows():
            listed.append(1)
            return [] if len(listed)==1 else alive
        child=SimpleNamespace(pid=500,poll=lambda:None)
        with tempfile.TemporaryDirectory() as folder, patch.object(guard,'cleanup',return_value=True):
            code,state=self._run(folder,rows,sample,child)
            lines=self._samples(folder)
        self.assertEqual((code,state['status'],state['reason'],state['monitorFailures']),
                         (75,'stopped','monitor_unavailable',5))
        # The baseline, then fail fail ok fail fail ok fail: the fifth failure of seven samples.
        self.assertEqual([bool(line.get('monitor_failed')) for line in lines],
                         [False,True,True,False,True,True,False,True])

    def test_a_listed_tree_over_the_process_limit_stops_without_a_system_sample(self):
        from types import SimpleNamespace
        lost=guard.GuardError('monitor_unavailable')
        crowd=[proc(500,1,500)]+[proc(600+i,500,500) for i in range(guard.Policy().max_processes)]
        listed=[]
        def rows():
            listed.append(1)
            return [] if len(listed)==1 else crowd
        samples=iter([self.SAMPLE,lost])
        def sample():
            value=next(samples)
            if isinstance(value,Exception):raise value
            return value
        child=SimpleNamespace(pid=500,poll=lambda:None)
        with tempfile.TemporaryDirectory() as folder, patch.object(guard,'cleanup',return_value=True):
            code,state=self._run(folder,rows,sample,child)
        self.assertEqual((code,state['status'],state['reason'],state['monitorFailures']),
                         (75,'stopped','tree_process_count',1))

    def test_cleanup_reads_a_failed_listing_again_and_waits_for_a_slow_exit(self):
        tree=guard.OwnedTree(500);tree.observe([proc(500,1,500),proc(501,500,501)])
        alive=[proc(500,1,500),proc(501,500,501)]
        lost=guard.GuardError('monitor_unavailable')
        # A failed listing, TERM, still there, KILL, three more listings before it is gone.
        script=iter([lost,alive,alive,alive,alive,alive,alive,alive,alive,[proc(501,1,501)],[proc(501,1,501)],[]])
        calls=[]
        def rows():
            calls.append(1);value=next(script)
            if isinstance(value,Exception):raise value
            return value
        signals=[]
        class Child:
            pid=500
            def poll(self):return None
        with patch.object(guard,'processes',side_effect=rows),patch.object(guard.time,'sleep'), \
             patch.object(guard.os,'kill',side_effect=lambda pid,sig:signals.append((pid,sig))):
            self.assertTrue(guard.cleanup(tree,Child()))
        self.assertEqual(len(calls),12)
        self.assertEqual(sorted(signals),[(500,guard.signal.SIGKILL),(500,guard.signal.SIGTERM),
                                          (501,guard.signal.SIGKILL),(501,guard.signal.SIGTERM)])

    def test_cleanup_without_a_process_list_signals_only_the_unreaped_root(self):
        tree=guard.OwnedTree(500);tree.observe([proc(500,1,500),proc(501,500,501)])
        signals=[];waits=[]
        class Child:
            pid=500;code=None
            def poll(self):return self.code
            def wait(self,timeout=None):
                waits.append(timeout);self.code=-15;return self.code
        with patch.object(guard,'processes',side_effect=guard.GuardError('monitor_unavailable')), \
             patch.object(guard.time,'sleep'), \
             patch.object(guard.os,'kill',side_effect=lambda pid,sig:signals.append((pid,sig))):
            self.assertFalse(guard.cleanup(tree,Child()))
        self.assertEqual(signals,[(500,guard.signal.SIGTERM)])
        self.assertEqual(waits,[2])

    def test_a_reaped_root_gets_no_signal_when_the_process_list_is_unreadable(self):
        tree=guard.OwnedTree(500);tree.observe([proc(500,1,500)])
        class Child:
            pid=500
            def poll(self):return 0
        with patch.object(guard,'processes',side_effect=guard.GuardError('monitor_unavailable')), \
             patch.object(guard.time,'sleep'),patch.object(guard.os,'kill') as kill:
            self.assertFalse(guard.cleanup(tree,Child()))
            kill.assert_not_called()

    def test_the_budget_counts_awake_time_and_the_state_records_the_sleep(self):
        from types import SimpleNamespace
        alive=[proc(500,1,500)]
        rows=iter([[],alive,[]])
        child=SimpleNamespace(pid=500,poll=lambda:0)
        # 2,135 s asleep between the first and the last reading of the two clocks.
        clocks=iter([(100.0,1000.0)])
        def reading():return next(clocks,(120.0,3155.0))
        with tempfile.TemporaryDirectory() as folder, patch.object(guard,'sleep_clocks',side_effect=reading), \
             patch.object(guard.time,'monotonic',return_value=5.0):
            code,state=self._run(folder,lambda:next(rows),lambda:self.SAMPLE,child)
            first,*_,last=[guard.json.loads(line) for line in
                           (Path(state['run'])/'samples.jsonl').read_text().splitlines()]
        self.assertEqual((code,state['status'],state['sleptSeconds']),(0,'passed',2135.0))
        self.assertEqual(last['slept_seconds'],2135.0)
        self.assertNotIn('slept_seconds',first)

    def test_slept_seconds_is_absent_without_the_two_clocks(self):
        self.assertIsNone(guard.slept_seconds(None))
        with patch.object(guard,'sleep_clocks',return_value=None):
            self.assertIsNone(guard.slept_seconds((1.0,2.0)))
        with patch.object(guard,'sleep_clocks',return_value=(3.0,2.5)):
            self.assertEqual(guard.slept_seconds((1.0,2.0)),0.0)
        self.assertEqual(len(guard.sleep_clocks()),2)


if __name__=='__main__':unittest.main()
