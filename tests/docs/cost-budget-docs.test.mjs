// The documented default bound follows the compile and verify budget constants.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

test('documented default paid-call bound matches the code', () => {
  const raw = execFileSync('python3', ['-B', '-c', `import json
from memvane.settings.types import DEFAULT_CONFIG, VERIFY_PRODUCED_HARD_CAP
b = DEFAULT_CONFIG['compile']['budget']
print(json.dumps([b['extractorPerRun'], b['cardsPerSource'], b['verifyPerRun'], VERIFY_PRODUCED_HARD_CAP]))
`], { encoding: 'utf8' });
  const [extract, cards, verify, cap] = JSON.parse(raw);
  const bound = extract + extract * cards + Math.max(verify, Math.min(extract * cards, cap)) + 1;
  assert.equal(bound, 141);
  assert.match(readFileSync('docs/settings.md', 'utf8'), /최대 141회/);
});
