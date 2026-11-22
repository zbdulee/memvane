"""Synthetic two-bundle handoff with owned model-free processes."""
import asyncio
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Child entry points execute the actual runtime and resident loops with synthetic
# ownership and a model-free socket worker. All roots belong to this fixture.
def child(role, home, old, new, inherited_fd=None):
    from types import SimpleNamespace
    from memvane.runtime import runtime_work as runtime, host_bundle, runtime_monitor
    from memvane.resident import resident_service as resident
    from memvane.resident.tests.support import Handle, manifest_for
    import fcntl

    home, old, new = map(Path, (home, old, new))
    switching, shutdown = home/'switch', home/'shutdown'
    version = 'old' if str(Path(sys.argv[0])).startswith(str(old)) else 'new'
    queue = runtime.WakeQueue(home/'queue')
    identity = SimpleNamespace(intact=lambda:True,
        preferred_background=lambda:version == 'new' or not switching.exists())
    runtime.BACKGROUND_BUNDLE = identity
    runtime_monitor.BACKGROUND_BUNDLE = identity
    resident.BACKGROUND_BUNDLE = identity
    host_bundle.background_installed = lambda:new if switching.exists() else old
    host_bundle.retire_old_helpers = lambda *args, **kwargs:0
    runtime.TIMER_CHUNK_SECONDS = .05
    def entry(name, *args, **kwargs):
        assert name == 'runtime-work' and args[0] == 'timer'
        return [sys.executable, '-B', str(new/'entry/run.py'), 'timer', str(home), str(old), str(new), args[1]]
    runtime.entry_argv = entry
    if role == 'timer':
        if inherited_fd is None:
            fd = runtime._lock(queue.root/runtime.TIMER_LOCK)
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd = int(inherited_fd)
        (home/('timer-'+version)).write_text(str(os.getpid()))
        try:
            outcome = runtime.timer(queue, max_seconds=60)
        finally:
            os.close(fd)
        if version == 'old':
            assert outcome == 'bundle_changed', outcome
            runtime._after_timer(queue, outcome)
        (home/('timer-'+version+'-done')).write_text(outcome)
        return
    if role == 'monitor':
        (home/('monitor-'+version)).write_text(str(os.getpid()))
        report = lambda:{'ready':True,'observed_at_mono':time.monotonic(),'total_mib':16000,
                         'available_mib':12000,'load_per_cpu':.1,
                         'query_status':{'memory_pressure':'ok'},
                         'memory_pressure':{'level':'normal','system_free_percent':70}}
        result = runtime_monitor.run(queue, probe=report, wake=lambda:None,
            alive=lambda:not shutdown.exists(), wait=lambda _:time.sleep(.05))
        (home/('monitor-'+version+'-done')).write_text(result['status'])
        return
    assert role == 'resident'
    root, project = home/'resident', home/'project'
    manifest = manifest_for(home/'worker.sock')
    decision = {'projectRoot':str(project),'pluginRoot':'/synthetic/plugin','installId':'fixture',
                'journalPath':str(home/'journal'),'scopeId':'fixture','storePath':str(home/'store'),
                'storeIncarnation':'fixture','vectorProfile':{'embeddingFingerprint':manifest['embeddingFingerprint']}}
    def prove(_project):
        return decision, {'providerManifest':manifest}, 'selected'
    def start_worker(value, admit):
        assert admit() is True
        args = [sys.executable,'-B','-m','memvane.resident.tests.support','--fake-worker',
                value['serviceSocketPath'],json.dumps({key:value[key] for key in ('approvedProviderId',
                'providerArtifactSha256','embeddingFingerprint')}),'.01',str(home/'pending'),
                str(home/'slow'),str(value['dimension'])]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert process.stdout.readline() == b'READY\n'
        (home/('worker-'+version)).write_text(str(process.pid))
        return Handle(process, value['serviceSocketPath'])
    def spawn_successor(*args, **kwargs):
        subprocess.Popen([sys.executable,'-B',str(new/'entry/run.py'),'resident',str(home),str(old),str(new)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    resident._spawn = spawn_successor
    service = resident.Service(root, bundle={'version':'1.0.0' if version=='old' else '1.0.1',
        'digest':version}, prove=prove, attest=lambda *_:None, start_worker=start_worker,
        admit=lambda:True, tick=.05)
    assert service.acquire(wait=10)
    resident.register(str(project), root=root)
    service.start()
    (home/('resident-'+version)).write_text(str(os.getpid()))
    if version == 'new':
        subprocess.Popen([sys.executable,'-B',str(new/'entry/run.py'),'monitor',str(home),str(old),str(new)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not service.stopped.wait(.05):
        if shutdown.exists():
            service.begin_drain('fixture_done')
    service.close()
    (home/('resident-'+version+'-done')).write_text('stopped')


class Handoff(unittest.IsolatedAsyncioTestCase):
    async def test_two_bundle_four_component_handoff(self):
        from tests.support.case import wait_until_async
        from memvane.runtime.api import wake_queue
        short_temp = '/private/tmp' if Path('/private/tmp').is_dir() else '/tmp'
        with tempfile.TemporaryDirectory(prefix='mvh4-', dir=short_temp) as directory:
            home = Path(directory).resolve(); home.chmod(0o700)
            old, new = home/'old', home/'new'
            source = Path(__file__).read_text()
            for root, version in ((old,'1.0.0'), (new,'1.0.1')):
                (root/'entry').mkdir(parents=True)
                (root/'plugin.json').write_text(json.dumps({'name':'memvane','version':version}))
                (root/'entry/run.py').write_text(source)
            for name in ('queue','resident','project','runtime'):
                (home/name).mkdir(mode=0o700)
            queue = wake_queue(home/'queue')
            queue.put('laya-sync', str(home/'project'), now=time.time()+3600)
            env = {**os.environ, 'HOME':str(home), 'PYTHONPATH':str(ROOT),
                   'MEMVANE_TEST_RUNTIME_ROOT':str(home/'runtime'), 'PYTHONDONTWRITEBYTECODE':'1'}
            running = []
            try:
                for role in ('timer','monitor','resident'):
                    running.append(subprocess.Popen([sys.executable,'-B',str(old/'entry/run.py'),role,
                        str(home),str(old),str(new)], env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True))
                # Each process writes its pid after creating the file: wait for the digits, not the name.
                await wait_until_async(lambda:all((home/name).exists() and (home/name).read_text().strip().isdigit()
                    for name in ('timer-old','monitor-old','resident-old','worker-old')), timeout=30)
                old_worker = int((home/'worker-old').read_text())
                old_pids = {int((home/name).read_text()) for name in
                            ('timer-old','monitor-old','resident-old','worker-old')}
                self.assertEqual(len(old_pids), 4)
                (home/'switch').write_text('switch')
                await wait_until_async(lambda:all((home/name).exists() for name in
                    ('timer-old-done','monitor-old-done','resident-old-done')) and all(
                    (home/name).exists() and (home/name).read_text().strip().isdigit() for name in
                    ('timer-new','monitor-new','resident-new','worker-new')), timeout=45)
                for process in running:
                    self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
                self.assertNotEqual(old_worker, int((home/'worker-new').read_text()))
                with self.assertRaises(ProcessLookupError):
                    os.kill(old_worker, 0)
                new_pids = {int((home/name).read_text()) for name in
                            ('timer-new','monitor-new','resident-new','worker-new')}
                self.assertEqual(len(new_pids), 4)
                self.assertFalse(old_pids & new_pids)
                for pid in new_pids:
                    os.kill(pid, 0)
                self.assertEqual((home/'timer-old-done').read_text(), 'bundle_changed')
                self.assertEqual((home/'monitor-old-done').read_text(), 'bundle_changed')
                self.assertEqual((home/'resident-old-done').read_text(), 'stopped')
                self.assertEqual(len(queue.pending()), 1)
                with queue.connect() as db:
                    db.execute('DELETE FROM wakes')
                (home/'shutdown').write_text('done')
                await wait_until_async(lambda:all((home/name).exists() for name in
                    ('timer-new-done','monitor-new-done','resident-new-done')), timeout=30)
                self.assertEqual(len(queue.pending()), 0)
            finally:
                (home/'shutdown').write_text('done')
                with queue.connect() as db:
                    db.execute('DELETE FROM wakes')
                started = [name for name in ('timer-new','monitor-new','resident-new') if (home/name).exists()]
                try:
                    await wait_until_async(lambda:all((home/(name+'-done')).exists() for name in started), timeout=15)
                except AssertionError:
                    # Only fixture children identified by this test's exact entry and owner.
                    for name in started:
                        if (home/(name+'-done')).exists():
                            continue
                        pid = int((home/name).read_text())
                        argv = ['/bin/ps','-p',str(pid),'-o','uid=,lstart=,command=']
                        ps = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
                        again = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
                        fields = ps.stdout.strip().split(None, 6)
                        command = shlex.split(fields[6]) if len(fields) == 7 else []
                        expected = [str(new/'entry/run.py'), name.removesuffix('-new'), str(home), str(old), str(new)]
                        if (ps.returncode == again.returncode == 0 and ps.stdout == again.stdout
                                and fields[0] == str(os.getuid()) and len(command) >= 7
                                and command[2:7] == expected):
                            os.kill(pid, signal.SIGTERM)
                for process in running:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=10)
                    process.stderr.close()


if __name__ == '__main__':
    if len(sys.argv) in (5, 6):
        child(*sys.argv[1:])
    else:
        unittest.main()
