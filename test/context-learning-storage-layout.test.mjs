import test from 'node:test';
import {execFileSync} from 'node:child_process';

test('plugin learning registry keeps stable IDs and scoped concurrent views', () => {
  execFileSync('python3', ['test/fixtures/context-learning-storage-layout-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: ''},
    encoding: 'utf8',
  });
});
