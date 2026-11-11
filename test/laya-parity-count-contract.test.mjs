import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('same-snapshot effective inventory and candidate trust counts', () => {
  const code = `import runpy,sys
print('python-executable=%s python-version=%s' % (sys.executable, sys.version.split()[0]), flush=True)
sys.argv=['test/fixtures/laya-shadow-migration-checks.py','MigrationSnapshotChecks.test_changed_sidecar_shadows_old_published_membership']
runpy.run_path(sys.argv[0], run_name='__main__')`;
  const result = spawnSync('python3', ['-B', '-c', code], {
    encoding: 'utf8', env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  process.stdout.write(result.stdout || '');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /same_snapshot_effective_count=103 candidate_trusted_count=3/);
});
