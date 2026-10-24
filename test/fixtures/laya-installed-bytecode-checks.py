"""Host and setup execution must preserve an exact installed plugin tree."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
import laya_product_bundle as bundle


class InstalledBytecodeChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='laya-installed-bytecode-')
        cls.root = Path(cls.temp.name).resolve()
        cls.source = cls.root / 'source'
        cls.source.mkdir(mode=0o700)
        names = subprocess.run(
            ['git', '-C', str(REPO), 'ls-files', '-z'], check=True,
            stdout=subprocess.PIPE).stdout.decode().split('\0')
        for name in filter(None, names):
            dst = cls.source / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, dst)
        git = ['git', '-C', str(cls.source)]
        subprocess.run(git + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                              '-c', 'commit.gpgsign=false', 'commit', '-qm', 'candidate'], check=True)
        cls.commit = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def installed(self, label):
        root = self.root / label / 'plugin'
        shutil.copytree(self.source, root, ignore=shutil.ignore_patterns('.git'))
        for directory, _, files in os.walk(root):
            Path(directory).chmod(0o700)
            for name in files:
                path = Path(directory) / name
                path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o600)
        return root

    def test_host_then_direct_setup_keeps_installed_tree_exact(self):
        installed = self.installed('host')
        bundle.adopt_host_loaded_plugin(self.source, installed,
                                        commit_id=self.commit, vector_profile=None)
        project = self.root / 'project'
        project.mkdir()
        home = self.root / 'home'
        home.mkdir()
        # macOS system Python redirects bytecode globally; force the ordinary
        # per-source cache behavior seen with the supported Python 3.12 host.
        bin_dir = home / 'bin'
        bin_dir.mkdir()
        python = bin_dir / 'python3'
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                          ' -X pycache_prefix= "$@"\n')
        python.chmod(0o755)
        env = dict(os.environ, HOME=str(home), QMD_RECALL_LOG='',
                   QMD_BACKEND_MANAGER='/usr/bin/true', QMD_SANDBOX='',
                   CODEX_SANDBOX='', CLAUDE_HEADLESS='', QMD_SETUP_GUARD_FIXTURE='',
                   PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'))
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.pop('PYTHONPYCACHEPREFIX', None)
        payload = ('{"cwd":"' + str(project) + '","tool_name":"Edit"}').encode()
        subprocess.run([str(installed / 'hooks/run-hook'), 'gate', 'codex'],
                       input=payload, env=env, cwd=project, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        subprocess.run([str(python), str(installed / 'core/setup_entry.py'),
                        'inspect', '--project', str(project)],
                       env=env, cwd=project, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=30)
        # Direct maintenance/status entrypoints also load installed modules.
        # They must not add bytecode to the sealed plugin cache.
        status = subprocess.run([str(python), str(installed / 'core/runtime_work.py'),
                                 'build-wake-status', '--project', str(project)],
                                env=env, cwd=project, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
        self.assertIn(status.returncode, (0, 75), status.stderr.decode(errors='replace'))
        self.assertEqual([str(p.relative_to(installed)) for p in installed.rglob('*')
                          if p.name == '__pycache__' or p.suffix in ('.pyc', '.pyo')], [])
        # Manual skill entrypoints bypass run-hook. Their setup check imports
        # installed core modules even when the project is still unconfigured.
        manager = home / 'manager.sh'
        manager.write_text('#!/bin/sh\nexit 0\n')
        manager.chmod(0o700)
        skill_env = dict(env, QMD_BACKEND_MANAGER=str(manager))
        result = subprocess.run(
            ['bash', str(installed / 'skills/query/scripts/query.sh'), str(project), 'QA'],
            env=skill_env, cwd=project, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)
        self.assertIn(result.returncode, (0, 1),
                      result.stderr.decode(errors='replace'))
        extras = [str(p.relative_to(installed)) for p in installed.rglob('*')
                  if p.name == '__pycache__' or p.suffix in ('.pyc', '.pyo')]
        self.assertEqual(extras, [])
        self.assertEqual(bundle.inspect_full_plugin_candidate(installed)['sourceCommit'],
                         self.commit)

    def test_planted_bytecode_still_fails_exact_attestation(self):
        fresh = self.installed('fresh')
        extra = fresh / 'core/__pycache__/config.cpython-39.pyc'
        extra.parent.mkdir(mode=0o700)
        extra.write_bytes(b'not trusted')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'installed_file_set_not_exact_commit'):
            bundle.adopt_host_loaded_plugin(self.source, fresh,
                                            commit_id=self.commit, vector_profile=None)
        extra.unlink()
        extra.parent.rmdir()
        bundle.adopt_host_loaded_plugin(self.source, fresh,
                                        commit_id=self.commit, vector_profile=None)
        extra.parent.mkdir(mode=0o700)
        extra.write_bytes(b'not trusted')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unexpected_bundle_entry'):
            bundle.inspect_full_plugin_candidate(fresh)


if __name__ == '__main__':
    unittest.main()
