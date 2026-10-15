import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
test('explicit inference identity and unchanged warm cache never train or smoke', () => {
  execFileSync('python3', ['test/fixtures/context-learning-inference-identity-checks.py'], {
    env: {...process.env, PYTHONPATH:'core', PYTHONDONTWRITEBYTECODE:'1', QMD_RECALL_LOG:''},
    encoding:'utf8', timeout:30000,
  });
});
