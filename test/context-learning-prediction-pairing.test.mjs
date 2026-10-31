import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('prediction pairing: over-budget cards keep their leading prefix, training stays strict', () => {
  execFileSync('python3', ['test/fixtures/context-learning-prediction-pairing-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});
