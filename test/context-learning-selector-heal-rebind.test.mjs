import {test} from 'node:test';
import {execFileSync} from 'node:child_process';

test('selector heal keeps another host identical official adapter anchor', () => {
  execFileSync('python3', ['test/fixtures/context-learning-selector-heal-rebind-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
});
