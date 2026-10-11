import { test } from 'node:test';
import { execFileSync } from 'node:child_process';

test('reviewed complementary overlap retains immutable verified siblings', () => {
  const output = execFileSync('python3',
    ['test/fixtures/wiki-topical-reviewed-overlap.py'],
    { encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '' } });
  const result = JSON.parse(output);
  for (const value of Object.values(result)) {
    if (value !== true) throw new Error(output);
  }
});
