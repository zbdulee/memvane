"""Observer contracts: real kernel/process/wire, synthetic registered hosts."""
from dataclasses import dataclass
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
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
import json, os, pathlib, subprocess, sys
role, root, ident, host, nonce, session, behavior, source, log = sys.argv[1:]
sys.path.insert(0, source)
import laya_bridge_observer as observer
with open(log, 'a', encoding='utf8') as out:
    out.write(json.dumps(dict(pid=os.getpid(), role=role))+'\n')
if role == 'cli':
    if behavior == 'stdout':
        print(ident+' '+nonce, flush=True)
        sys.exit(0)
    if behavior == 'nohook':
        sys.exit(0)
    if behavior != 'manual-core':
        script = pathlib.Path(root)/('unregistered.py' if behavior == 'entrypoint' else 'core/laya_host_response.py')
        child = subprocess.run([sys.executable, '-B', '-I', str(script), 'core', *sys.argv[2:]], timeout=12)
        sys.exit(child.returncode)
if behavior == 'earlyexit':
    sys.exit(0)
observation = dict(observed_challenge_id=ident, observed_nonce_sha256=nonce,
    plugin_root=root, release_commit='c'*40, manifest_sha256='d'*64, hook_stage='registered-v4')
if behavior == 'nonce': observation['observed_nonce_sha256']='0'*64
if behavior == 'root': observation['plugin_root']=str(pathlib.Path(root).parent)
observer.emit_registered_hook(ident, 'codex' if behavior == 'host' else host,
    observation, 'wrong-session' if behavior == 'session' else session)
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

    def challenge(self):
        import laya_bridge_readiness as bridge
        return bridge, bridge.begin_bridge_preflight(self.case.state, release_commit='c'*40,
                                                     expected_hosts=frozenset(('claude',)))

    def collect(self, behavior):
        observer = self.observer()
        bridge, challenge = self.challenge()
        root = self.case.plugin
        (root / 'core').mkdir(mode=0o700, exist_ok=True)
        for path in (root / 'fake-cli.py', root / 'unregistered.py', root / 'core/laya_host_response.py'):
            path.write_text(FAKE, encoding='utf8')
            path.chmod(0o600)
        log = self.case.home / ('observer-' + behavior + '.jsonl')
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
        host = FakeHostSpec((str(exe), '-B', '-I', str(root / 'fake-cli.py'), 'cli', str(root),
            challenge.challenge_id, 'claude', challenge.nonce_sha256, session, behavior,
            str(HERE.parents[1] / 'core'), str(log)), self.case.project, exe, root,
            'c'*40, 'd'*64, session)
        error, receipt = None, None
        with self.helper.full_authority(self.case, observer, bridge), \
                mock.patch.object(observer, '_host_spec', return_value=host):
            try:
                receipt = bridge.collect_bridge_preflight(challenge, host='claude')
            except ValueError as exc:
                error = str(exc)
        processes = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()] if log.exists() else []
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

    def test_stdout_nonce_and_manual_core_in_owned_root_are_not_hook_evidence(self):
        for behavior in ('stdout', 'manual-core'):
            with self.subTest(behavior=behavior):
                _, _, _, receipt, error, _, directory = self.collect(behavior)
                self.assertIsNone(receipt)
                self.assertIsNotNone(error)
                self.assertFalse((directory / 'claude.json').exists())

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
