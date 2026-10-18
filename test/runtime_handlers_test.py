"""Import-boundary guard for the fixed runtime handler table; no handler runs."""
from pathlib import Path
import subprocess
import sys
import unittest

CORE=Path(__file__).resolve().parents[1]/'core'


class HandlerImportBoundary(unittest.TestCase):
    def test_importing_handlers_does_not_load_context_learning(self):
        # A fresh interpreter: sibling runtime tests already import learning code.
        probe=('import sys;sys.path.insert(0,sys.argv[1]);import runtime_handlers;'
               "print(sorted(m for m in sys.modules if m=='context_learning' or m.startswith('context_learning.')))")
        result=subprocess.run([sys.executable,'-I','-B','-c',probe,str(CORE)],
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=10,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'[]')


if __name__=='__main__':
    unittest.main()
