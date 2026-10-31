import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('first-load admission uses the measured footprint, not the RSS ceiling', () => {
  execFileSync('python3', ['-B', 'test/fixtures/context-learning-footprint-admission-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});
