"""Observer contracts: real kernel/process/wire, synthetic registered hosts."""
from dataclasses import dataclass
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('observer_base', HERE / 'laya-journal-reader-checks.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


@dataclass(frozen=True)
class FakeHostSpec:
    argv: tuple
    cwd: Path
    executable: Path
    plugin_root: Path
    release_commit: str
    manifest_sha256: str
    session_id: str


FAKE = r'''
import json, os, pathlib, subprocess, sys, time
if sys.argv[1] == 'serve-qa-stdin':
    role = 'core'
    root = str(pathlib.Path(__file__).resolve().parents[1])
    spec = json.loads((pathlib.Path(root)/'.observer-fixture.json').read_text(encoding='utf8'))
    ident, host, nonce, session, behavior, source, log = [spec[k] for k in ('ident','host','nonce','session','behavior','source','log')]
else:
    role, root, ident, host, nonce, session, behavior, source, log = sys.argv[1:]
sys.path.insert(0, source)
import laya_bridge_observer as observer
with open(log, 'a', encoding='utf8') as out:
    out.write(json.dumps(dict(pid=os.getpid(), role=role))+'\n')
if role == 'cli':
    if behavior == 'outside-owned':
        time.sleep(12)
        sys.exit(0)
    if host == 'codex' and behavior != 'late-thread':
        if behavior == 'bad-thread':
            print(json.dumps(dict(type='thread.started',thread_id=7)),flush=True)
            time.sleep(12)
            sys.exit(0)
        if behavior != 'missing-thread':
            print(json.dumps(dict(type='thread.started',thread_id=(
                'other-session' if behavior == 'thread-session' else session))),flush=True)
    if behavior == 'stdout':
        print(ident+' '+nonce, flush=True)
        sys.exit(0)
    if behavior == 'nohook':
        sys.exit(0)
    if behavior != 'manual-core':
        if behavior == 'direct-core':
            script = pathlib.Path(root)/'core/laya_host_response.py'
            argv = [sys.executable, '-B', str(script), 'serve-qa-stdin', 'recall', host, os.getcwd()]
            env = dict(os.environ)
        elif behavior == 'entrypoint':
            script = pathlib.Path(root)/'unregistered.py'
            argv = [sys.executable, '-B', '-I', str(script), 'core', *sys.argv[2:]]
            env = dict(os.environ)
        elif behavior == 'path-bash':
            argv = [str(pathlib.Path(root)/'hooks/run-hook'), 'recall', host]
            # Only the disposable fake core is reachable through this actual
            # dispatcher; keep the fixture runtime root and setup guard.
            env = dict(os.environ, PATH=str(pathlib.Path(root)/'bin')+os.pathsep+os.environ['PATH'],
                       QMD_SANDBOX='', CLAUDE_HEADLESS='', CLAUDE_SANDBOX='')
        else:
            argv = ['/bin/bash', str(pathlib.Path(root)/'hooks/run-hook'), 'recall', host]
            env = dict(os.environ, BRIDGE_FAKE_PYTHON=sys.executable)
        if behavior == 'path-bash':
            child = subprocess.run(argv, env=env, timeout=12, capture_output=True, encoding='utf8')
            pathlib.Path(log+'.dispatcher.json').write_text(json.dumps(dict(status=child.returncode,
                stdout=child.stdout[:4096],stderr=child.stderr[:4096])),encoding='utf8')
        elif behavior == 'late-thread':
            child = subprocess.Popen(argv, env=env)
            end=time.monotonic()+5
            while not pathlib.Path(log+'.packet-sent').exists():
                if time.monotonic()>=end:sys.exit(72)
                time.sleep(0.005)
            print(json.dumps(dict(type='thread.started',thread_id=session)),flush=True)
            child.wait(timeout=8)
        else:
            child = subprocess.run(argv, env=env, timeout=12)
        pathlib.Path(log+'.cli-complete').write_text('child-wait-completed', encoding='utf8')
        sys.exit(child.returncode)
if behavior == 'earlyexit':
    sys.exit(0)
observation = dict(observed_challenge_id=ident, observed_nonce_sha256=nonce,
    plugin_root=root, release_commit='c'*40, manifest_sha256='d'*64, hook_stage='registered-v4')
if behavior == 'nonce': observation['observed_nonce_sha256']='0'*64
if behavior == 'root': observation['plugin_root']=str(pathlib.Path(root).parent)
if behavior == 'late-thread':
    send=observer._send
    def sent(peer,value):
        send(peer,value)
        pathlib.Path(log+'.packet-sent').write_text('real-packet-sent',encoding='utf8')
    observer._send=sent
observer.emit_registered_hook(ident, 'codex' if behavior == 'host' else host,
    observation, 'wrong-session' if behavior == 'session' else session)
pathlib.Path(log+'.core-complete').write_text('post-ACK-emitter-returned', encoding='utf8')
'''


class ObserverChecks(unittest.TestCase):
    def setUp(self):
        self.helper = base.JournalReaderChecks('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.case = self.helper.compose()

    def observer(self):
        if importlib.util.find_spec('laya_bridge_observer') is None:
            print(json.dumps(dict(feature='laya_bridge_observer', status='absent',
                                  actualHostExecuted=False, classification='feature-RED')), flush=True)
            self.fail('observer API is not implemented; actual legacy selected proof already passed')
        module = importlib.import_module('laya_bridge_observer')
        for name in ('observe_registered_hook', 'emit_registered_hook', 'qa_hook_reply',
                     'qa_session_start', '_peer_identity', '_process_identity', '_host_spec'):
            self.assertTrue(callable(getattr(module, name, None)), name)
        return module

    def challenge(self, host_name='claude'):
        import laya_bridge_readiness as bridge
        return bridge, bridge.begin_bridge_preflight(self.case.state, release_commit='c'*40,
                                                     expected_hosts=frozenset((host_name,)))

    def collect(self, behavior, host_name='claude'):
        observer = self.observer()
        bridge, challenge = self.challenge(host_name)
        root = self.case.plugin
        (root / 'core').mkdir(mode=0o700, exist_ok=True)
        for path in (root / 'fake-cli.py', root / 'unregistered.py', root / 'core/laya_host_response.py'):
            path.write_text(FAKE, encoding='utf8')
            path.chmod(0o600)
        log = self.case.home / ('observer-' + behavior + '.jsonl')
        (root/'hooks').mkdir(mode=0o700,exist_ok=True)
        dispatcher=root/'hooks/run-hook'
        dispatcher.write_text('#!/bin/bash\nset -u\n'
            'root="$(cd "$(dirname "$0")/.." && pwd)"\n'
            '"$BRIDGE_FAKE_PYTHON" -B "$root/core/laya_host_response.py" serve-qa-stdin "$1" "$2" "$PWD"\n'
            'status=$?\nexit "$status"\n',encoding='utf8')
        dispatcher.chmod(0o700)
        if behavior == 'path-bash':
            dispatcher.write_bytes((HERE.parents[1]/'hooks/run-hook').read_bytes())
            private_bin=root/'bin'
            private_bin.mkdir(mode=0o700)
            shutil.copyfile('/bin/bash',private_bin/'bash')
            (private_bin/'bash').chmod(0o700)
            if sys.platform == 'darwin':
                # A relocated Apple platform binary needs its own fixture
                # signature. This changes only our private copy, never the
                # system binary, trust configuration or security settings.
                signed=subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(private_bin/'bash')],
                    capture_output=True,encoding='utf8',timeout=10)
                self.assertEqual(signed.returncode,0,signed.stderr)
        # Framework Python's command-line launcher execs Python.app on macOS.
        # This synthetic native HostSpec must name the stable executable,
        # rather than ask the collector to accept a changed process image.
        if sys.platform == 'darwin':
            import ctypes
            path = ctypes.create_string_buffer(4096)
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.assertGreater(lib.proc_pidpath(os.getpid(), path, len(path)), 0)
            exe = Path(path.value.decode('utf8')).resolve(strict=True)
        elif sys.platform.startswith('linux'):
            exe = Path('/proc/self/exe').resolve(strict=True)
        else:
            exe = Path(sys.executable).resolve(strict=True)
        session = 'synthetic-owned-session'
        config=root/'.observer-fixture.json'
        config.write_text(json.dumps(dict(ident=challenge.challenge_id,host=host_name,nonce=challenge.nonce_sha256,
            session=session,behavior=behavior,source=str(HERE.parents[1]/'core'),log=str(log))),encoding='utf8')
        config.chmod(0o600)
        host = FakeHostSpec((str(exe), '-B', '-I', str(root / 'fake-cli.py'), 'cli', str(root),
            challenge.challenge_id, host_name, challenge.nonce_sha256, session, behavior,
            str(HERE.parents[1] / 'core'), str(log)), self.case.project, exe, root,
            'c'*40, 'd'*64, '' if host_name == 'codex' else session)
        outside, outside_identity = None, None
        if behavior == 'outside-owned':
            argv=list(host.argv)
            argv[4]='external'
            outside=subprocess.Popen(argv,cwd=str(self.case.project),stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,encoding='utf8')
            outside_identity=observer._process_identity(outside.pid)
            # This handle is a sibling of the collector's host, not its child.
            # Only this fixture owns its cleanup; collector must not signal it.
            self.addCleanup(lambda: self.reap_outside(outside, outside_identity, observer))
        error, receipt = None, None
        with self.helper.full_authority(self.case, observer, bridge), \
                mock.patch.object(observer, '_host_spec', return_value=host), \
                mock.patch.object(observer, '_TIMEOUT', 0.8 if host_name == 'codex' else 8):
            try:
                receipt = bridge.collect_bridge_preflight(challenge, host=host_name)
            except ValueError as exc:
                error = str(exc)
                if behavior == 'path-bash':
                    print(json.dumps(dict(case='path-bash-refusal', trace=[
                        [Path(frame.filename).name,frame.lineno,frame.name]
                        for frame in traceback.extract_tb(exc.__traceback__)])),flush=True)
        if outside is not None:
            out,err=outside.communicate(timeout=10)
            print(json.dumps(dict(case='outside-owned-emitter',status=outside.returncode,
                real_pid=outside.pid,signalled=outside.returncode<0)),flush=True)
            self.assertGreaterEqual(outside.returncode,0,'collector must not signal an outside process')
        processes = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()] if log.exists() else []
        if behavior == 'path-bash' and Path(str(log)+'.dispatcher.json').exists():
            print(Path(str(log)+'.dispatcher.json').read_text(encoding='utf8'),flush=True)
        print(json.dumps(dict(case=behavior, actualHostExecuted=False, error=error,
                              receipt=None if receipt is None else str(receipt), processes=processes)), flush=True)
        # Never signal a PID from this log. The collector owns cleanup, and
        # process identity is read only for these freshly recorded children.
        for process in processes:
            try:
                identity = observer._process_identity(process['pid'])
            except (ValueError, ProcessLookupError, FileNotFoundError):
                continue
            self.fail('owned synthetic host/hook remains after collector completion: %r' % identity)
        directory = bridge._issued(challenge, bridge.BridgeChallenge)[0]
        return observer, bridge, challenge, receipt, error, processes, directory

    @staticmethod
    def reap_outside(process, identity, observer):
        if process.poll() is None and observer.same_process(identity):
            process.kill()  # Only the explicitly owned fixture sibling handle.
        process.communicate(timeout=10)

    def test_kernel_peer_identity_matches_real_connected_owned_child(self):
        observer = self.observer()
        with tempfile.TemporaryDirectory(prefix='bo-', dir='/tmp') as temp:
            path = str(Path(temp).resolve() / 'peer.sock')
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                os.chmod(path, 0o600)
                server.listen(1)
                server.settimeout(8)
                script = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b'ready'); assert s.recv(3)==b'ack'"
                child = subprocess.Popen([sys.executable, '-B', '-I', '-c', script, path],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf8')
                try:
                    with server.accept()[0] as peer:
                        self.assertEqual(peer.recv(5), b'ready')
                        uid, pid = observer._peer_identity(peer)
                        self.assertEqual((uid, pid), (os.getuid(), child.pid))
                        peer.sendall(b'ack')
                    out, err = child.communicate(timeout=8)
                    self.assertEqual(child.returncode, 0, out+err)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.communicate(timeout=5)

    def test_owned_fake_cli_descendant_transport_issues_valid_synthetic_receipt(self):
        _, bridge, _, receipt, error, processes, _ = self.collect('valid')
        self.assertIsNone(error)
        self.assertIsInstance(receipt, Path)
        record = json.loads(receipt.read_text(encoding='utf8'))
        root_pid = next(row['pid'] for row in processes if row['role'] == 'cli')
        peer_pid = next(row['pid'] for row in processes if row['role'] == 'core')
        self.assertNotEqual(root_pid, peer_pid)
        self.assertEqual(record['process_pid'], root_pid)
        with self.helper.full_authority(self.case, bridge):
            ready = bridge.check_bridge_preflight([receipt], release_commit='c'*40,
                                                   expected_hosts=frozenset(('claude',)))
        self.assertEqual(type(ready).__name__, 'BridgeReadiness')
        log = self.case.home / 'observer-valid.jsonl'
        self.assertTrue(Path(str(log)+'.core-complete').is_file(),'hook must finish after ACK')
        self.assertTrue(Path(str(log)+'.cli-complete').is_file(),'CLI must finish waiting for its hook')
        self.assertEqual(Path(str(log)+'.core-complete').read_text(encoding='utf8'), 'post-ACK-emitter-returned')
        self.assertEqual(Path(str(log)+'.cli-complete').read_text(encoding='utf8'), 'child-wait-completed')

    def test_stdout_nonce_and_manual_core_in_owned_root_are_not_hook_evidence(self):
        for behavior in ('stdout', 'manual-core'):
            with self.subTest(behavior=behavior):
                _, _, _, receipt, error, _, directory = self.collect(behavior)
                self.assertIsNone(receipt)
                self.assertIsNotNone(error)
                self.assertFalse((directory / 'claude.json').exists())

    def test_direct_core_descendant_without_registered_bash_dispatcher_has_no_receipt(self):
        _, _, _, receipt, error, _, directory = self.collect('direct-core')
        self.assertIsNone(receipt,'bare core descendant is not a registered hook invocation')
        self.assertIsNotNone(error)
        self.assertFalse((directory/'claude.json').exists())

    def test_registered_dispatcher_works_when_env_selects_a_distinct_bash_executable(self):
        _, _, _, receipt, error, _, _ = self.collect('path-bash')
        self.assertIsNone(error)
        self.assertIsNotNone(receipt, 'actual registered script must not depend on the caller PATH Bash')

    def test_kernel_peer_outside_the_owned_host_tree_is_refused_without_signalling_it(self):
        _, _, _, receipt, error, _, directory = self.collect('outside-owned')
        self.assertIsNone(receipt)
        self.assertIsNotNone(error)
        self.assertFalse((directory/'claude.json').exists())

    def test_codex_session_comes_from_the_owned_root_typed_event(self):
        _,bridge,challenge,receipt,error,_,_=self.collect('valid','codex')
        self.assertIsNone(error)
        self.assertIsNotNone(receipt)
        row=bridge._read(receipt)
        self.assertEqual(row['session_id'],'synthetic-owned-session')

    def test_codex_delayed_native_thread_event_precedes_receipt_acceptance(self):
        _,_,_,receipt,error,_,_=self.collect('late-thread','codex')
        self.assertIsNone(error)
        self.assertIsNotNone(receipt)

    def test_codex_missing_malformed_or_conflicting_native_thread_cannot_issue_receipts(self):
        for behavior in ('missing-thread','bad-thread','thread-session'):
            with self.subTest(behavior=behavior):
                _,_,_,receipt,error,_,directory=self.collect(behavior,'codex')
                self.assertIsNone(receipt)
                self.assertIsNotNone(error)
                self.assertFalse((directory/'codex.json').exists())

    def test_drip_stream_cannot_extend_an_absolute_receive_deadline(self):
        observer=self.observer()
        supported='deadline' in inspect.signature(observer._receive).parameters
        stop=threading.Event()
        reader,writer=socket.socketpair()
        with reader,writer:
            reader.settimeout(0.04)
            def drip():
                try:
                    for _ in range(10):
                        if stop.is_set():break
                        writer.sendall(b'x')
                        if stop.wait(0.02):break
                except OSError:pass
                finally:
                    writer.close()
            thread=threading.Thread(target=drip)
            start=time.monotonic()
            thread.start()
            error=None
            try:
                try:
                    if supported:observer._receive(reader,deadline=start+0.07)
                    else:observer._receive(reader)
                except (ValueError,TimeoutError,OSError) as exc:error=type(exc).__name__
                elapsed=time.monotonic()-start
            finally:
                stop.set()
                thread.join(1)
            print(json.dumps(dict(case='absolute-receive-deadline',deadline_api=supported,
                elapsed=elapsed,error=error,absolute_budget=0.07,writer_alive=thread.is_alive())),flush=True)
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(error)
            self.assertLess(elapsed,0.12,'drip bytes must not renew the absolute receive budget')

    def test_cleanup_reaps_term_ignoring_tracked_child_after_root_has_exited(self):
        observer=self.observer()
        handles,identities=[],{}
        try:
            for ignore in (False,True):
                script=('import signal,time; '+('signal.signal(signal.SIGTERM,signal.SIG_IGN); ' if ignore else '')+
                        "print('ready',flush=True); time.sleep(20)")
                process=subprocess.Popen([sys.executable,'-B','-I','-c',script],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
                handles.append(process)
                import select
                self.assertTrue(select.select([process.stdout],[],[],5)[0])
                self.assertEqual(process.stdout.readline().strip(),'ready')
                identities[process.pid]=observer._process_identity(process.pid)
            root,worker=handles
            # Sibling handles model already-tracked descendants after reparenting;
            # this unit does not claim new ancestry authority.
            observer._cleanup(root,identities)
            # The collector cannot waitpid a reparented non-child. This fixture
            # owns both handles and reaps their terminal OS exit after cleanup.
            root.wait(timeout=1)
            worker.wait(timeout=1)
            print(json.dumps(dict(case='tracked-cleanup',root_status=root.poll(),worker_status=worker.poll(),
                                   siblings_are_cleanup_control_not_ancestry=True)),flush=True)
            self.assertIsNotNone(root.poll())
            self.assertIsNotNone(worker.poll(),'root.wait completion cannot leave a tracked TERM-ignore worker alive')
        finally:
            for process in handles:
                if process.poll() is None:
                    try:
                        current=observer._process_identity(process.pid)
                    except ValueError:
                        # Exiting/zombie identities are deliberately rejected by
                        # the production identity API; wait without signalling.
                        process.wait(timeout=5)
                    else:
                        saved=identities.get(process.pid,current)
                        self.assertTrue(all(current[k]==saved[k] for k in ('pid','uid','start_key','executable')))
                        process.kill()  # Only this fixture's bootstrap Popen handle.
                process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_private_socket_rejects_symlink_modes_and_real_parent_inode_replacement(self):
        observer=self.observer()
        for variant in ('socket-mode','parent-mode','symlink','socket-inode','parent-inode','moved-parent'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(prefix='bg-',dir='/tmp') as temp:
                parent=Path(temp).resolve()/'private'
                parent.mkdir(mode=0o700)
                path=parent/'s.sock'
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path));path.chmod(0o600)
                    node=[path.stat().st_dev,path.stat().st_ino]
                    parent_node=[parent.stat().st_dev,parent.stat().st_ino]
                    if variant=='socket-mode':path.chmod(0o644)
                    elif variant=='parent-mode':parent.chmod(0o755)
                    elif variant=='symlink':
                        alias=parent/'alias.sock';alias.symlink_to(path);path=alias
                    elif variant=='socket-inode':node=[node[0],node[1]+1]
                    elif variant=='parent-inode':parent_node=[parent_node[0],parent_node[1]+1]
                    else:
                        old=parent.with_name('old-private');parent.rename(old);parent.mkdir(mode=0o700)
                        (old/'s.sock').rename(path)
                    with self.assertRaises(ValueError):observer._private_socket(path,node,parent_node)

    def test_real_packet_nonce_host_session_root_and_entrypoint_mismatches_refuse(self):
        for behavior in ('nonce', 'host', 'session', 'root', 'entrypoint'):
            with self.subTest(behavior=behavior):
                _, _, _, receipt, error, _, directory = self.collect(behavior)
                self.assertIsNone(receipt)
                self.assertIsNotNone(error)
                self.assertFalse((directory / 'claude.json').exists())

    def test_peer_early_exit_and_unloaded_hook_refuse_with_owned_children_reaped(self):
        for behavior in ('earlyexit', 'nohook'):
            with self.subTest(behavior=behavior):
                start = time.monotonic()
                _, _, _, receipt, error, _, directory = self.collect(behavior)
                self.assertIsNone(receipt)
                self.assertIsNotNone(error)
                self.assertLess(time.monotonic()-start, 15)
                self.assertFalse((directory / 'claude.json').exists())

    def test_qa_hook_normal_is_none_but_invalid_qa_is_blocked_without_receipt(self):
        observer = self.observer()
        bridge, challenge = self.challenge()
        decision = self.case.decision()
        with mock.patch.dict(os.environ):
            os.environ.pop('MEMVANE_BRIDGE_PREFLIGHT', None)
            self.assertIsNone(observer.qa_hook_reply({'prompt':'ordinary','cwd':str(self.case.project)},
                                'claude', decision, self.case.plugin))
            os.environ['MEMVANE_BRIDGE_PREFLIGHT'] = challenge.challenge_id
            response = observer.qa_hook_reply({'prompt':'bad challenge','cwd':str(self.case.project)},
                                               'claude', decision, self.case.plugin)
        self.assertEqual(response.get('decision'), 'block')
        directory = bridge._issued(challenge, bridge.BridgeChallenge)[0]
        self.assertFalse((directory / 'claude.json').exists())

    def test_qa_session_skip_requires_valid_challenge_cwd_host_and_root(self):
        observer = self.observer()
        bridge, challenge = self.challenge()
        decision = self.case.decision()
        payload = dict(prompt=bridge.challenge_prompt(challenge), cwd=str(self.case.project))
        with mock.patch.dict(os.environ), self.helper.full_authority(self.case, observer, bridge):
            os.environ.pop('MEMVANE_BRIDGE_PREFLIGHT', None)
            self.assertFalse(observer.qa_session_start(payload, 'claude', decision, self.case.plugin))
            os.environ['MEMVANE_BRIDGE_PREFLIGHT'] = challenge.challenge_id
            self.assertTrue(observer.qa_session_start(payload, 'claude', decision, self.case.plugin))
            for bad, host, root in ((dict(payload,cwd=str(self.case.home)), 'claude',self.case.plugin),
                                    (payload,'hermes',self.case.plugin), (payload,'claude',self.case.home)):
                self.assertFalse(observer.qa_session_start(bad, host, decision, root))


if __name__ == '__main__':
    unittest.main(verbosity=2)
