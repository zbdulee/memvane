"""Background bundle handoff uses only installed, verified synthetic host copies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
from unittest import mock

from tests.support.case import FixtureCase, wait_until

from memvane.runtime import host_bundle


class HostBundleHandoff(FixtureCase):
    def bundle(self, host, marketplace, version):
        root = self.home / ('.codex' if host == 'codex' else '.claude') / 'plugins' / 'cache' / marketplace / 'memvane' / version
        (root / 'entry').mkdir(parents=True)
        (root / ('.' + host + '-plugin')).mkdir()
        (root / 'plugin.json').write_text(json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
        (root / 'package.json').write_text(json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
        (root / ('.' + host + '-plugin') / 'plugin.json').write_text(
            json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
        (root / 'entry' / 'run.py').write_text('# synthetic entry\n', encoding='utf8')
        return root

    def test_removed_bundle_is_detected_and_latest_installed_bundle_is_selected(self):
        old = self.bundle('codex', 'market', '1.0.1')
        current = self.bundle('codex', 'market', '1.0.2')
        stale = self.bundle('codex', 'market', '1.9.9')
        lease = host_bundle.BundleIdentity(old)
        self.assertTrue(lease.intact())
        shutil.rmtree(old)
        self.assertFalse(lease.intact())
        installed = {'installed': [
            {'name': 'memvane', 'marketplaceName': 'market', 'version': '1.0.2',
             'installed': True, 'enabled': True},
            {'name': 'memvane', 'marketplaceName': 'market', 'version': '1.9.9',
             'installed': True, 'enabled': False},
        ]}
        with mock.patch.object(host_bundle, '_codex_inventory', return_value=installed):
            self.assertEqual(host_bundle.latest_installed(home=self.home), current)
        self.assertTrue(stale.exists())

    def test_replaced_entry_and_symlinked_or_version_mismatched_bundles_are_refused(self):
        root = self.bundle('codex', 'market', '1.0.2')
        lease = host_bundle.BundleIdentity(root)
        entry = root / 'entry' / 'run.py'
        replacement = entry.with_name('replacement.py')
        replacement.write_text('# replaced\n', encoding='utf8')
        replacement.replace(entry)
        self.assertFalse(lease.intact())
        (root / 'plugin.json').write_text('{"name":"memvane","version":"1.8.0"}', encoding='utf8')
        inventory = {'installed': [{'name': 'memvane', 'marketplaceName': 'market', 'version': '1.0.2',
                                    'installed': True, 'enabled': True}]}
        with mock.patch.object(host_bundle, '_codex_inventory', return_value=inventory):
            self.assertIsNone(host_bundle.latest_installed(home=self.home))
        (root / 'plugin.json').write_text('{"name":"memvane","version":"1.0.2"}', encoding='utf8')
        entry.unlink()
        entry.symlink_to(self.tmp / 'other.py')
        with mock.patch.object(host_bundle, '_codex_inventory', return_value=inventory):
            self.assertIsNone(host_bundle.latest_installed(home=self.home))

    def test_unproved_host_inventory_cannot_select_an_older_cache(self):
        self.bundle('claude', 'market', '1.0.1')
        with mock.patch.object(host_bundle, '_codex_inventory', side_effect=ValueError('unavailable')):
            self.assertIsNone(host_bundle.latest_installed(home=self.home))

    def test_a_still_present_older_bundle_is_not_preferred(self):
        old = self.bundle('codex', 'market', '1.0.1')
        self.bundle('codex', 'market', '1.0.2')
        identity = host_bundle.BundleIdentity(old)
        inventory = {'installed': [
            {'name': 'memvane', 'marketplaceName': 'market', 'version': version,
             'installed': True, 'enabled': True} for version in ('1.0.1', '1.0.2')]}
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': ''}), \
                mock.patch.object(host_bundle, 'fast_installed', return_value=self.home/'.codex/plugins/cache/market/memvane/1.0.2'):
            self.assertFalse(identity.preferred())

    def test_installed_hermes_copy_participates_in_version_order(self):
        self.bundle('codex', 'market', '1.0.2')
        root = self.home / '.hermes' / 'plugins' / 'memvane'
        (root / 'entry').mkdir(parents=True)
        (root / 'plugin.json').write_text('{"name":"memvane","version":"1.0.3"}', encoding='utf8')
        (root / 'entry' / 'run.py').write_text('# synthetic\n', encoding='utf8')
        inventory = {'installed': [{'name': 'memvane', 'marketplaceName': 'market', 'version': '1.0.2',
                                    'installed': True, 'enabled': True}]}
        with mock.patch.object(host_bundle, '_codex_inventory', return_value=inventory):
            self.assertEqual(host_bundle.latest_installed(home=self.home), root)

    def test_claude_inventory_must_name_the_exact_cache_path(self):
        root = self.bundle('claude', 'market', '1.0.2')
        registry = self.home / '.claude' / 'plugins' / 'installed_plugins.json'
        row = {'version': '1.0.2', 'installPath': str(root)}
        registry.write_text(json.dumps({'plugins': {'memvane@market': [row]}}), encoding='utf8')
        listed = {'id': 'memvane@market', 'version': '1.0.2', 'installPath': str(root), 'enabled': True}
        with mock.patch.object(host_bundle, '_codex_inventory', return_value={'installed': []}), \
                mock.patch.object(host_bundle, '_claude_listing', return_value=[listed], create=True):
            self.assertEqual(host_bundle.latest_installed(home=self.home), root)
            row['installPath'] = str(self.tmp)
            registry.write_text(json.dumps({'plugins': {'memvane@market': [row]}}), encoding='utf8')
            self.assertIsNone(host_bundle.latest_installed(home=self.home))
            row['installPath'] = str(root)
            registry.write_text(json.dumps({'plugins': {'memvane@market': [row]}}), encoding='utf8')
            listed['enabled'] = False
            self.assertIsNone(host_bundle.latest_installed(home=self.home))

    def test_codex_inventory_uses_the_host_cli_in_fixture_home(self):
        codex = self.tmp / 'codex'
        codex.write_text('#!/bin/sh\nprintf \'{"installed":[]}\\n\'\n', encoding='utf8')
        codex.chmod(0o700)
        with mock.patch.dict(os.environ, {'PATH': str(self.tmp)}):
            self.assertEqual(host_bundle._codex_inventory(self.home), {'installed': []})

    def test_codex_toml_enabled_plugin_selects_latest_cache(self):
        old = self.bundle('codex', 'market', '1.0.2')
        new = self.bundle('codex', 'market', '1.0.3')
        config = self.home/'.codex/config.toml'
        config.write_text('[plugins."memvane@market"]\nenabled = true\n')
        config.chmod(0o600)
        with mock.patch.dict(os.environ, {'CODEX_HOME':str(self.home/'.codex')}):
            self.assertEqual(host_bundle.fast_installed(home=self.home), new)
            config.write_text('[plugins."memvane@market"]\nenabled = false\n')
            self.assertIsNone(host_bundle.fast_installed(home=self.home))
        self.assertTrue(old.exists())

    def test_retired_timer_is_rearmed_in_same_wake(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=time.time()+300)
        with mock.patch.object(host_bundle, 'retire_old_helpers', return_value=1), \
             mock.patch.object(runtime, '_arm_timer', side_effect=[False, False, True]) as arm, \
             mock.patch.object(runtime.time, 'sleep') as sleep:
            self.assertEqual(runtime._wake(queue), 'idle')
        self.assertEqual(arm.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_group_writable_path_does_not_stop_spawn(self):
        unsafe = self.tmp / 'group-writable'
        unsafe.mkdir(mode=0o775); unsafe.chmod(0o775)
        executable = unsafe / 'codex'
        marker = self.tmp / 'untrusted-executed'
        executable.write_text(f'#!/bin/sh\n: > "{marker}"\n', encoding='utf8')
        executable.chmod(0o700)
        root = self.bundle('codex', 'market', '1.0.3')
        (root/'entry'/'run.py').write_text('print("SPAWNED")\n')
        with mock.patch.dict(os.environ, {'PATH':str(unsafe), 'MEMVANE_TEST_RUNTIME_ROOT':''}), \
             mock.patch('memvane.kernel.bundle_paths.plugin_root', return_value=root), \
             mock.patch.object(host_bundle, 'fast_installed', return_value=None):
            argv = host_bundle.entry_argv('runtime-work', 'worker', '4')
            child = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual((child.returncode, child.stdout.strip()), (0, 'SPAWNED'))
        self.assertEqual(argv[-4:], [str(root/'entry'/'run.py'), 'runtime-work', 'worker', '4'])
        self.assertFalse(marker.exists())

    def test_unknown_inventory_keeps_intact_resident(self):
        root = self.bundle('codex', 'market', '1.0.3')
        identity = host_bundle.BundleIdentity(root)
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT':''}), \
             mock.patch.object(host_bundle, 'fast_installed', return_value=None):
            self.assertTrue(identity.preferred())

    def test_transient_successor_metadata_failure_keeps_intact_resident(self):
        root = self.bundle('codex', 'market', '1.0.3')
        identity = host_bundle.BundleIdentity(root)
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT':''}), \
             mock.patch.object(host_bundle, 'fast_installed', return_value=self.tmp/'successor'), \
             mock.patch.object(host_bundle, '_read_json', side_effect=OSError('transient')):
            self.assertTrue(identity.preferred())

    def test_unsafe_host_settings_emit_inventory_event(self):
        codex = self.home / '.codex'; codex.mkdir(exist_ok=True)
        config = codex / 'config.toml'; config.write_text(''); config.chmod(0o660)
        claude = self.home / '.claude' / 'plugins'; claude.mkdir(parents=True, exist_ok=True)
        (claude / 'installed_plugins.json').write_text('{"plugins":{}}')
        settings = self.home / '.claude' / 'settings.json'; settings.symlink_to(config)
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex), 'CLAUDE_CONFIG_DIR': str(claude.parent)}), \
             mock.patch('memvane.diagnostics.api.emit') as emit:
            self.assertIsNone(host_bundle.fast_installed(home=self.home))
        hosts = {call.kwargs.get('host') for call in emit.call_args_list
                 if call.kwargs.get('reason') == 'host_inventory_unreadable'}
        self.assertEqual(hosts, {'codex', 'claude'})

    def test_host_cli_is_not_run_from_a_writable_path(self):
        unsafe = self.tmp / 'unsafe'
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        codex = unsafe / 'codex'
        marker = self.tmp / 'unsafe-ran'
        codex.write_text(f'#!/bin/sh\n: > "{marker}"\nprintf \'{{"installed":[]}}\\n\'\n', encoding='utf8')
        codex.chmod(0o700)
        with mock.patch.dict(os.environ, {'PATH': str(unsafe)}):
            self.assertEqual(host_bundle._codex_inventory(self.home), {'installed': []})
        self.assertFalse(marker.exists())

    def test_timer_waits_for_host_installation_gap_without_dropping_wake(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        tick = [0.0]
        identity = types.SimpleNamespace(intact=lambda: False)
        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', identity), \
                mock.patch.object(host_bundle, 'fast_installed', side_effect=[None, self.tmp]):
            result = runtime.timer(queue, wall=lambda: 1, clock=lambda: tick[0],
                                   sleep=lambda seconds: tick.__setitem__(0, tick[0] + .1),
                                   max_seconds=1)
        self.assertEqual(result, 'bundle_changed')
        self.assertEqual(len(queue.pending()), 1)

    def test_timer_handoff_retries_if_successor_disappears_during_spawn(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        waits = []
        with mock.patch.object(runtime, '_wake', side_effect=['unavailable', 'spawned']) as wake, \
                mock.patch.object(runtime.time, 'sleep', side_effect=waits.append):
            runtime._after_timer(queue, 'bundle_changed')
        self.assertEqual(wake.call_count, 2)
        self.assertEqual(waits, [runtime.TIMER_CHUNK_SECONDS])
        self.assertEqual(len(queue.pending()), 1)

    def test_removed_worker_stops_before_claiming_another_wake(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        identity = types.SimpleNamespace(intact=lambda: False)
        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', identity):
            result = runtime.drain(queue, execute=lambda row: self.fail('stale worker claimed a wake'),
                                   ready=lambda: True)
        self.assertEqual(result, {'status': 'bundle_changed', 'runs': 0})
        self.assertEqual(len(queue.pending()), 1)

    def test_removed_worker_releases_lease_and_waits_for_successor(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        lease = runtime._lock(queue.root / 'runtime-worker.lock')
        waits = []
        identity = types.SimpleNamespace(intact=lambda: False)
        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', identity), \
                mock.patch.object(runtime.time, 'sleep', side_effect=waits.append), \
                mock.patch.object(runtime, '_wake', side_effect=['unavailable', 'spawned']) as wake:
            result = runtime._drain_and_rewake(queue, lease, execute=lambda _: self.fail('claimed'))
        self.assertEqual(result['status'], 'bundle_changed')
        self.assertEqual(waits, [runtime.TIMER_CHUNK_SECONDS])
        self.assertEqual(wake.call_count, 2)
        with self.assertRaises(OSError):
            os.fstat(lease)
        self.assertEqual(len(queue.pending()), 1)

    def test_present_older_timer_is_retired_before_due_wake_is_processed(self):
        import fcntl
        from memvane.runtime import runtime_work as runtime
        old = self.bundle('codex', 'market', '1.0.1')
        new = self.bundle('codex', 'market', '1.0.2')
        (old/'entry'/'run.py').write_text('import time\nprint("READY",flush=True)\ntime.sleep(120)\n')
        marker = self.tmp/'new-worker.json'
        source = Path(__file__).resolve().parents[4]
        (new/'entry'/'run.py').write_text(
            'import json,os,sys\n'
            f'sys.path.insert(0,{str(source)!r})\n'
            'from memvane.runtime import runtime_work\n'
            f'open({str(marker)!r},"w").write(json.dumps({{"pid":os.getpid(),"entry":__file__}}))\n'
            'raise SystemExit(runtime_work.main(sys.argv[2:],execute=lambda row: True))\n')
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        lock = runtime._lock(queue.root/runtime.TIMER_LOCK)
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        child = subprocess.Popen([sys.executable,'-B',str(old/'entry'/'run.py'),
                                  'runtime-work','timer',str(lock)],
                                 pass_fds=(lock,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(lock)
        spawned = []
        def spawn(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs); spawned.append(process); return process
        process_api = types.SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL)
        def entry(name, *args, **_kwargs):
            return [sys.executable,'-B',str(new/'entry'/'run.py'),name,*args]
        try:
            self.assertEqual(child.stdout.readline().strip(), 'READY')
            with mock.patch.dict(os.environ, {'CODEX_HOME':str(self.home/'.codex')}), \
                 mock.patch.object(runtime, 'BACKGROUND_BUNDLE', host_bundle.BundleIdentity(new)), \
                 mock.patch.object(runtime, 'entry_argv', side_effect=entry), \
                 mock.patch.object(runtime, 'subprocess', process_api):
                self.assertEqual(runtime._wake(queue), 'spawned')
            self.assertEqual(child.wait(timeout=15), -15)
            wait_until(lambda: marker.exists() and not queue.pending(), timeout=30)
            self.assertEqual(json.loads(marker.read_text())['entry'], str(new/'entry'/'run.py'))
            self.assertEqual(len(spawned), 1)
            self.assertEqual(spawned[0].wait(timeout=30), 0)
            self.assertTrue(old.exists() and new.exists())
        finally:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=10)
            child.stdout.close(); child.stderr.close()

    def test_deleted_timer_bundle_hands_due_wake_to_new_bundle_worker(self):
        from memvane.runtime import runtime_work as runtime
        old = self.bundle('codex', 'market', '1.0.1')
        new = self.bundle('codex', 'market', '1.0.2')
        identity = host_bundle.BundleIdentity(old)
        marker = self.tmp / 'new-worker.json'
        source_root = Path(__file__).resolve().parents[4]
        (new / 'entry' / 'run.py').write_text(
            'import json,os,sys\n'
            f'sys.path.insert(0,{str(source_root)!r})\n'
            'from memvane.runtime import runtime_work\n'
            f'open({str(marker)!r},"w").write(json.dumps({{"pid":os.getpid(),"entry":__file__}}))\n'
            'raise SystemExit(runtime_work.main(sys.argv[2:],execute=lambda row: True))\n', encoding='utf8')
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        shutil.rmtree(old)
        tick = [0.0]

        def sleep(seconds):
            tick[0] += seconds

        def entry(name, *args, **_kwargs):
            return [sys.executable, '-B', str(new / 'entry' / 'run.py'), name, *args]

        spawned = []
        original_spawn = runtime.subprocess.Popen

        def spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            spawned.append(child)
            return child

        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', identity, create=True), \
                mock.patch.object(host_bundle, 'fast_installed', return_value=new), \
                mock.patch.object(runtime, 'entry_argv', side_effect=entry), \
                mock.patch.object(runtime.subprocess, 'Popen', side_effect=spawn):
            outcome = runtime.timer(queue, wake_once=lambda _: self.fail('old timer ran a due wake'),
                                    wall=lambda: 1, clock=lambda: tick[0], sleep=sleep,
                                    max_seconds=1)
            self.assertEqual(outcome, 'bundle_changed')
            runtime._after_timer(queue, outcome)
        wait_until(lambda: marker.exists() and not queue.pending(), timeout=30)
        observed = json.loads(marker.read_text(encoding='utf8'))
        self.assertEqual(observed['entry'], str(new / 'entry' / 'run.py'))
        self.assertEqual(len(queue.pending()), 0)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0].wait(timeout=30), 0)

    def test_running_timer_survives_its_tree_removal_and_hands_off(self):
        from memvane.runtime import runtime_work
        old = self.bundle('codex', 'market', '1.0.1')
        new = self.bundle('codex', 'market', '1.0.2')
        source = Path(__file__).resolve().parents[4] / 'memvane'
        for root in (old, new):
            shutil.copytree(source, root / 'memvane', ignore=shutil.ignore_patterns('tests', '__pycache__', '*.pyc'))
        ready, proceed, handled = self.tmp / 'timer-ready', self.tmp / 'timer-proceed', self.tmp / 'worker-handled'
        old_script = (
            'import fcntl,os,sys,time\n'
            'from pathlib import Path\n'
            'sys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n'
            'from memvane.runtime import host_bundle\n'
            f'new=Path({str(new)!r})\n'
            'host_bundle.fast_installed=lambda: new if new.is_dir() else None\n'
            'host_bundle.entry_argv=lambda name,*args,**kw: [sys.executable,"-B",str(new/"entry"/"run.py"),name,*args]\n'
            'from memvane.runtime import runtime_work as r\n'
            'r.TIMER_CHUNK_SECONDS=.05; r.TIMER_MIN_SLEEP_SECONDS=.01\n'
            'q=r.WakeQueue(); fd=r._lock(q.root/r.TIMER_LOCK); fcntl.flock(fd,fcntl.LOCK_EX)\n'
            'def sleep(seconds):\n'
            f'    Path({str(ready)!r}).write_text("ready")\n'
            f'    while not Path({str(proceed)!r}).exists(): time.sleep(.01)\n'
            '    time.sleep(.01)\n'
            'outcome=r.timer(q,max_seconds=30,sleep=sleep); os.close(fd); r._after_timer(q,outcome)\n'
            'raise SystemExit(0 if outcome=="bundle_changed" else 2)\n')
        new_script = (
            'import os,sys\n'
            'from pathlib import Path\n'
            'sys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n'
            'from memvane.runtime import runtime_work as r\n'
            'def execute(row):\n'
            f'    Path({str(handled)!r}).write_text(str(os.getpid()))\n'
            '    return True\n'
            'raise SystemExit(r.main(sys.argv[2:],execute=execute))\n')
        (old / 'entry' / 'run.py').write_text(old_script, encoding='utf8')
        (new / 'entry' / 'run.py').write_text(new_script, encoding='utf8')
        queue = runtime_work.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=time.time() + 3600)
        child = subprocess.Popen([sys.executable, '-B', str(old / 'entry' / 'run.py')],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_until(lambda: ready.exists() or child.poll() is not None, timeout=30)
            self.assertTrue(ready.exists(), child.communicate(timeout=2) if child.poll() is not None else '')
            shutil.rmtree(old)
            queue.offer('laya-sync', '/synthetic/project', now=0, pull_due=True)
            proceed.write_text('continue', encoding='utf8')
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, (out, err))
            wait_until(lambda: handled.exists() and not queue.pending(), timeout=30)
            self.assertEqual(len(queue.pending()), 0)
            self.assertGreater(int(handled.read_text()), 1)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)
