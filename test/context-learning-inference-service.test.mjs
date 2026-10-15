import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('user Laya singleton: private scope, bounded queue and atomic generation', () => {
  execFileSync('python3', ['test/fixtures/context-learning-inference-service-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG:'', PYTHONDONTWRITEBYTECODE:'1'},
  });
});
