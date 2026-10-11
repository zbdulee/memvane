import { test } from 'node:test';
import { execFileSync } from 'node:child_process';

test('renamed replacement cards reuse completed response and retire old lineage', () => {
  execFileSync('python3', ['test/fixtures/wiki-topical-renamed-refresh.py'], {
    encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '' },
  });
});
