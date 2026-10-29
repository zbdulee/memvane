"""Neutral runtime lease primitives: import boundary, aliases and refusals.

Synthetic temp directories only; no model, service, queue worker or real root.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CORE=Path(__file__).resolve().parents[1]/'core'
sys.path.insert(0,str(CORE))

import runtime_lease as lease


class ImportBoundary(unittest.TestCase):
    def test_runtime_work_queue_with_explicit_root_does_not_load_context_learning(self):
        # A fresh interpreter: sibling runtime tests already import learning code.
        probe=('import sys;sys.path.insert(0,sys.argv[1]);import runtime_work;'
               "q=runtime_work.WakeQueue(sys.argv[2]);key=q.put('index','');rows=q.pending();"
               "print(repr((key is not None,[r['kind'] for r in rows],"
               "sorted(m for m in sys.modules if m=='context_learning' or m.startswith('context_learning.')))))")
        with tempfile.TemporaryDirectory() as raw:
            root=Path(os.path.realpath(raw));os.chmod(root,0o700)
            result=subprocess.run([sys.executable,'-I','-B','-c',probe,str(CORE),str(root)],
                stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=10,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),repr((True,['index'],[])))


class Aliases(unittest.TestCase):
    def test_learning_modules_re_export_the_same_objects(self):
        import runtime_work
        from context_learning import local_cycle,resident,resource_coordination
        self.assertIs(resident._private_state,lease.private_state)
        self.assertIs(resident._private_lock,lease.private_lock)
        self.assertIs(resource_coordination._lock,lease.coordination_lock)
        self.assertIs(local_cycle._atomic_json,lease.atomic_json)
        self.assertEqual(local_cycle.MAX_FILE,16*1024*1024)
        self.assertIs(runtime_work._lock,lease.coordination_lock)
        self.assertIs(runtime_work._atomic_json,lease.atomic_json)

    def test_runtime_work_shims_reach_the_real_coordinator_and_root_on_call(self):
        import runtime_work
        from context_learning import resource_coordination
        with tempfile.TemporaryDirectory() as raw:
            root=Path(os.path.realpath(raw));os.chmod(root,0o700)
            # A temp-area HOME (a sandboxed run) has its own queue root; the
            # shim delegates for every other HOME.
            with patch.object(resource_coordination,'default_root',return_value=root), \
                 patch.object(runtime_work,'disposable_home',return_value=None):
                self.assertEqual(runtime_work.default_root(),root)
                self.assertEqual(runtime_work.WakeQueue().root,root)
            controller=runtime_work.Coordinator(root)
            self.assertIsInstance(controller,resource_coordination.Coordinator)
            self.assertEqual(controller.root,root)


class Refusals(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(os.path.realpath(self.temp.name));os.chmod(self.root,0o700)

    def test_unsafe_state_directories_are_refused(self):
        self.assertEqual(lease.private_state(self.root),self.root)
        shared=self.root/'shared';shared.mkdir(mode=0o755);os.chmod(shared,0o755)
        link=self.root/'link';link.symlink_to(self.root)
        for unsafe in (Path('relative'),shared,link,self.root/'missing'):
            with self.subTest(unsafe=str(unsafe)),self.assertRaisesRegex(ValueError,'^resident_state_unsafe$'):
                lease.private_state(unsafe)

    def test_unsafe_locks_are_refused(self):
        path=self.root/'ok.lock';fd=lease.private_lock(path)
        try:self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode),0o600)
        finally:os.close(fd)
        public=self.root/'public.lock';public.write_text('');os.chmod(public,0o644)
        with self.assertRaisesRegex(ValueError,'^resident_lock_unsafe$'):lease.private_lock(public)
        link=self.root/'link.lock';link.symlink_to(path)
        with self.assertRaises(OSError):lease.private_lock(link)
        # A hard link passes the private check but never the coordination check.
        os.link(path,self.root/'alias.lock')
        fd=lease.private_lock(path);os.close(fd)
        with self.assertRaisesRegex(ValueError,'^unsafe_coordination_lock$'):lease.coordination_lock(path)


class AtomicJson(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(os.path.realpath(self.temp.name));os.chmod(self.root,0o700)

    def test_writes_canonical_utf8_bytes_with_owner_only_mode(self):
        from context_learning.store import canonical
        path=self.root/'state.json';value={'b':[1,2.5,None],'a':'한글 é','c':{'z':True,'y':False}}
        lease.atomic_json(path,value)
        expected=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
        self.assertEqual(path.read_bytes(),expected)
        self.assertEqual(path.read_bytes(),(canonical(value)+'\n').encode())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['state.json'])

    def test_oversize_is_refused_without_touching_the_target(self):
        path=self.root/'state.json';lease.atomic_json(path,{'kept':1});before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'^output_budget_exceeded$'):
            lease.atomic_json(path,'x'*lease.MAX_FILE)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['state.json'])


if __name__=='__main__':
    unittest.main()
