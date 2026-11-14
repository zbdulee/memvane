import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tests.support.case import FixtureCase
from unittest.mock import patch
from memvane.runtime import runtime_lease as lease


class Refusals(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root=self.tmp / 'state';self.root.mkdir(mode=0o700)

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


class AtomicJson(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root=self.tmp / 'state';self.root.mkdir(mode=0o700)

    def test_writes_canonical_utf8_bytes_with_owner_only_mode(self):
        path=self.root/'state.json';value={'b':[1,2.5,None],'a':'한글 é','c':{'z':True,'y':False}}
        lease.atomic_json(path,value)
        expected=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
        self.assertEqual(path.read_bytes(),expected)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['state.json'])

    def test_oversize_is_refused_without_touching_the_target(self):
        path=self.root/'state.json';lease.atomic_json(path,{'kept':1});before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'^output_budget_exceeded$'):
            lease.atomic_json(path,'x'*lease.MAX_FILE)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['state.json'])


class RuntimeAliases(FixtureCase):
    def test_runtime_work_queue_with_explicit_root_does_not_load_context_learning(self):
        root = self.tmp / 'queue'
        root.mkdir(mode=0o700)
        repo = str(Path(__file__).resolve().parents[4])
        probe = ("import sys; from memvane.runtime import runtime_work as r; "
                 "q=r.WakeQueue(sys.argv[1]);q.put('learning','/synthetic/a'); "
                 "print(any(x.startswith('memvane.learning.') for x in sys.modules))")
        result = subprocess.run([sys.executable, '-B', '-c', probe, str(root)],
                                env={**os.environ, 'PYTHONPATH': repo},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'False')

    def test_runtime_work_shims_reach_the_real_coordinator_and_root_on_call(self):
        from memvane.runtime import runtime_work, resource_coordination
        root = self.tmp / 'state'
        root.mkdir(mode=0o700)
        with patch.object(resource_coordination, 'default_root', return_value=root):
            self.assertEqual(runtime_work.default_root(), root)
            self.assertEqual(runtime_work.WakeQueue().root, root)
        controller = runtime_work.Coordinator(root)
        self.assertIsInstance(controller, resource_coordination.Coordinator)
        self.assertEqual(controller.root, root)
